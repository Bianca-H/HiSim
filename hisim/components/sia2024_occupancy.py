"""SIA 2024 schedule-based occupancy and internal loads.

This component is a lightweight alternative to the LPG/UTSP-based `UtspLpgConnector`.
It generates people / appliance / lighting schedules as percentages (hourly for one week)
and scales them by conditioned floor area.

Notes:
- The implementation is intentionally simple: electricity consumption is converted 1:1 to internal heat gains.
- DHW (water consumption) is set to 0 by default; extend if needed.
- Residential lighting can follow SIA **Nutzungsstunden** (intraday shape) and optional **t_L** annual
  full-load hours with **monthly** seasonality (see ``lighting_annual_full_load_hours``).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from dataclasses_json import dataclass_json

from hisim import component as cp
from hisim import loadtypes as lt
from hisim.component import OpexCostDataClass, CapexCostDataClass
from hisim.simulationparameters import SimulationParameters
from hisim.postprocessing.kpi_computation.kpi_structure import KpiEntry


def _validate_weekly_hourly_schedule(values: Sequence[float], name: str) -> List[float]:
    vals = [float(v) for v in values]
    if len(vals) != 168:
        raise ValueError(f"{name} must have 168 hourly values (one week). Got {len(vals)}.")
    return vals


def _hourly_to_timestep_schedule(hourly_values: Sequence[float], seconds_per_timestep: int, name: str) -> np.ndarray:
    """Expand 168 hourly values to simulation timestep resolution."""
    if 3600 % seconds_per_timestep != 0:
        raise ValueError(
            f"SIA 2024 schedules currently require seconds_per_timestep to divide 3600. "
            f"Got {seconds_per_timestep}."
        )
    hourly = np.array(_validate_weekly_hourly_schedule(hourly_values, name=name), dtype=float)
    steps_per_hour = int(3600 / seconds_per_timestep)
    expanded = np.repeat(hourly, steps_per_hour)
    # length = 168 * steps_per_hour
    return expanded


# SIA 2024 residential lighting: jährliche Volllaststunden ``t_L`` (example from standard: 846 h/a).
SIA2024_RESIDENTIAL_LIGHTING_ANNUAL_FULL_LOAD_H = 846.0
# SIA 2024 residential appliances: jährliche Volllaststunden ``t_A,Ps`` vs. daily ``t_A,d`` * 365 days.
# Example values from the standard: ``t_A,Ps`` = 1780 h, ``t_A,d`` = 6.1 h/d → ratio ≈ 0.7995.
SIA2024_RESIDENTIAL_APPLIANCES_YEARLY_UTILIZATION = 1780.0 / (6.1 * 365.0)


def _month_index_0_11_from_timestep(
    timestep: int, seconds_per_timestep: float, start_date: datetime.datetime
) -> int:
    instant = start_date + datetime.timedelta(seconds=float(timestep) * float(seconds_per_timestep))
    return int(instant.month) - 1


def _compute_lighting_month_scales(
    base_week_steps: np.ndarray,
    simulation_parameters: SimulationParameters,
    annual_full_load_hours: float,
    seasonal_amplitude: float,
) -> np.ndarray:
    """Return length-12 multipliers so ``base * k[month]`` hits ``annual_full_load_hours`` over the sim period.

    Monthly raw weights use ``1 + amp * cos(2*pi*m/12)`` (January ``m=0`` max, July ``m=6`` min) for a simple
    Northern-mid-latitude seasonality (more artificial lighting in darker months).
    """
    sp = simulation_parameters
    n_steps = int(sp.timesteps)
    dt_h = float(sp.seconds_per_timestep) / 3600.0
    amp = max(0.0, float(seasonal_amplitude))
    raw = np.ones(12, dtype=float)
    for m in range(12):
        raw[m] = 1.0 + amp * np.cos(2.0 * np.pi * float(m) / 12.0)
    raw = np.clip(raw, 0.01, None)

    duration_sec = float(n_steps) * float(sp.seconds_per_timestep)
    ref_year_sec = 365.0 * 24.0 * 3600.0
    target_equiv_h = float(annual_full_load_hours) * (duration_sec / ref_year_sec)

    n_base = int(len(base_week_steps))
    sum_bu = 0.0
    start = sp.start_date
    for t in range(n_steps):
        idx = t % n_base
        midx = _month_index_0_11_from_timestep(t, float(sp.seconds_per_timestep), start)
        sum_bu += float(base_week_steps[idx]) * float(raw[midx]) * dt_h

    if sum_bu <= 1e-18:
        raise ValueError("Lighting annual scaling: unweighted schedule integral is zero; check lighting profile.")

    gamma = target_equiv_h / sum_bu
    return raw * gamma


@dataclass_json
@dataclass
class SIA2024OccupancyConfig(cp.ConfigBase):
    """Config for SIA 2024 schedule-based occupancy."""

    building_name: str
    name: str

    # Scaling base
    conditioned_floor_area_in_m2: float

    # People
    people_per_m2: float
    sensible_heat_gain_per_person_in_watt: float
    people_schedule_weekly_hourly: List[float]  # 168 values, 0..1

    # Appliances
    appliances_load_w_per_m2: float
    appliances_schedule_weekly_hourly: List[float]  # 168 values, 0..1

    # Lighting
    lighting_load_w_per_m2: float
    #: 168 hourly fractions (0..1) for one week. If ``None``, lighting is on at nominal power whenever
    #: ``people_present > 0`` (used for non-residential defaults). Residential defaults use SIA
    #: Nutzungsstunden: 4.0 h equivalent in 7--18 h and 3.0 h equivalent in 18--7 h per day.
    lighting_schedule_weekly_hourly: Optional[List[float]] = None

    #: SIA-style annual utilisation of the **people** part of the schedule: applied every calendar month as a constant
    #: multiplier on ``people_frac`` from the weekly profile (0..1). Default ``0.8`` matches a common SIA assumption
    #: (~80 % presence vs nominal). Affects ``NumberOfResidents`` and ``HeatingByResidents``. When
    #: ``lighting_schedule_weekly_hourly`` is ``None``, lighting also follows ``people_present``; residential
    #: defaults use an explicit lighting schedule instead. Does **not** scale the appliance profile (see
    #: ``appliances_yearly_utilization_per_month``).

    person_yearly_utilization_per_month: float = 0.8

    #: SIA-style annual utilisation of the **appliance** weekly profile (0..1), applied uniformly every timestep
    #: (same idea as monthly presence reduction for people). Relates ``t_A,Ps`` to ``t_A,d`` * 365 when the weekly
    #: profile is normalised to ``t_A,d`` (e.g. residential default ``1780 / (6.1 * 365)``). Use ``1.0`` to disable.
    appliances_yearly_utilization_per_month: float = 1.0

    #: SIA ``t_L``: target nominal full-load hours of lighting over a **365 d** reference year. When set (e.g. 846)
    #: together with ``lighting_schedule_weekly_hourly``, the weekly intraday **shape** is scaled so the simulated
    #: period matches ``lighting_annual_full_load_hours * (sim_duration / (365*24h))``, with **monthly** seasonality
    #: (higher in winter, lower in summer). ``None`` = use raw hourly fractions only (no annual cap).
    lighting_annual_full_load_hours: Optional[float] = None
    #: Amplitude for ``1 + amp*cos(2*pi*m/12)`` monthly weights, ``m=0`` January (0 = uniform across months).
    lighting_seasonal_variation_amplitude: float = 0.35

    @staticmethod
    def lighting_schedule_weekly_sia_residential_nutzungsstunden() -> List[float]:
        """SIA 2024 residential lighting: equivalent use hours per day in each band.

        Day **7--18 h** (11 clock hours): **4.0** Nutzungsstunden at nominal lighting power spread uniformly
        → fraction ``4/11`` each hour in that band. Night **18--7 h** (13 hours): **3.0** h equivalent
        → fraction ``3/13`` each hour. Same daily pattern for all seven weekdays (hour 0 = Monday 00:00).
        """
        hourly: List[float] = []
        for _ in range(7):
            for hod in range(24):
                if 7 <= hod < 18:
                    hourly.append(4.0 / 11.0)
                else:
                    hourly.append(3.0 / 13.0)
        return hourly

    @classmethod
    def get_main_classname(cls):
        return SIA2024Occupancy.get_full_classname()

    @staticmethod
    def get_default_weekly_people_schedules_by_use_type() -> Dict[str, List[float]]:
        """Return placeholder 168h schedules per use type (0..1).

        Replace these with SIA 2024 schedules for your use types.
        Keys are matched case-insensitively in `get_default_for_use_type`.
        """

        # NOTE: These are placeholders (flat 1.0) to make the setup runnable until filled.
        weekly_flat = [1.0] * 168
        return {
            "residential": [1.0,1.0,1.0,1.0,1.0,1.0,0.6,0.4,0.0,0.0,0.0,0.0,0.8,0.4,0.0,0.0,0.0,0.4,0.8,0.8,0.8,1.0,1.0,1.0]*7,
            "residential_sfh": [1.0,1.0,1.0,1.0,1.0,1.0,0.6,0.4,0.0,0.0,0.0,0.0,0.8,0.4,0.0,0.0,0.0,0.4,0.8,0.8,0.8,1.0,1.0,1.0]*7,
            "residential_mfh": weekly_flat,
            "office": weekly_flat,
            "school": weekly_flat,
            "retail": weekly_flat,
        }

    @staticmethod
    def get_default_weekly_appliances_schedules_by_use_type() -> Dict[str, List[float]]:
        """Return placeholder 168h schedules per use type (0..1)."""

        weekly_flat = [1.0] * 168
        return {
            "residential": [0.1,0.1,0.1,0.1,0.1,0.2,0.8,0.2,0.1,0.1,0.1,0.1,0.8,0.2,0.1,0.1,0.1,0.2,0.8,1.0,0.2,0.2,0.2,0.1]*7,
            "residential_sfh": [0.1,0.1,0.1,0.1,0.1,0.2,0.8,0.2,0.1,0.1,0.1,0.1,0.8,0.2,0.1,0.1,0.1,0.2,0.8,1.0,0.2,0.2,0.2,0.1]*7,
            "residential_mfh": weekly_flat,
            "office": weekly_flat,
            "school": weekly_flat,
            "retail": weekly_flat,
        }

    @classmethod
    def get_default_for_use_type(
        cls,
        conditioned_floor_area_in_m2: float,
        use_type: str = "residential",
        building_name: str = "BUI1",
        name: str = "SIA2024Occupancy",
    ) -> "SIA2024OccupancyConfig":
        """Default values are placeholders; adjust to SIA 2024 tables for your use type."""

        use_type_norm = use_type.strip().lower()

        lighting_sched: Optional[List[float]] = None
        appliance_ann_util = 1.0
        lighting_flh_target: Optional[float] = None
        if use_type_norm in {"residential", "residential_sfh", "residential_mfh", "housing", "dwelling"}:
            people_per_m2 = 0.02  # 2 persons / 100 m2
            appliances_load_w_per_m2 = 10.0  # "Standard" mode from SIA 2024 as some "Bestand" but also some renovated
            lighting_load_w_per_m2 = 3.0  # "Standard" mode from SIA 2024 as some "Bestand" but also some renovated
            lighting_sched = cls.lighting_schedule_weekly_sia_residential_nutzungsstunden()
            appliance_ann_util = SIA2024_RESIDENTIAL_APPLIANCES_YEARLY_UTILIZATION
            lighting_flh_target = SIA2024_RESIDENTIAL_LIGHTING_ANNUAL_FULL_LOAD_H
        elif use_type_norm in {"office"}:
            people_per_m2 = 0.05  # 5 persons / 100 m2
            appliances_load_w_per_m2 = 8.0
            lighting_load_w_per_m2 = 7.0
        else:
            people_per_m2 = 0.02
            appliances_load_w_per_m2 = 3.0
            lighting_load_w_per_m2 = 2.0

        people_schedules = cls.get_default_weekly_people_schedules_by_use_type()
        appliances_schedules = cls.get_default_weekly_appliances_schedules_by_use_type()
        # Fallback order: exact key, then "residential"
        people_weekly = people_schedules.get(use_type_norm, people_schedules["residential"])
        appliances_weekly = appliances_schedules.get(use_type_norm, appliances_schedules["residential"])

        return cls(
            building_name=building_name,
            name=name,
            conditioned_floor_area_in_m2=float(conditioned_floor_area_in_m2),
            people_per_m2=float(people_per_m2),
            sensible_heat_gain_per_person_in_watt=89.0,
            people_schedule_weekly_hourly=list(people_weekly),
            appliances_load_w_per_m2=float(appliances_load_w_per_m2),
            appliances_schedule_weekly_hourly=list(appliances_weekly),
            lighting_load_w_per_m2=float(lighting_load_w_per_m2),
            lighting_schedule_weekly_hourly=lighting_sched,
            person_yearly_utilization_per_month=0.8,
            appliances_yearly_utilization_per_month=appliance_ann_util,
            lighting_annual_full_load_hours=lighting_flh_target,
        )


class SIA2024Occupancy(cp.Component):
    """Generates internal gains and electricity schedules based on SIA 2024 style inputs."""

    # Outputs (match names used by Building default connections conceptually)
    NumberOfResidents = "NumberOfResidents"
    HeatingByResidents = "HeatingByResidents"
    HeatingByDevices = "HeatingByDevices"
    ElectricalPowerConsumption = "ElectricalPowerConsumption"
    WaterConsumption = "WaterConsumption"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: SIA2024OccupancyConfig,
        my_display_config: cp.DisplayConfig = cp.DisplayConfig(),
    ) -> None:
        self.my_simulation_parameters = my_simulation_parameters
        self.config = config
        component_name = self.get_component_name()
        super().__init__(
            name=component_name,
            my_simulation_parameters=my_simulation_parameters,
            my_config=config,
            my_display_config=my_display_config,
        )

        # Outputs
        self.number_of_residents_output = self.add_output(
            object_name=self.component_name,
            field_name=self.NumberOfResidents,
            load_type=lt.LoadTypes.ANY,
            unit=lt.Units.ANY,
            output_description="SIA 2024 derived number of residents (can be fractional).",
        )
        self.heating_by_residents_output = self.add_output(
            object_name=self.component_name,
            field_name=self.HeatingByResidents,
            load_type=lt.LoadTypes.HEATING,
            unit=lt.Units.WATT,
            output_description="Internal sensible heat gains from occupants.",
        )
        self.heating_by_devices_output = self.add_output(
            object_name=self.component_name,
            field_name=self.HeatingByDevices,
            load_type=lt.LoadTypes.HEATING,
            unit=lt.Units.WATT,
            output_description="Internal heat gains from appliances + lighting (assumed equal to electric power).",
        )
        self.electrical_power_consumption_output = self.add_output(
            object_name=self.component_name,
            field_name=self.ElectricalPowerConsumption,
            load_type=lt.LoadTypes.ELECTRICITY,
            unit=lt.Units.WATT,
            postprocessing_flag=[lt.InandOutputType.ELECTRICITY_CONSUMPTION_UNCONTROLLED],
            output_description="Electric power demand from appliances + lighting.",
        )
        self.water_consumption_output = self.add_output(
            object_name=self.component_name,
            field_name=self.WaterConsumption,
            load_type=lt.LoadTypes.VOLUME,
            unit=lt.Units.LITER,
            output_description="DHW draw (not modeled here; default 0).",
        )

        # Precompute schedules at timestep resolution (one week repeated)
        self._people_schedule_steps = _hourly_to_timestep_schedule(
            config.people_schedule_weekly_hourly, my_simulation_parameters.seconds_per_timestep, "people_schedule_weekly_hourly"
        )
        self._appliances_schedule_steps = _hourly_to_timestep_schedule(
            config.appliances_schedule_weekly_hourly, my_simulation_parameters.seconds_per_timestep, "appliances_schedule_weekly_hourly"
        )
        self._lighting_schedule_steps: Optional[np.ndarray] = None
        self._lighting_base_steps: Optional[np.ndarray] = None
        self._lighting_month_scale: Optional[np.ndarray] = None

        if config.lighting_schedule_weekly_hourly is not None:
            base_steps = _hourly_to_timestep_schedule(
                config.lighting_schedule_weekly_hourly,
                my_simulation_parameters.seconds_per_timestep,
                "lighting_schedule_weekly_hourly",
            )
            ann_flh = getattr(config, "lighting_annual_full_load_hours", None)
            if ann_flh is not None and float(ann_flh) > 0.0:
                amp = float(getattr(config, "lighting_seasonal_variation_amplitude", 0.35) or 0.0)
                self._lighting_base_steps = base_steps
                self._lighting_month_scale = _compute_lighting_month_scales(
                    base_steps,
                    my_simulation_parameters,
                    float(ann_flh),
                    amp,
                )
                if len(base_steps) != len(self._people_schedule_steps):
                    raise ValueError(
                        "Lighting weekly profile length mismatch vs people schedule; use the same seconds_per_timestep."
                    )
            else:
                self._lighting_schedule_steps = base_steps
        else:
            self._lighting_schedule_steps = None

        self._num_people = float(config.conditioned_floor_area_in_m2) * float(config.people_per_m2)

    def i_prepare_simulation(self) -> None:
        pass

    def i_save_state(self) -> None:
        pass

    def i_restore_state(self) -> None:
        pass

    def i_doublecheck(self, timestep: int, stsv: cp.SingleTimeStepValues) -> None:
        pass

    def i_simulate(self, timestep: int, stsv: cp.SingleTimeStepValues, force_convergence: bool) -> None:
        idx = int(timestep % len(self._people_schedule_steps))
        people_frac = float(np.clip(self._people_schedule_steps[idx], 0.0, 1.0))
        appl_frac = float(np.clip(self._appliances_schedule_steps[idx], 0.0, 1.0))

        person_util = float(getattr(self.config, "person_yearly_utilization_per_month", 0.8) or 0.0)
        person_util = max(0.0, min(person_util, 1.0))
        # SIA: same yearly utilisation factor applied in every month (scales effective people vs weekly profile).
        people_present = self._num_people * people_frac * person_util

        appliance_util = float(getattr(self.config, "appliances_yearly_utilization_per_month", 1.0) or 0.0)
        appliance_util = max(0.0, min(appliance_util, 1.0))
        if self._lighting_base_steps is not None and self._lighting_month_scale is not None:
            midx = _month_index_0_11_from_timestep(
                timestep, float(self.my_simulation_parameters.seconds_per_timestep), self.my_simulation_parameters.start_date
            )
            light_frac = float(
                np.clip(
                    float(self._lighting_base_steps[idx]) * float(self._lighting_month_scale[midx]),
                    0.0,
                    1.0,
                )
            )
        elif self._lighting_schedule_steps is not None:
            light_frac = float(np.clip(self._lighting_schedule_steps[idx], 0.0, 1.0))
        else:
            # Non-residential default: full nominal lighting power whenever someone is effectively present.
            light_frac = 1.0 if people_present > 0.0 else 0.0

        heating_by_residents_w = people_present * float(self.config.sensible_heat_gain_per_person_in_watt)

        appliances_w = (
            float(self.config.conditioned_floor_area_in_m2)
            * float(self.config.appliances_load_w_per_m2)
            * appl_frac
            * appliance_util
        )
        lighting_w = float(self.config.conditioned_floor_area_in_m2) * float(self.config.lighting_load_w_per_m2) * light_frac
        electrical_w = appliances_w + lighting_w

        heating_by_devices_w = electrical_w  # assume all electric ends as internal heat

        stsv.set_output_value(self.number_of_residents_output, people_present)
        stsv.set_output_value(self.heating_by_residents_output, heating_by_residents_w)
        stsv.set_output_value(self.heating_by_devices_output, heating_by_devices_w)
        stsv.set_output_value(self.electrical_power_consumption_output, electrical_w)
        stsv.set_output_value(self.water_consumption_output, 0.0)

    def get_cost_opex(
        self,
        all_outputs: List,
        postprocessing_results,  # pd.DataFrame (kept generic to avoid extra dependency here)
    ) -> OpexCostDataClass:
        """No direct OPEX: this component only provides schedules/loads.

        Electricity costs/emissions are accounted for by the meter components in postprocessing.
        """

        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(config: SIA2024OccupancyConfig, simulation_parameters: SimulationParameters) -> CapexCostDataClass:  # pylint: disable=unused-argument
        """No CAPEX by default."""

        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(
        self,
        all_outputs: List,  # pylint: disable=unused-argument
        postprocessing_results,  # pd.DataFrame (kept generic)
    ) -> List[KpiEntry]:
        """No KPIs for this schedule component (meters/building provide KPIs)."""

        return []

