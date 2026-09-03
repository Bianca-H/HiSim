"""Collect resolved simulation inputs for Swiss CH batch documentation.

Runs each system setup's ``setup_function`` (no time-step simulation) and extracts
curated parameters from component configs — envelope, internal loads, plant sizing, etc.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from hisim import cli_overrides
from hisim.simulator import Simulator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYSTEM_SETUPS_DIR = REPO_ROOT / "system_setups"

# Documented CH batch energy-system modules (same set as batch_run.SQ50.matrix.json).
DEFAULT_ENERGY_SYSTEMS: Tuple[str, ...] = (
    "hp01",
    "hp02",
    "hp03",
    "hp04",
    "hp05",
    "bo01",
    "bo02",
    "bo03",
    "bo04",
    "bg01",
    "bp01",
    "gr01",
    "gr02",
)

DEFAULT_ARCHETYPES: Tuple[str, ...] = tuple(f"{i:02d}_CH" for i in range(1, 10))

BUILDING_PARAMETER_KEYS: Tuple[str, ...] = (
    "building_name",
    "building_code",
    "building_heat_capacity_class",
    "absolute_conditioned_floor_area_in_m2",
    "heating_reference_temperature_in_celsius",
    "floor_u_value_in_watt_per_m2_per_kelvin",
    "floor_area_in_m2",
    "facade_u_value_in_watt_per_m2_per_kelvin",
    "facade_area_in_m2",
    "roof_u_value_in_watt_per_m2_per_kelvin",
    "roof_area_in_m2",
    "window_u_value_in_watt_per_m2_per_kelvin",
    "window_area_in_m2",
    "door_u_value_in_watt_per_m2_per_kelvin",
    "door_area_in_m2",
    "set_heating_temperature_in_celsius",
    "set_cooling_temperature_in_celsius",
    "natural_ventilation_m3_per_h_per_person",
    "sia_natural_ventilation_m3_per_h_per_m2",
    "swiss_infiltration_rate_per_h",
    "heating_demand_mode",
    "operative_heating_proportional_band_in_celsius",
    "operative_heating_proportional_gain",
    "control_comfort_lower_bound_shift_in_celsius",
    "comfort_aware_seasonal_heating_gate",
)

OCCUPANCY_PARAMETER_KEYS: Tuple[str, ...] = (
    "use_type",
    "conditioned_floor_area_in_m2",
    "appliances_load_w_per_m2",
    "lighting_load_w_per_m2",
    "lighting_annual_full_load_hours",
    "appliances_yearly_utilization_per_month",
    "people_present_schedule_scaling",
)

WEATHER_PARAMETER_KEYS: Tuple[str, ...] = ("source_path", "location", "name")

HEAT_PUMP_KEYS: Tuple[str, ...] = (
    "set_thermal_output_power_in_watt",
    "flow_temperature_in_celsius",
    "heating_reference_temperature_in_celsius",
    "with_domestic_hot_water_preparation",
    "minimum_running_time_in_seconds",
    "minimum_idle_time_in_seconds",
    "group_id",
)

BOILER_KEYS: Tuple[str, ...] = (
    "minimal_thermal_power_in_watt",
    "maximal_thermal_power_in_watt",
    "eff_th_min",
    "eff_th_max",
    "boiler_type",
    "energy_carrier",
)

DISTRICT_HEATING_KEYS: Tuple[str, ...] = (
    "connected_load_w",
)

STORAGE_SH_KEYS: Tuple[str, ...] = (
    "volume_heating_water_storage_in_liter",
    "water_mean_temperature_in_celsius",
)

STORAGE_DHW_KEYS: Tuple[str, ...] = (
    "volume_dhw_storage_in_liter",
    "water_mean_temperature_in_celsius",
)

HDS_KEYS: Tuple[str, ...] = (
    "heat_distribution_system_type",
    "absolute_conditioned_floor_area_in_m2",
)

PV_KEYS: Tuple[str, ...] = ("power_in_watt", "tilt", "azimuth", "location")

BATTERY_KEYS: Tuple[str, ...] = (
    "battery_capacity_in_kwh",
    "max_charge_power_in_watt",
    "max_discharge_power_in_watt",
)

CLI_USED_KEYS: Tuple[str, ...] = (
    "ARCH",
    "WEATHER",
    "TIME_HORIZON",
    "SCENARIO",
    "OCC",
    "HEATGEN_SIZING",
    "HP_MODEL",
    "HP_NOMINAL_POWER_W",
    "HP_SHARE_OF_IDEAL",
)

COMPONENT_FIELD_MAP: Dict[str, Tuple[str, ...]] = {
    "Building": BUILDING_PARAMETER_KEYS,
    "SIA2024Occupancy": OCCUPANCY_PARAMETER_KEYS,
    "Weather": WEATHER_PARAMETER_KEYS,
    "HeatPumpHPLib": HEAT_PUMP_KEYS,
    "OilBoiler": BOILER_KEYS,
    "GasBoiler": BOILER_KEYS,
    "PelletBoiler": BOILER_KEYS,
    "DistrictHeating": DISTRICT_HEATING_KEYS,
    "SimpleHotWaterStorage": STORAGE_SH_KEYS,
    "DHWStorage": STORAGE_DHW_KEYS,
    "HeatDistributionSystem": HDS_KEYS,
    "PVSystem": PV_KEYS,
    "Battery": BATTERY_KEYS,
}


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if abs(value) >= 1000 or (abs(value) > 0 and abs(value) < 0.001):
            return f"{value:.6g}"
        return f"{value:.4g}".rstrip("0").rstrip(".")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _pick_fields(config: Any, keys: Iterable[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in keys:
        if hasattr(config, key):
            out[key] = getattr(config, key)
    return out


def _scalar_fallback_fields(config: Any) -> Dict[str, Any]:
    """Scalars from config for components without an explicit field list."""
    if hasattr(config, "to_dict"):
        raw = config.to_dict()
    elif is_dataclass(config):
        raw = {f.name: getattr(config, f.name) for f in fields(config)}
    else:
        return {}
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, (list, tuple, dict)):
            continue
        if value is None:
            continue
        out[key] = value
    return out


def _simulation_run_rows(sim: Simulator) -> List[Dict[str, str]]:
    params = sim._simulation_parameters  # noqa: SLF001 — documentation collector only
    rows: List[Dict[str, str]] = []
    if params is None:
        return rows
    rows.append(
        {
            "section": "simulation",
            "component": "SimulationParameters",
            "parameter": "year",
            "value": _format_value(params.year),
            "unit": "",
        }
    )
    rows.append(
        {
            "section": "simulation",
            "component": "SimulationParameters",
            "parameter": "seconds_per_timestep",
            "value": _format_value(params.seconds_per_timestep),
            "unit": "s",
        }
    )
    rows.append(
        {
            "section": "simulation",
            "component": "SimulationParameters",
            "parameter": "country",
            "value": _format_value(getattr(params, "country", "")),
            "unit": "",
        }
    )
    return rows


def _cli_used_rows() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for key in CLI_USED_KEYS:
        value = cli_overrides.get_used_value(key)
        if value is None:
            value = cli_overrides.get_override(key)
        if value is None:
            continue
        rows.append(
            {
                "section": "cli",
                "component": "cli_overrides",
                "parameter": key,
                "value": _format_value(value),
                "unit": "",
            }
        )
    return rows


def extract_documentation_rows(sim: Simulator) -> List[Dict[str, str]]:
    """Return long-format parameter rows from a built (not simulated) setup."""
    rows: List[Dict[str, str]] = []
    rows.extend(_simulation_run_rows(sim))
    rows.extend(_cli_used_rows())

    for component_name, config in sim.config_dictionary.items():
        key_list = COMPONENT_FIELD_MAP.get(component_name)
        if key_list:
            picked = _pick_fields(config, key_list)
        else:
            picked = _scalar_fallback_fields(config)
        if not picked:
            continue
        section = "component"
        if component_name == "Building":
            section = "building"
        elif component_name == "SIA2024Occupancy":
            section = "internal_loads"
        elif component_name in ("HeatPumpHPLib", "OilBoiler", "GasBoiler", "PelletBoiler", "DistrictHeating"):
            section = "heat_generator"
        elif component_name in ("SimpleHotWaterStorage", "DHWStorage"):
            section = "thermal_storage"
        elif component_name == "HeatDistributionSystem":
            section = "heat_distribution"
        elif component_name == "Weather":
            section = "weather"
        elif component_name == "PVSystem":
            section = "pv"
        elif component_name == "Battery":
            section = "battery"

        for param, value in picked.items():
            rows.append(
                {
                    "section": section,
                    "component": component_name,
                    "parameter": param,
                    "value": _format_value(value),
                    "unit": "",
                }
            )
    return rows


def build_system_setup(
    energy_system: str,
    *,
    archetype: str,
    weather: str,
    extra_cli_overrides: Optional[Dict[str, str]] = None,
    system_setups_dir: Path = DEFAULT_SYSTEM_SETUPS_DIR,
) -> Simulator:
    """Import a CH setup module and run ``setup_function`` without simulating."""
    overrides: Dict[str, str] = {
        "ARCH": archetype,
        "WEATHER": weather,
        "BATCH_OPEN_EXPLORER": "0",
    }
    if extra_cli_overrides:
        overrides.update({str(k).upper(): str(v) for k, v in extra_cli_overrides.items()})
    cli_overrides.set_overrides(overrides)

    setups_path = str(system_setups_dir.resolve())
    if setups_path not in sys.path:
        sys.path.insert(0, setups_path)

    module = importlib.import_module(energy_system)
    sim = Simulator(
        module_directory=setups_path,
        module_filename=energy_system,
        my_simulation_parameters=None,
    )
    module.setup_function(sim, None)
    return sim


def collect_all_cases(
    *,
    archetypes: Sequence[str],
    energy_systems: Sequence[str],
    weather_locations: Sequence[str],
    cli_overrides_config: Optional[Dict[str, str]] = None,
    system_setups_dir: Path = DEFAULT_SYSTEM_SETUPS_DIR,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Collect documentation rows for the cartesian product of inputs.

    Returns:
        (parameter_rows, error_rows)
    """
    parameter_rows: List[Dict[str, str]] = []
    error_rows: List[Dict[str, str]] = []
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    total = len(energy_systems) * len(archetypes) * len(weather_locations)
    case_index = 0

    for energy_system in energy_systems:
        for archetype in archetypes:
            for weather in weather_locations:
                case_index += 1
                case_id = f"{energy_system}_{archetype}_{weather}"
                print(f"  [{case_index}/{total}] {case_id} …", flush=True)
                try:
                    sim = build_system_setup(
                        energy_system,
                        archetype=archetype,
                        weather=weather,
                        extra_cli_overrides=cli_overrides_config,
                        system_setups_dir=system_setups_dir,
                    )
                    doc_rows = extract_documentation_rows(sim)
                except Exception as exc:  # noqa: BLE001 — collect and continue
                    error_rows.append(
                        {
                            "case_id": case_id,
                            "energy_system": energy_system,
                            "archetype": archetype,
                            "weather": weather,
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                        }
                    )
                    continue

                for row in doc_rows:
                    parameter_rows.append(
                        {
                            "generated_at": generated_at,
                            "case_id": case_id,
                            "energy_system": energy_system,
                            "archetype": archetype,
                            "weather": weather,
                            **row,
                        }
                    )

    return parameter_rows, error_rows


