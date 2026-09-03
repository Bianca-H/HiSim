"""Helpers for sizing and selecting heating systems.

This module provides:
- opt-in "ideal size lookup" (per ARCH×WEATHER/location) from a JSON table
- discrete product selection for heat pumps (from the smart devices database)

Setups can decide whether to use the default sizing logic or this lookup-based sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Any

from hisim import loadtypes as lt
from hisim import utils


@dataclass(frozen=True)
class HeatPumpProduct:
    """Discrete heat pump product from the smart devices database."""

    manufacturer: str
    name: str
    nominal_heating_power_in_watt: float


DEFAULT_IDEAL_SIZES_PATH = Path(utils.HISIMPATH["inputs"]) / "heating_system_ideal_sizes.json"


def load_ideal_sizes(path: Optional[Path] = None) -> Dict[str, Dict[str, float]]:
    """Load ideal sizes from JSON.

    Expected JSON format:
    {
      "unit": "kW",
      "data": {
        "01_CH": { "ZUESTA": 7.0, "BASSTA": 6.5, ... },
        "02_CH": { ... }
      }
    }

    Returns:
        Mapping arch -> mapping weather/location -> ideal_power_in_watt
    """

    path = path or DEFAULT_IDEAL_SIZES_PATH
    with path.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)
    unit = str(raw.get("unit") or "kW").strip()
    data = raw.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Ideal sizes file {path} is missing top-level 'data' dict.")

    out: Dict[str, Dict[str, float]] = {}
    for arch, by_loc in data.items():
        if not isinstance(by_loc, dict):
            continue
        arch_key = str(arch).strip()
        out[arch_key] = {}
        for loc, val in by_loc.items():
            fval = _as_float(val)
            if fval is None:
                continue
            loc_key = str(loc).strip()
            if unit.lower() == "kw":
                out[arch_key][loc_key] = fval * 1e3
            elif unit.lower() in ("w", "watt", "watts"):
                out[arch_key][loc_key] = fval
            else:
                raise ValueError(f"Unsupported unit '{unit}' in ideal sizes file {path}. Use 'kW' or 'W'.")
    return out


def get_ideal_power_from_lookup(*, arch: str, weather: str, path: Optional[Path] = None) -> float:
    """Get the ideal heat generator power (W) for a given ARCH×WEATHER."""

    sizes = load_ideal_sizes(path)
    if arch not in sizes or weather not in sizes[arch]:
        available_arch = ", ".join(sorted(sizes.keys()))
        available_weather = ", ".join(sorted(sizes.get(arch, {}).keys()))
        raise KeyError(
            "No ideal size found for "
            f"ARCH={arch} WEATHER={weather}. "
            f"Available ARCH in file: [{available_arch}]. "
            f"Available WEATHER for ARCH={arch}: [{available_weather}]."
        )
    power_w = float(sizes[arch][weather])
    if power_w <= 0:
        raise ValueError(
            f"Ideal size for ARCH={arch} WEATHER={weather} must be > 0, got {power_w}. "
            f"Please fill `{(path or DEFAULT_IDEAL_SIZES_PATH)}` with real values."
        )
    return power_w


# Fraction of discrete HP nominal used as max cooling plant power (split AC / district cooling).
# Matches HP setups: same IDEAL_LOOKUP + pick_heat_pump_closest_to_ideal nominal, then scale down
# because delivered HP cooling is typically well below catalogue/nominal (part load, cycling).
DEFAULT_COOLING_PLANT_NOMINAL_FRACTION = 0.20


def get_cooling_plant_cap_from_hp_nominal(
    nominal_heating_power_in_watt: float,
    nominal_fraction: float = DEFAULT_COOLING_PLANT_NOMINAL_FRACTION,
) -> float:
    """Max cooling plant power (W) from the same discrete HP nominal used in HP variants."""
    nominal_w = float(nominal_heating_power_in_watt)
    if nominal_w <= 0:
        raise ValueError(f"nominal_heating_power_in_watt must be > 0, got {nominal_w}.")
    fraction = float(nominal_fraction)
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"nominal_fraction must be in (0, 1], got {fraction}.")
    return nominal_w * fraction


def get_cooling_plant_cap_from_combined_heating_nominal(
    hp_nominal_heating_power_in_watt: float,
    supplemental_peak_heating_power_in_watt: float = 0.0,
    nominal_fraction: float = DEFAULT_COOLING_PLANT_NOMINAL_FRACTION,
) -> float:
    """Max cooling plant power (W) from combined HP + supplemental peak heating nominal (e.g. HP05 oil peak)."""
    combined_w = float(hp_nominal_heating_power_in_watt) + float(supplemental_peak_heating_power_in_watt)
    if combined_w <= 0:
        raise ValueError(f"combined heating nominal must be > 0, got {combined_w}.")
    return get_cooling_plant_cap_from_hp_nominal(combined_w, nominal_fraction=nominal_fraction)


# ComfortBandCoolingDemand tuning (Layer B): softer P-control so requests do not pin at cap.
DEFAULT_COOLING_COMFORT_PROPORTIONAL_GAIN_DIVISOR = 10.0
DEFAULT_COOLING_COMFORT_PROPORTIONAL_GAIN_FLOOR_W = 400.0
DEFAULT_COOLING_COMFORT_RELAXATION_FACTOR = 0.55

# Layer C: do not boost comfort P-control with full 5R1C theoretical cooling (pins plant at cap).
DEFAULT_COOLING_THEORETICAL_BLEND = "comfort_only"


def get_cooling_comfort_proportional_gain_w_per_k(
    cooling_cap_w: float,
    cap_divisor: float = DEFAULT_COOLING_COMFORT_PROPORTIONAL_GAIN_DIVISOR,
    floor_w: float = DEFAULT_COOLING_COMFORT_PROPORTIONAL_GAIN_FLOOR_W,
) -> float:
    """P-gain (W/K) for comfort-band cooling: full cap at ~divisor K above upper setpoint."""
    cap_w = float(cooling_cap_w)
    if cap_w <= 0:
        raise ValueError(f"cooling_cap_w must be > 0, got {cap_w}.")
    divisor = float(cap_divisor)
    if divisor <= 0:
        raise ValueError(f"cap_divisor must be > 0, got {divisor}.")
    return max(cap_w / divisor, float(floor_w))


def _as_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        # common "range" format in database, e.g. "2.9-4.4" -> use first number
        if isinstance(value, str) and "-" in value:
            maybe = value.split("-", 1)[0].strip()
            return float(maybe)
        # sometimes values are lists like [] or [3.5]
        if isinstance(value, (list, tuple)):
            if not value:
                return None
            return _as_float(value[0])
        return float(value)
    except Exception:
        return None


def _has_usable_cop_curve(hp: dict) -> bool:
    """Return True if the product has at least 2 numeric COP points."""

    cop = hp.get("COP")
    if not isinstance(cop, list):
        return False
    numeric_points = 0
    for entry in cop:
        if not isinstance(entry, dict) or not entry:
            continue
        val = list(entry.values())[0]
        if _as_float(val) is not None:
            numeric_points += 1
        if numeric_points >= 2:
            return True
    return False


def load_heat_pump_products() -> List[HeatPumpProduct]:
    """Load all heat pump products that have nominal heating power and usable COP data."""

    raw = utils.load_smart_appliance("Heat Pump")
    products: List[HeatPumpProduct] = []
    for hp in raw:
        manufacturer = str(hp.get("Manufacturer", "")).strip()
        name = str(hp.get("Name", "")).strip()
        power_kw = _as_float(hp.get("Nominal Heating Power A2/35"))
        if not manufacturer or not name or power_kw is None:
            continue
        # Avoid selecting products that cannot be simulated because COP data is missing/empty.
        if not _has_usable_cop_curve(hp):
            continue
        products.append(
            HeatPumpProduct(
                manufacturer=manufacturer,
                name=name,
                nominal_heating_power_in_watt=power_kw * 1e3,
            )
        )
    return products


def pick_closest_by_nominal_power(
    *,
    ideal_power_in_watt: float,
    candidates: Iterable[Tuple[str, float]],
    prefer_smaller_on_tie: bool = True,
) -> str:
    """Pick candidate with nominal power closest to ideal.

    Args:
        ideal_power_in_watt: Target size.
        candidates: Iterable of (id, nominal_power_in_watt).
        prefer_smaller_on_tie: If same absolute distance, prefer the smaller unit.

    Returns:
        The candidate id.
    """

    best_id: Optional[str] = None
    best_dist: Optional[float] = None
    best_power: Optional[float] = None

    for cid, power in candidates:
        dist = abs(float(power) - float(ideal_power_in_watt))
        if best_id is None:
            best_id, best_dist, best_power = str(cid), dist, float(power)
            continue
        assert best_dist is not None
        assert best_power is not None
        if dist < best_dist:
            best_id, best_dist, best_power = str(cid), dist, float(power)
        elif dist == best_dist:
            if prefer_smaller_on_tie and float(power) < best_power:
                best_id, best_dist, best_power = str(cid), dist, float(power)
            elif (not prefer_smaller_on_tie) and float(power) > best_power:
                best_id, best_dist, best_power = str(cid), dist, float(power)

    if best_id is None:
        raise ValueError("No candidates provided.")
    return best_id


def pick_size_up_with_small_down_tolerance(
    *,
    ideal_power_in_watt: float,
    candidates: Iterable[Tuple[str, float]],
    downsize_tolerance_in_watt: float = 100.0,
) -> str:
    """Pick a discrete unit by sizing up, with a small allowed downsize tolerance.

    Selection logic:
    - Prefer the smallest candidate with power >= ideal (size up).
    - Only pick the largest candidate below ideal (size down) if it is within
      `downsize_tolerance_in_watt` of the ideal.
    - If there is no size-up candidate, pick the largest below ideal.
    """

    ideal = float(ideal_power_in_watt)
    down_tol = float(downsize_tolerance_in_watt)

    up_id: Optional[str] = None
    up_power: Optional[float] = None
    down_id: Optional[str] = None
    down_power: Optional[float] = None

    for cid, power_raw in candidates:
        power = float(power_raw)
        if power >= ideal:
            if up_power is None or power < up_power:
                up_power = power
                up_id = str(cid)
        else:
            if down_power is None or power > down_power:
                down_power = power
                down_id = str(cid)

    if up_id is None and down_id is None:
        raise ValueError("No candidates provided.")

    # Downsize only if very close to ideal.
    if down_id is not None and down_power is not None:
        if (ideal - down_power) < down_tol:
            return down_id

    # Otherwise size up if possible, else fall back to largest smaller.
    if up_id is not None:
        return up_id
    assert down_id is not None
    return down_id


def pick_always_size_up_with_extra_if_close(
    *,
    ideal_power_in_watt: float,
    candidates: Iterable[Tuple[str, float]],
    extra_upsizing_if_within_watt: float = 100.0,
) -> str:
    """Pick a discrete unit by always sizing up.

    Selection logic:
    - Pick the smallest candidate with power >= ideal (size up).
    - If that chosen unit is within `extra_upsizing_if_within_watt` above the ideal,
      pick the next larger unit (one more size up), if available.
    - If there is no size-up candidate, pick the largest available candidate.
    """

    ideal = float(ideal_power_in_watt)
    close_w = float(extra_upsizing_if_within_watt)

    ordered: List[Tuple[float, str]] = []
    for cid, power_raw in candidates:
        ordered.append((float(power_raw), str(cid)))
    if not ordered:
        raise ValueError("No candidates provided.")
    ordered.sort(key=lambda x: x[0])

    up_index: Optional[int] = None
    for idx, (pwr, _) in enumerate(ordered):
        if pwr >= ideal:
            up_index = idx
            break

    if up_index is None:
        return ordered[-1][1]

    chosen_power, chosen_id = ordered[up_index]
    if (chosen_power - ideal) < close_w and (up_index + 1) < len(ordered):
        return ordered[up_index + 1][1]
    return chosen_id


def pick_heat_pump_closest_to_ideal(
    *,
    ideal_power_in_watt: float,
    products: Optional[List[HeatPumpProduct]] = None,
) -> HeatPumpProduct:
    """Pick a heat pump product for an ideal size.

    Always sizes up. If the first size-up is within 100 W above ideal, it picks
    one additional size step larger (if available).
    """

    products = products or load_heat_pump_products()
    if not products:
        raise ValueError("No heat pump products available in database.")

    chosen_key = pick_always_size_up_with_extra_if_close(
        ideal_power_in_watt=ideal_power_in_watt,
        candidates=[(f"{p.manufacturer}::{p.name}", p.nominal_heating_power_in_watt) for p in products],
        extra_upsizing_if_within_watt=100.0,
    )
    manufacturer, name = chosen_key.split("::", 1)
    for p in products:
        if p.manufacturer == manufacturer and p.name == name:
            return p
    raise RuntimeError("Chosen heat pump product not found after selection.")


# KPI labels (aligned with Building timestep degree-hour outputs).
KPI_OVERTEMPERATURE_DEGREE_HOURS_ABOVE_ADAPTIVE_UPPER = (
    "Overtemperature degree-hours above adaptive comfort upper bound (occupied timesteps)"
)
KPI_UNDERTEMPERATURE_DEGREE_HOURS_BELOW_ADAPTIVE_LOWER = (
    "Undertemperature degree-hours below adaptive comfort lower bound (occupied timesteps)"
)


@dataclass(frozen=True)
class ChComfortBandSpaceCoolingWiring:
    """Comfort-band space cooling demand + sign conversion for HDS (negative W = cooling)."""

    comfort_cooling_demand: Any
    signed_cooling_demand_negator: Any
    minus_one_scalar: Any


def create_ch_comfort_band_space_cooling_wiring(
    my_simulation_parameters: Any,
    cooling_cap_w: float,
    building_name: str = "BUI1",
) -> ChComfortBandSpaceCoolingWiring:
    """Create comfort-band cooling demand and multiply by -1 for signed HDS demand convention."""
    from hisim.components import comfort_band_cooling_demand, sumbuilder

    cooling_p_gain = get_cooling_comfort_proportional_gain_w_per_k(float(cooling_cap_w))
    comfort_cooling = comfort_band_cooling_demand.ComfortBandCoolingDemand(
        my_simulation_parameters=my_simulation_parameters,
        config=comfort_band_cooling_demand.ComfortBandCoolingDemandConfig.get_default_config(
            building_name=building_name,
            name="ComfortBandCoolingDemand",
            max_cooling_power_in_watt=float(cooling_cap_w),
            proportional_gain_in_watt_per_kelvin=cooling_p_gain,
            relaxation_factor=DEFAULT_COOLING_COMFORT_RELAXATION_FACTOR,
            theoretical_blend=DEFAULT_COOLING_THEORETICAL_BLEND,
        ),
    )
    minus_one_scalar = sumbuilder.ConstantThermalPowerOutput(
        my_simulation_parameters=my_simulation_parameters,
        config=sumbuilder.ConstantThermalPowerConfig(
            building_name=building_name,
            name="ComfortCoolingDemandSign",
            value_watt=-1.0,
            loadtype=lt.LoadTypes.ANY,
            unit=lt.Units.WATT,
        ),
    )
    signed_negator = sumbuilder.CalculateOperation(
        config=sumbuilder.SumBuilderConfig(
            building_name=building_name,
            name="SignedComfortCoolingDemand",
            loadtype=lt.LoadTypes.ANY,
            unit=lt.Units.WATT,
        ),
        my_simulation_parameters=my_simulation_parameters,
    )
    signed_negator.connect_arbitrary_input(
        comfort_cooling.component_name,
        comfort_cooling.CoolingDemand,
    )
    signed_negator.add_operation("Multiply")
    signed_negator.connect_arbitrary_input(
        minus_one_scalar.component_name,
        minus_one_scalar.SumOutput,
    )
    return ChComfortBandSpaceCoolingWiring(
        comfort_cooling_demand=comfort_cooling,
        signed_cooling_demand_negator=signed_negator,
        minus_one_scalar=minus_one_scalar,
    )

