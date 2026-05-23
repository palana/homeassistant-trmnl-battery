"""Sensor platform for TRMNL integration."""
import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MIN_VOLTAGE,
    MAX_VOLTAGE,
    CONF_DEVICE_ACCESS_TOKEN,
    CHARGING_VOLTAGE_THRESHOLD,
    CHARGE_DURATION_HOURS,
    CC_SOC_THRESHOLD,
    CV_SLOWDOWN_FACTOR,
    VOLTAGE_EMA_ALPHA,
)

_LOGGER = logging.getLogger(__name__)

def calculate_battery_percentage(voltage):
    """Calculate battery percentage based on voltage."""
    if voltage <= MIN_VOLTAGE:
        return 0
    if voltage >= MAX_VOLTAGE:
        return 100

    percentage = ((voltage - MIN_VOLTAGE) / (MAX_VOLTAGE - MIN_VOLTAGE)) * 100
    return percentage

async def async_setup_entry(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up TRMNL sensor based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # Create sensors for each device
    entities = []
    for device in coordinator.data:
        entities.append(TrmnlBatterySensor(coordinator, device))
        entities.append(TrmnlBatteryPercentageSensor(coordinator, device))
        entities.append(TrmnlRssiSensor(coordinator, device))
        # Conditionally add TrmnlLastSeenSensor
        if entry.data.get(CONF_DEVICE_ACCESS_TOKEN): # Check if token is provided
            entities.append(TrmnlLastSeenSensor(coordinator, device))

    async_add_entities(entities)

class TrmnlBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for TRMNL sensors."""

    def __init__(self, coordinator, device):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.device = device
        self._friendly_id = device["friendly_id"]
        self._mac_address = device["mac_address"]
        self._name = device["name"]

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._mac_address)},
            "name": self._name,
            "manufacturer": "TRMNL",
            "model": "e-ink display",
        }

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        # Simply use the current time as the last_updated attribute
        return {
            "last_updated": dt_util.utcnow().isoformat()
        }

    def get_device_data(self):
        """Get current device data from coordinator."""
        for device in self.coordinator.data:
            if device["friendly_id"] == self._friendly_id:
                return device
        return None

class TrmnlBatterySensor(TrmnlBaseSensor):
    """Representation of a TRMNL battery voltage sensor."""

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device)
        self._voltage_ema: float | None = None
        self._was_charging: bool = False

    @callback
    def _handle_coordinator_update(self):
        device_data = self.get_device_data()
        if device_data:
            voltage = float(device_data["battery_voltage"])
            is_charging = voltage > CHARGING_VOLTAGE_THRESHOLD
            if self._voltage_ema is None or (self._was_charging and not is_charging):
                # First reading or just unplugged: reset to actual voltage.
                self._voltage_ema = voltage
            elif is_charging:
                self._voltage_ema = VOLTAGE_EMA_ALPHA * voltage + (1 - VOLTAGE_EMA_ALPHA) * self._voltage_ema
            else:
                # Discharge: clamp so EMA only moves downward.
                new_ema = VOLTAGE_EMA_ALPHA * voltage + (1 - VOLTAGE_EMA_ALPHA) * self._voltage_ema
                self._voltage_ema = min(self._voltage_ema, new_ema)
            self._was_charging = is_charging
        self.async_write_ha_state()

    @property
    def unique_id(self):
        """Return a unique ID to use for this entity."""
        return f"{self._mac_address}_battery"

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"{self._name} Battery"

    @property
    def state(self):
        return self._voltage_ema

    @property
    def extra_state_attributes(self):
        device_data = self.get_device_data()
        return {
            "raw_voltage": float(device_data["battery_voltage"]) if device_data else None,
            "last_updated": dt_util.utcnow().isoformat(),
        }

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return "V"

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        return "voltage"

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        return SensorStateClass.MEASUREMENT

    @property
    def icon(self):
        """Return the icon of the sensor."""
        return "mdi:battery"


class TrmnlBatteryPercentageSensor(TrmnlBaseSensor, RestoreEntity):
    """Battery percentage sensor with charging-state tracking.

    Extends RestoreEntity so charging state survives HA restarts.
    Charging is detected when voltage exceeds CHARGING_VOLTAGE_THRESHOLD.
    While charging, SOC is linearly interpolated from soc_at_charge_start
    to 100 % over CHARGE_DURATION_HOURS instead of using the raw voltage curve.
    """

    def __init__(self, coordinator, device):
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._computed_percentage = None
        self._charging = False
        self._soc_at_charge_start = None
        self._charge_start_time = None
        self._voltage_ema: float | None = None
        self._was_charging: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self):
        """Restore state, then re-derive current charging context."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes:
            attrs = last_state.attributes
            soc_start = attrs.get("soc_at_charge_start")
            if soc_start is not None:
                self._soc_at_charge_start = float(soc_start)
            start_str = attrs.get("charge_start_time")
            if start_str:
                self._charge_start_time = dt_util.parse_datetime(start_str)
            try:
                self._computed_percentage = float(last_state.state)
            except (ValueError, TypeError):
                pass

        # Recompute with restored charging context so the first written
        # state after restart is already correct.
        device_data = self.get_device_data()
        if device_data:
            voltage = float(device_data["battery_voltage"])
            self._voltage_ema = voltage
            self._update_charging_state(voltage)
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Coordinator update
    # ------------------------------------------------------------------

    @callback
    def _handle_coordinator_update(self):
        """Handle updated data from the coordinator."""
        device_data = self.get_device_data()
        if device_data:
            voltage = float(device_data["battery_voltage"])
            is_charging = voltage > CHARGING_VOLTAGE_THRESHOLD
            if self._voltage_ema is None or (self._was_charging and not is_charging):
                self._voltage_ema = voltage
            elif is_charging:
                self._voltage_ema = VOLTAGE_EMA_ALPHA * voltage + (1 - VOLTAGE_EMA_ALPHA) * self._voltage_ema
            else:
                new_ema = VOLTAGE_EMA_ALPHA * voltage + (1 - VOLTAGE_EMA_ALPHA) * self._voltage_ema
                self._voltage_ema = min(self._voltage_ema, new_ema)
            self._was_charging = is_charging
            self._update_charging_state(self._voltage_ema)
        self.async_write_ha_state()

    def _update_charging_state(self, voltage):
        if voltage > CHARGING_VOLTAGE_THRESHOLD:
            if not self._charging:
                if self._charge_start_time is None:
                    # New charge cycle: snapshot current SOC and record start time.
                    self._soc_at_charge_start = (
                        self._computed_percentage if self._computed_percentage is not None else 0
                    )
                    self._charge_start_time = dt_util.utcnow()
                # else: restored from a previous cycle — keep existing values.
                self._charging = True
            elapsed_h = (
                (dt_util.utcnow() - self._charge_start_time).total_seconds() / 3600
            )
            soc_start = self._soc_at_charge_start if self._soc_at_charge_start is not None else 0.0
            cc_rate = 100.0 / CHARGE_DURATION_HOURS
            cv_rate = cc_rate * CV_SLOWDOWN_FACTOR
            if soc_start < CC_SOC_THRESHOLD:
                cc_time = (CC_SOC_THRESHOLD - soc_start) / cc_rate
                if elapsed_h <= cc_time:
                    soc = soc_start + elapsed_h * cc_rate
                else:
                    soc = CC_SOC_THRESHOLD + (elapsed_h - cc_time) * cv_rate
            else:
                soc = soc_start + elapsed_h * cv_rate
            self._computed_percentage = min(100.0, max(0.0, soc))
        else:
            self._charging = False
            self._soc_at_charge_start = None
            self._charge_start_time = None
            self._computed_percentage = calculate_battery_percentage(voltage)

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    suggested_display_precision = 1

    @property
    def unique_id(self):
        """Return a unique ID to use for this entity."""
        return f"{self._mac_address}_battery_percentage"

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"{self._name} Battery Percentage"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._computed_percentage

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return "%"

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        return "battery"

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        return SensorStateClass.MEASUREMENT

    @property
    def icon(self):
        """Return the icon of the sensor."""
        return "mdi:battery"

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        attrs = {
            "charging_state": "charging" if self._charging else "discharging",
            "voltage": self._voltage_ema,
            "last_updated": dt_util.utcnow().isoformat(),
        }
        if self._charging:
            attrs["soc_at_charge_start"] = self._soc_at_charge_start
            if self._charge_start_time:
                attrs["charge_start_time"] = self._charge_start_time.isoformat()
                soc_now = self._computed_percentage if self._computed_percentage is not None else 0.0
                cc_rate = 100.0 / CHARGE_DURATION_HOURS
                cv_rate = cc_rate * CV_SLOWDOWN_FACTOR
                if soc_now < CC_SOC_THRESHOLD:
                    time_remaining = (CC_SOC_THRESHOLD - soc_now) / cc_rate + (100.0 - CC_SOC_THRESHOLD) / cv_rate
                else:
                    time_remaining = max(0.0, (100.0 - soc_now) / cv_rate)
                attrs["time_remaining_hours"] = round(time_remaining, 2)
        return attrs


class TrmnlRssiSensor(TrmnlBaseSensor):
    """Representation of a TRMNL RSSI sensor."""

    @property
    def unique_id(self):
        """Return a unique ID to use for this entity."""
        return f"{self._mac_address}_rssi"

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"{self._name} Signal Strength"

    @property
    def state(self):
        """Return the state of the sensor."""
        device_data = self.get_device_data()
        if device_data:
            return device_data["rssi"]
        return None

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return "dBm"

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        return "signal_strength"

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        return SensorStateClass.MEASUREMENT

    @property
    def icon(self):
        """Return the icon of the sensor."""
        return "mdi:wifi"


class TrmnlLastSeenSensor(TrmnlBaseSensor):
    """Representation of when the TRMNL device was last seen."""

    @property
    def unique_id(self):
        """Return a unique ID to use for this entity."""
        return f"{self._mac_address}_last_seen"

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"{self._name} Last Seen"

    @property
    def state(self):
        """Return the state of the sensor."""
        rendered_at_str = self.coordinator.last_rendered_at
        if rendered_at_str:
            try:
                # Attempt to parse the timestamp.
                # TRMNL API's `rendered_at` format is assumed to be ISO 8601.
                # If it's a different format, this parsing might need adjustment.
                # Example from a similar API: "2023-10-26T10:30:00Z"
                parsed_datetime = dt_util.parse_datetime(rendered_at_str)
                if parsed_datetime:
                    # Ensure it's timezone-aware and in UTC for Home Assistant
                    return dt_util.as_utc(parsed_datetime).isoformat()
                else:
                    _LOGGER.warning(
                        "Failed to parse 'rendered_at' timestamp: %s for device %s",
                        rendered_at_str,
                        self._friendly_id
                    )
                    return None # Or handle as an error state if preferred
            except ValueError as e:
                _LOGGER.error(
                    "ValueError parsing 'rendered_at' timestamp '%s' for device %s: %s",
                    rendered_at_str,
                    self._friendly_id,
                    e
                )
                return None
        return None

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        return "timestamp"

    @property
    def icon(self):
        """Return the icon of the sensor."""
        return "mdi:clock-outline"
