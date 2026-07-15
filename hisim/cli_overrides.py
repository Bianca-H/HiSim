"""Helpers for passing command-line overrides into system setups.

This module provides a tiny in-process "override registry" that `hisim_main.py`
can populate from CLI args (e.g. ARCH=..., WEATHER=...), and system setups can
optionally consume to override configuration defaults.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from hisim import log

_OVERRIDES: Dict[str, str] = {}
_USED: Dict[str, str] = {}


def set_overrides(overrides: Dict[str, str]) -> None:
    """Replace current overrides with given mapping (keys are uppercased)."""
    global _OVERRIDES  # noqa: PLW0603
    _OVERRIDES = {str(k).strip().upper(): str(v).strip() for k, v in overrides.items()}
    # Reset used markers for this run.
    global _USED  # noqa: PLW0603
    _USED = {}


def get_override(key: str) -> Optional[str]:
    """Get an override value by key (case-insensitive)."""
    return _OVERRIDES.get(str(key).strip().upper())


def set_used_value(key: str, value: str) -> None:
    """Record the actually used value for a key (e.g. ARCH/WEATHER)."""
    _USED[str(key).strip().upper()] = str(value).strip()


def get_used_value(key: str) -> Optional[str]:
    """Get the actually used value for a key (e.g. ARCH/WEATHER)."""
    return _USED.get(str(key).strip().upper())


def warn_unused_overrides(used_keys: set[str]) -> None:
    """Warn if overrides were provided but not used by the setup."""
    unused = sorted(set(_OVERRIDES.keys()) - {k.upper() for k in used_keys})
    for key in unused:
        log.warning(f"CLI override {key} was provided but not used by this system setup.")


# Swiss SFH / SIA natural ventilation defaults (occupancy + floor-area minimum).
DEFAULT_NATURAL_VENTILATION_M3_PER_H_PER_PERSON = 29.0
DEFAULT_SIA_NATURAL_VENTILATION_M3_PER_H_PER_M2 = 0.6
# Swiss infiltration air-change rates (replace TABULA n_air_infiltration when enabled).
SWISS_INFILTRATION_RATE_ARCH_01_TO_05_PER_H = 0.3
SWISS_INFILTRATION_RATE_ARCH_06_TO_09_PER_H = 0.15
# Operative-temperature heating demand (used by CH batch setups hp/bo/bp/bg/gr).
HEATING_DEMAND_MODE_ISO13790 = "iso13790"
HEATING_DEMAND_MODE_OPERATIVE_COMFORT = "operative_comfort_proportional"
DEFAULT_OPERATIVE_HEATING_PROPORTIONAL_BAND_IN_CELSIUS = 0.5
DEFAULT_OPERATIVE_HEATING_PROPORTIONAL_GAIN = 3.0
DEFAULT_OPERATIVE_HEATING_MINIMUM_FRACTION_BELOW_COMFORT_LOWER = 0.85
DEFAULT_OPERATIVE_COMFORT_INNER_OFFSET_LOWER_IN_CELSIUS = 1.0
DEFAULT_OPERATIVE_COMFORT_INNER_OFFSET_UPPER_IN_CELSIUS = 0.5
DEFAULT_HEATING_DISABLED_ABOVE_RUNNING_MEAN_OUTDOOR_TEMPERATURE_IN_CELSIUS = 18.0

# Simulation scenarios (CLI: SCENARIO=...; default status quo when omitted or SCENARIO=none).
# Comma-separated values are supported, e.g. SCENARIO=fossil_Crisis,heatwave,financial_Shock
SCENARIO_FOSSIL_CRISIS = "fossil_Crisis"
SCENARIO_HEATWAVE = "heatwave"
SCENARIO_FINANCIAL_SHOCK = "financial_Shock"
_KNOWN_SCENARIOS = {
    SCENARIO_FOSSIL_CRISIS.lower(): SCENARIO_FOSSIL_CRISIS,
    SCENARIO_HEATWAVE.lower(): SCENARIO_HEATWAVE,
    SCENARIO_FINANCIAL_SHOCK.lower(): SCENARIO_FINANCIAL_SHOCK,
}

# Time horizon (CLI: TIME_HORIZON=present|future; default present → result tag 25, future → 50).
TIME_HORIZON_PRESENT = "present"
TIME_HORIZON_FUTURE = "future"
PRESENT_ECONOMIC_YEAR = 2021
FUTURE_ECONOMIC_YEAR = 2050

CH_CUSTOM_CSV_WEATHER_LOCATIONS = frozenset({"ZUESTA", "BASSTA", "KLO", "RUE"})

# Swiss CH batch setups (hp/bo/bp/bg/gr): ISO/fallback heating setpoint (operative control uses adaptive band).
DEFAULT_HEATING_SETPOINT_IN_CELSIUS = 20.5
# Shift adaptive comfort lower bound down for fossil crisis (control only; upper bound and KPI degree-hours stay unshifted).
FOSSIL_CRISIS_COMFORT_LOWER_BOUND_SHIFT_IN_CELSIUS = -1.5 # based on Jaeger-Erben et. al (225): Policies for times of disruptions: How households in Europe dealt with the energy crisis in the winter 2022/2023
FOSSIL_CRISIS_ELECTRICITY_PRICE_MULTIPLIER = 1.29 # Swiss difference between 2022 and 2023 electricity prices based on ElComElectricityPrices2021; applied for present and future fossil crisis runs
FOSSIL_CRISIS_GAS_PRICE_MULTIPLIER = 1.95 # based on Jaeger-Erben et. al (225): Policies for times of disruptions: How households in Europe dealt with the energy crisis in the winter 2022/2023; Swiss difference between 2021 and 2022
FOSSIL_CRISIS_OIL_PRICE_MULTIPLIER = 1.95 # based on Jaeger-Erben et. al (225): Policies for times of disruptions: How households in Europe dealt with the energy crisis in the winter 2022/2023; Swiss difference between 2021 and 2022
# Financial shock (future only): renewable-mix progress from present→2050 reaches only this share of the planned path.
# Based on the post 25 years (2005-2030) renewable-share shortfall vs linear expectation (84.2%).
FINANCIAL_SHOCK_RENEWABLE_PROGRESS_FACTOR = 0.842
# Footprint fields with a mixed energy carrier at the base (progress toward cleaner 2050 mix is incomplete).
FINANCIAL_SHOCK_FOOTPRINT_FIELDS = (
    "electricity_footprint_in_kg_per_kwh",
    "district_heating_footprint_in_kg_per_kwh",
    "district_cooling_footprint_in_kg_per_kwh",
)


def get_active_scenarios() -> Set[str]:
    """Return all active scenario flags from comma-separated CLI override SCENARIO.

    Accepts both ``SCENARIO=fossil_Crisis,heatwave`` and JSON-list-like strings
    such as ``SCENARIO=['fossil_Crisis', 'heatwave']``.
    """
    raw = get_override("SCENARIO")
    if raw is None:
        return set()
    normalized = raw.strip()
    if normalized.upper() in ("", "NONE", "NULL"):
        return set()
    # Tolerate accidental Python/JSON list serialization from batch configs.
    normalized = normalized.strip("[]")
    active: Set[str] = set()
    for token in normalized.split(","):
        part = token.strip().strip("'\"")
        if not part:
            continue
        canonical = _KNOWN_SCENARIOS.get(part.lower())
        if canonical is None:
            log.warning(f"Unknown SCENARIO token {part!r}; ignoring.")
            continue
        active.add(canonical)
    return active


def has_scenario(scenario_name: str) -> bool:
    """Return True when the given scenario flag is active."""
    return scenario_name in get_active_scenarios()


def get_scenario() -> Optional[str]:
    """Return a primary scenario name for backward compatibility, or None for status quo."""
    active = get_active_scenarios()
    if not active:
        return None
    if SCENARIO_FOSSIL_CRISIS in active:
        return SCENARIO_FOSSIL_CRISIS
    return sorted(active)[0]


def get_time_horizon() -> str:
    """Return TIME_HORIZON from CLI (present or future); default is present."""
    raw = get_override("TIME_HORIZON") or get_override("HORIZON")
    if raw is None:
        return TIME_HORIZON_PRESENT
    val = raw.strip().lower()
    if val in ("present", "25", "now", "current", "status_quo", "status-quo"):
        return TIME_HORIZON_PRESENT
    if val in ("future", "50", "2050", "2060"):
        return TIME_HORIZON_FUTURE
    raise ValueError(
        f"Unknown TIME_HORIZON={raw!r}. Use present (default) or future."
    )


def get_economic_year() -> int:
    """Economic year for OPEX/CAPEX lookup: 2021 (present) or 2050 (future)."""
    return PRESENT_ECONOMIC_YEAR if get_time_horizon() == TIME_HORIZON_PRESENT else FUTURE_ECONOMIC_YEAR


def validate_ch_batch_cli_configuration() -> None:
    """Validate Swiss CH batch CLI combinations before building the model."""
    if has_scenario(SCENARIO_HEATWAVE) and get_time_horizon() != TIME_HORIZON_FUTURE:
        raise ValueError(
            "SCENARIO=heatwave requires TIME_HORIZON=future. "
            "No present-day heatwave weather file is available."
        )
    if has_scenario(SCENARIO_FINANCIAL_SHOCK) and get_time_horizon() != TIME_HORIZON_FUTURE:
        raise ValueError(
            "SCENARIO=financial_Shock requires TIME_HORIZON=future. "
            "The financial-shock renewable shortfall is defined relative to the 2050 pathway."
        )
    if has_scenario(SCENARIO_FINANCIAL_SHOCK):
        log.information(
            "Applied scenario SCENARIO=financial_Shock: "
            f"future mixed-carrier footprints reach only "
            f"{100.0 * FINANCIAL_SHOCK_RENEWABLE_PROGRESS_FACTOR:.1f}% of planned 2021->2050 progress; "
            "2050 fuel costs unchanged."
        )


def get_custom_csv_filename_for_location(weather_location: str) -> str:
    """Return custom CSV filename for Swiss location, time horizon, and weather profile."""
    location = weather_location.strip().upper()
    if location not in CH_CUSTOM_CSV_WEATHER_LOCATIONS:
        raise ValueError(f"Weather location {weather_location!r} is not a Swiss custom CSV location.")

    use_heat_profile = has_scenario(SCENARIO_HEATWAVE)
    if get_time_horizon() == TIME_HORIZON_PRESENT:
        present_files = {
            "ZUESTA": "ZUESTA_2023_DRY.csv",
            "BASSTA": "BASSTA_2023_DRY.csv",
            "KLO": "KLO_2023_DRY.csv",
            "RUE": "RUE_2023_DRY.csv",
        }
        if use_heat_profile:
            raise ValueError(
                "SCENARIO=heatwave requires TIME_HORIZON=future. "
                "No present-day heatwave weather file is available."
            )
        return present_files[location]

    profile = "HEAT" if use_heat_profile else "DRY"
    future_files = {
        "ZUESTA": f"NABZUE_2060_RCP85_{profile}.csv",
        "BASSTA": f"BKLI_2060_RCP85_{profile}.csv",
        "KLO": f"KLO_2060_RCP85_{profile}.csv",
        "RUE": f"RUE_2060_RCP85_{profile}.csv",
    }
    return future_files[location]


# Short tags appended to flat result folder names (see simulator.prepare_simulation_directory).
# With TIME_HORIZON=future → `50_ES_` (documents ES50); stacking e.g. `50_FC_ES_HW_`.
SCENARIO_RESULT_DIRECTORY_TAGS = {
    SCENARIO_FOSSIL_CRISIS: "FC",
    SCENARIO_FINANCIAL_SHOCK: "ES",
    SCENARIO_HEATWAVE: "HW",
}


def get_result_directory_horizon_tag() -> str:
    """Return horizon suffix for result paths: 25_ (present) or 50_ (future)."""
    return "25_" if get_time_horizon() == TIME_HORIZON_PRESENT else "50_"


def get_result_directory_scenario_tag() -> str:
    """Return concatenated scenario suffixes for result paths, e.g. 'FC_ES_HW_'."""
    tags: list[str] = []
    for scenario_name in (SCENARIO_FOSSIL_CRISIS, SCENARIO_FINANCIAL_SHOCK, SCENARIO_HEATWAVE):
        if has_scenario(scenario_name):
            tag = SCENARIO_RESULT_DIRECTORY_TAGS.get(scenario_name)
            if tag:
                tags.append(tag)
    if not tags:
        return ""
    return "_".join(tags) + "_"


def incomplete_renewable_progress_value(base_value: float, target_value: float, progress_factor: float) -> float:
    """Return incomplete progress from base→target: base - factor * (base - target)."""
    return base_value - float(progress_factor) * (base_value - target_value)


def apply_batch_open_explorer_setting(my_simulation_parameters: Any) -> None:
    """Open the result folder in Explorer for single runs and the first batch run.

    ``enable_minimal_variant_artifacts()`` does not include ``OPEN_DIRECTORY_IN_EXPLORER``.
    Batch runners pass ``BATCH_OPEN_EXPLORER=0`` for subsequent runs to suppress it.
    """
    from hisim.postprocessingoptions import PostProcessingOptions

    batch_open_explorer = (get_override("BATCH_OPEN_EXPLORER") or "").strip()
    set_used_value("BATCH_OPEN_EXPLORER", batch_open_explorer if batch_open_explorer else "1")
    if batch_open_explorer == "0":
        my_simulation_parameters.post_processing_options = [
            opt
            for opt in my_simulation_parameters.post_processing_options
            if opt != PostProcessingOptions.OPEN_DIRECTORY_IN_EXPLORER
        ]
    elif PostProcessingOptions.OPEN_DIRECTORY_IN_EXPLORER not in my_simulation_parameters.post_processing_options:
        my_simulation_parameters.post_processing_options.append(PostProcessingOptions.OPEN_DIRECTORY_IN_EXPLORER)


def apply_fossil_crisis_scenario_settings(building_config: Any) -> None:
    """Lower the adaptive heating control lower bound; upper bound and KPI degree-hours keep standard values."""
    building_config.control_comfort_lower_bound_shift_in_celsius = (
        FOSSIL_CRISIS_COMFORT_LOWER_BOUND_SHIFT_IN_CELSIUS
    )


def apply_scenario_building_settings(building_config: Any) -> None:
    """Apply building-side effects for the active CLI scenario."""
    if has_scenario(SCENARIO_FOSSIL_CRISIS):
        apply_fossil_crisis_scenario_settings(building_config)


def get_swiss_infiltration_rate_for_arch(arch_value: str) -> float:
    """Return Swiss infiltration rate [1/h] for ARCH codes 01_CH … 09_CH."""
    arch_token = str(arch_value).strip().split("_", maxsplit=1)[0]
    try:
        arch_num = int(arch_token)
    except ValueError as exc:
        raise ValueError(f"Cannot parse ARCH number from {arch_value!r}.") from exc
    if 1 <= arch_num <= 5:
        return SWISS_INFILTRATION_RATE_ARCH_01_TO_05_PER_H
    if 6 <= arch_num <= 9:
        return SWISS_INFILTRATION_RATE_ARCH_06_TO_09_PER_H
    raise ValueError(
        f"Swiss infiltration is defined for ARCH 01–09; got ARCH={arch_value!r} (number {arch_num})."
    )


def apply_swiss_infiltration_settings(building_config: Any, arch_value: str) -> None:
    """Set Swiss infiltration rate on the building config (replaces TABULA n_air_infiltration)."""
    building_config.swiss_infiltration_rate_per_h = get_swiss_infiltration_rate_for_arch(arch_value)


def apply_operative_comfort_heating_demand_settings(building_config: Any) -> None:
    """Use operative heating demand aligned with strict_comfort_band_v1 lower target."""
    building_config.heating_demand_mode = HEATING_DEMAND_MODE_OPERATIVE_COMFORT
    building_config.operative_heating_proportional_band_in_celsius = (
        DEFAULT_OPERATIVE_HEATING_PROPORTIONAL_BAND_IN_CELSIUS
    )
    building_config.operative_heating_proportional_gain = DEFAULT_OPERATIVE_HEATING_PROPORTIONAL_GAIN
    building_config.operative_heating_minimum_fraction_below_comfort_lower = (
        DEFAULT_OPERATIVE_HEATING_MINIMUM_FRACTION_BELOW_COMFORT_LOWER
    )
    building_config.use_strict_comfort_band_for_operative_heating = True
    building_config.operative_comfort_inner_offset_lower_in_celsius = (
        DEFAULT_OPERATIVE_COMFORT_INNER_OFFSET_LOWER_IN_CELSIUS
    )
    building_config.operative_comfort_inner_offset_upper_in_celsius = (
        DEFAULT_OPERATIVE_COMFORT_INNER_OFFSET_UPPER_IN_CELSIUS
    )
    # Do not gate building heating demand on 48 h outdoor mean: that cut shoulder-season /
    # preventive heat and made annual demand drop while comfort violations increased.
    # Seasonal on/off remains on GenericHeatPumpController in setups that wire it to plant logic.
    building_config.heating_disabled_above_running_mean_outdoor_temperature_in_celsius = None
    building_config.comfort_aware_seasonal_heating_gate = True


def apply_swiss_sia_natural_ventilation_settings(
    building_config: Any,
    arch_value: Optional[str] = None,
) -> None:
    """Enable Swiss CH building settings: ventilation + infiltration.

    Heating control for the CH batch variants is handled explicitly in the system setups
    (proportional operative-temperature undershoot vs comfort bounds).
    """
    building_config.natural_ventilation_m3_per_h_per_person = DEFAULT_NATURAL_VENTILATION_M3_PER_H_PER_PERSON
    building_config.enable_occupancy_driven_natural_ventilation = True
    building_config.sia_natural_ventilation_m3_per_h_per_m2 = DEFAULT_SIA_NATURAL_VENTILATION_M3_PER_H_PER_M2
    building_config.enable_sia_floor_area_natural_ventilation = True
    arch = arch_value or get_used_value("ARCH")
    if arch:
        apply_swiss_infiltration_settings(building_config, arch)


def apply_building_archetype_override(building_module: Any, arch_value: Optional[str]) -> Any:
    """Return a BuildingConfig for the requested archetype if available.

    Expects `arch_value` like "01_CH" and maps it to a function named
    `BuildingConfig.get_01_CH_single_family_home()` if it exists.
    """
    if not arch_value:
        raise ValueError("arch_value was empty.")
    fn_name = f"get_{arch_value}_single_family_home"
    fn = getattr(building_module.BuildingConfig, fn_name, None)
    if fn is None:
        raise AttributeError(fn_name)
    return fn()


def apply_weather_location_override(weather_module: Any, weather_value: Optional[str], name: str = "Weather", building_name: str = "BUI1") -> Any:
    """Return a WeatherConfig for the requested LocationEnum value if available."""
    import os

    from hisim import utils

    if not weather_value:
        raise ValueError("weather_value was empty.")
    validate_ch_batch_cli_configuration()
    loc = getattr(weather_module.LocationEnum, weather_value, None)
    if loc is None:
        raise AttributeError(weather_value)
    config = weather_module.WeatherConfig.get_default(location_entry=loc, name=name, building_name=building_name)
    if loc.name in CH_CUSTOM_CSV_WEATHER_LOCATIONS:
        filename = get_custom_csv_filename_for_location(loc.name)
        config.source_path = os.path.join(
            utils.get_input_directory(),
            "weather",
            loc.value[1],
            loc.value[2],
            filename,
        )
        log.information(
            f"Using custom CSV weather file {filename} for {loc.name} "
            f"(TIME_HORIZON={get_time_horizon()})."
        )
    return config

