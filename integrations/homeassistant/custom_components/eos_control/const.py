"""Constants of EOS Control."""

from __future__ import annotations

DOMAIN = "eos_control"
PLATFORMS = ["sensor", "binary_sensor"]

SUBENTRY_ROOM = "room"
SUBENTRY_DEVICE = "device"

# Entry data
CONF_URL = "url"
CONF_DEVICE_ID = "device_id"
CONF_SCAN_INTERVAL = "scan_interval"

# Options: context entities (all optional)
CONF_HOLIDAY_ENTITY = "holiday_entity"
CONF_HOLIDAY_RETURN_ENTITY = "holiday_return_entity"
CONF_REDUCED_SETPOINT_ENTITY = "reduced_setpoint_entity"
CONF_HEATING_SEASON_ENTITY = "heating_season_entity"
CONF_HEATING_SEASON_STATES = "heating_season_states"
CONF_SG_ACTUAL_ENTITY = "sg_actual_entity"
CONF_ZONE_ALLOWED_ENTITY = "zone_allowed_entity"
CONF_REDUCTION_LOCK_ENTITY = "reduction_lock_entity"
# Options: shadow comparison (all optional)
CONF_SG_REFERENCE_ENTITY = "sg_reference_entity"
CONF_STANDBY_REFERENCE_ENTITY = "standby_reference_entity"
CONF_STANDBY_REFERENCE_STATE = "standby_reference_state"

# Options: rules
CONF_SG_REDUCED = "sg_reduced"
CONF_SG_NORMAL = "sg_normal"
CONF_SG_RAISED = "sg_raised"
CONF_SG_PREHEAT = "sg_preheat"
CONF_SG_FORCED = "sg_forced"
CONF_PLAN_MAX_AGE = "plan_max_age_min"
CONF_HYSTERESIS = "reduction_hysteresis_k"
CONF_MIN_VALID_ROOMS = "min_valid_rooms"
CONF_MAX_ROOM = "max_room_c"
CONF_AIRING_RUN_ON = "airing_run_on_min"
CONF_HOLIDAY_FLOOR = "holiday_floor_c"
CONF_HOLIDAY_RESTART = "holiday_restart_hours"
CONF_HOLIDAY_DHW_LEAD = "holiday_dhw_lead_hours"
CONF_SETTLE = "settle_seconds"

# Device subentry (battery, electric vehicle, home appliance from EOS)
CONF_EOS_DEVICE = "eos_device_id"
CONF_KIND = "kind"

# Room subentry
CONF_NAME = "name"
CONF_TEMPERATURE_ENTITY = "temperature_entity"
CONF_LOWER = "lower_c"
CONF_UPPER = "upper_c"
CONF_WINDOWS = "window_entities"
CONF_HUMIDITY_ENTITY = "humidity_entity"
CONF_HUMIDITY_MAX = "humidity_max_pct"
CONF_HUMIDITY_DELAY = "humidity_delay_min"
CONF_IN_ZONE = "in_zone"
CONF_SETPOINT_ENTITY = "setpoint_entity"

DEFAULTS = {
    CONF_SCAN_INTERVAL: 60,
    CONF_HEATING_SEASON_STATES: "on, heat, heating",
    CONF_STANDBY_REFERENCE_STATE: "standby",
    CONF_SG_REDUCED: 1,
    CONF_SG_NORMAL: 2,
    CONF_SG_RAISED: 3,
    CONF_SG_PREHEAT: 3,
    CONF_SG_FORCED: 4,
    CONF_PLAN_MAX_AGE: 45,
    CONF_HYSTERESIS: 0.5,
    CONF_MIN_VALID_ROOMS: 1,
    CONF_MAX_ROOM: 23.5,
    CONF_AIRING_RUN_ON: 30,
    CONF_HOLIDAY_FLOOR: 16.0,
    CONF_HOLIDAY_RESTART: 1.0,
    CONF_HOLIDAY_DHW_LEAD: 3.0,
    CONF_SETTLE: 10,
    CONF_LOWER: 19.5,
    CONF_UPPER: 23.5,
    CONF_HUMIDITY_MAX: 65.0,
    CONF_HUMIDITY_DELAY: 60,
    CONF_IN_ZONE: False,
}

# Consecutive polls with a different result before the shadow comparison counts a mismatch
MISMATCH_CONFIRM_POLLS = 2
MISMATCH_LOG_SIZE = 30
