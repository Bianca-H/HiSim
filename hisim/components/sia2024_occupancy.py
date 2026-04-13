"""SIA 2024 schedule-based occupancy and internal loads.

This component is a lightweight alternative to the LPG/UTSP-based `UtspLpgConnector`.
It generates people / appliance / lighting schedules as percentages (hourly for one week)
and scales them by conditioned floor area.

Notes:
- The implementation is intentionally simple: electricity consumption is converted 1:1 to internal heat gains.
- DHW (water consumption) is set to 0 by default; extend if needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

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
    # "operational": 1 if any people present, else 0 (derived)

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


        if use_type_norm in {"residential", "residential_sfh", "residential_mfh", "housing", "dwelling"}:
            people_per_m2 = 0.02  # 2 persons / 100 m2
            appliances_load_w_per_m2 = 10.0  # "Standard" mode from SIA 2024 as some "Bestand" but also some renovated
            lighting_load_w_per_m2 = 3.0  # "Standard" mode from SIA 2024 as some "Bestand" but also some renovated
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

        # Lighting is "operational": 1 if any people present, else 0
        light_frac = 1.0 if people_frac > 0.0 else 0.0

        people_present = self._num_people * people_frac
        heating_by_residents_w = people_present * float(self.config.sensible_heat_gain_per_person_in_watt)

        appliances_w = float(self.config.conditioned_floor_area_in_m2) * float(self.config.appliances_load_w_per_m2) * appl_frac
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

