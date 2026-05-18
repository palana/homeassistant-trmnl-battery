"""Constants for the TRMNL integration."""

DOMAIN = "trmnl"

# Configuration and options
CONF_API_KEY = "api_key"
CONF_API_BASE_URL = "api_base_url" # Renamed from CONF_API_ENDPOINT
CONF_DEVICE_ACCESS_TOKEN = "device_access_token" # New constant
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_SCAN_INTERVAL = 300  # 5 minutes
DEFAULT_API_BASE_URL = "https://usetrmnl.com" # Renamed and updated from DEFAULT_API_ENDPOINT
MIN_SCAN_INTERVAL = 15 # Minimum scan interval in seconds


# Battery voltage limits
MIN_VOLTAGE = 3.3   # 0 % SOC for this device
MAX_VOLTAGE = 4.1   # 100 % SOC for this device

# Charging detection
CHARGING_VOLTAGE_THRESHOLD = 4.5   # Voltage above this means charger is connected
CHARGE_DURATION_HOURS = 4          # Assumed full-charge time from 0 % SOC
