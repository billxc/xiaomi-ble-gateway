"""Integration constants; no credentials or protocol guesses."""
import math

DOMAIN = "xiaomi_ble_gateway"
CONF_ACCOUNT_ID = "account_id"
CONF_DEVICES = "devices"
CONF_UPLOAD = "enable_co2_upload"
CONF_TH_UPLOAD = "enable_temperature_humidity_upload"
CONF_TH_ENABLED_AT = "temperature_humidity_upload_enabled_at"


def environment_config(data):
    """Snapshot both independent permissions and the TH reception epoch."""
    return (data.get(CONF_UPLOAD, False), data.get(CONF_TH_UPLOAD, False),
            data.get(CONF_TH_ENABLED_AT, 0))

CONF_WEIGHT_UPLOAD = "enable_weight_upload"
CONF_WEIGHT_DEVICE = "weight_upload_device"
CONF_WEIGHT_PROFILE = "weight_upload_profile"
CONF_WEIGHT_ENABLED_AT = "weight_upload_enabled_at"
CONF_WEIGHT_MODE = "weight_upload_mode"
CONF_WEIGHT_THRESHOLD = "weight_match_threshold"
WEIGHT_MODE_FIXED = "fixed"
WEIGHT_MODE_NEAREST = "nearest"
# Missing mode must preserve the behavior of already-enabled legacy entries.
DEFAULT_WEIGHT_MODE = WEIGHT_MODE_FIXED
DEFAULT_WEIGHT_THRESHOLD = 2.5
WEIGHT_NEAREST_AUDIT_KEY = "__nearest__"  # Local Store only; never a cloud profile.
WEIGHT_CONFIG_KEYS = (CONF_WEIGHT_UPLOAD, CONF_WEIGHT_DEVICE,
                      CONF_WEIGHT_PROFILE, CONF_WEIGHT_ENABLED_AT,
                      CONF_WEIGHT_MODE, CONF_WEIGHT_THRESHOLD)


def positive_weight_threshold(value):
    """Finite positive kg threshold, shared by options validation and runtime."""
    if isinstance(value, bool):
        raise ValueError("Threshold must be a positive finite float")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Threshold must be a positive finite float")
    return value


def weight_config(data):
    """Immutable configuration snapshot; absent options are fail-closed."""
    return (data.get(CONF_WEIGHT_UPLOAD, False), data.get(CONF_WEIGHT_DEVICE),
            data.get(CONF_WEIGHT_PROFILE), data.get(CONF_WEIGHT_ENABLED_AT, 0),
            data.get(CONF_WEIGHT_MODE, DEFAULT_WEIGHT_MODE),
            data.get(CONF_WEIGHT_THRESHOLD, DEFAULT_WEIGHT_THRESHOLD))

CONF_RECORD_RAW = "record_raw"
CONF_RETENTION_DAYS = "retention_days"
DEFAULT_RECORD_RAW = True
DEFAULT_RETENTION_DAYS = 3
DATA_RECORDER_MANAGER = "recorder_manager"
FE95_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
MODEL_PIDS = {"xiaomi.sensor_ht.new3pr": 39609, "xiaomi.scales.ms110": 19205}
CO2_MODEL = "xiaomi.sensor_ht.new3pr"
SCALE_MODEL = "xiaomi.scales.ms110"


def bluetooth_address(mac: str) -> str:
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))
