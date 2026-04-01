"""Helpers for passing command-line overrides into system setups.

This module provides a tiny in-process "override registry" that `hisim_main.py`
can populate from CLI args (e.g. ARCH=..., WEATHER=...), and system setups can
optionally consume to override configuration defaults.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

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
    if not weather_value:
        raise ValueError("weather_value was empty.")
    loc = getattr(weather_module.LocationEnum, weather_value, None)
    if loc is None:
        raise AttributeError(weather_value)
    return weather_module.WeatherConfig.get_default(location_entry=loc, name=name, building_name=building_name)