def write_excel_document(
    path: Path,
    parameter_rows: List[Dict[str, str]],
    error_rows: List[Dict[str, str]],
    metadata: Dict[str, Any],
) -> None:
    """Write parameter table and metadata to an Excel workbook."""
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        meta_df = pd.DataFrame([{"key": k, "value": str(v)} for k, v in metadata.items()])
        meta_df.to_excel(writer, sheet_name="metadata", index=False)
        if parameter_rows:
            pd.DataFrame(parameter_rows).to_excel(writer, sheet_name="parameters", index=False)
        if error_rows:
            pd.DataFrame(error_rows).to_excel(writer, sheet_name="errors", index=False)


def write_markdown_summary(
    path: Path,
    parameter_rows: List[Dict[str, str]],
    metadata: Dict[str, Any],
) -> None:
    """Write a readable Markdown summary (building + sizing highlights per case)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        "# Swiss CH simulation documentation",
        "",
        f"Generated: {metadata.get('generated_at', '')}",
        "",
        "## Input scope",
        "",
        f"- Archetypes: {metadata.get('archetypes')}",
        f"- Energy systems: {metadata.get('energy_systems')}",
        f"- Weather locations: {metadata.get('weather_locations')}",
        f"- CLI overrides: `{metadata.get('cli_overrides')}`",
        "",
        "Full parameter tables are in the companion Excel file (`parameters` sheet).",
        "",
    ]

    if not parameter_rows:
        lines.append("_No cases succeeded._")
        path.write_text("\n".join(lines), encoding="utf-8")
        return

    highlight_params = {
        "building": [
            "building_name",
            "absolute_conditioned_floor_area_in_m2",
            "facade_u_value_in_watt_per_m2_per_kelvin",
            "window_u_value_in_watt_per_m2_per_kelvin",
            "swiss_infiltration_rate_per_h",
        ],
        "internal_loads": ["appliances_load_w_per_m2", "lighting_load_w_per_m2"],
        "heat_generator": [
            "set_thermal_output_power_in_watt",
            "maximal_thermal_power_in_watt",
            "connected_load_w",
        ],
        "cli": ["HP_MODEL", "HP_NOMINAL_POWER_W"],
    }

    case_ids = sorted({r["case_id"] for r in parameter_rows})
    for case_id in case_ids:
        case_rows = [r for r in parameter_rows if r["case_id"] == case_id]
        if not case_rows:
            continue
        header = case_rows[0]
        lines.append(f"## {case_id}")
        lines.append("")
        lines.append(
            f"Setup `{header['energy_system']}`, archetype `{header['archetype']}`, weather `{header['weather']}`."
        )
        lines.append("")
        for section, params in highlight_params.items():
            section_rows = [r for r in case_rows if r["section"] == section and r["parameter"] in params]
            if not section_rows:
                continue
            lines.append(f"### {section}")
            lines.append("")
            lines.append("| Parameter | Value |")
            lines.append("| --- | --- |")
            for r in section_rows:
                lines.append(f"| {r['parameter']} | {r['value']} |")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
