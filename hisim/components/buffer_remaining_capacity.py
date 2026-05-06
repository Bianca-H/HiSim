"""Compute remaining thermal capacity of a buffer tank until a max temperature.

Provides a Wh-per-timestep style capacity headroom signal for FlexibilityPotential.
"""

from __future__ import annotations

from dataclasses import dataclass

from dataclasses_json import dataclass_json

from hisim import component as cp
from hisim import loadtypes as lt
from hisim.component import CapexCostDataClass, OpexCostDataClass
from hisim.simulationparameters import SimulationParameters


@dataclass_json
@dataclass
class BufferRemainingCapacityConfig(cp.ConfigBase):
    """Config."""

    building_name: str
    name: str
    volume_heating_water_storage_in_liter: float
    max_temperature_in_celsius: float = 60.0

    # constants (kept simple / transparent)
    water_density_kg_per_liter: float = 0.992
    water_specific_heat_capacity_j_per_kg_per_k: float = 4180.0

    @classmethod
    def get_main_classname(cls):
        return BufferRemainingCapacity.get_full_classname()

    @classmethod
    def get_default_config(
        cls,
        building_name: str = "BUI1",
        name: str = "BufferRemainingCapacity",
        volume_heating_water_storage_in_liter: float = 500.0,
        max_temperature_in_celsius: float = 60.0,
    ) -> "BufferRemainingCapacityConfig":
        return BufferRemainingCapacityConfig(
            building_name=building_name,
            name=name,
            volume_heating_water_storage_in_liter=float(volume_heating_water_storage_in_liter),
            max_temperature_in_celsius=float(max_temperature_in_celsius),
        )


class BufferRemainingCapacity(cp.Component):
    """Compute remaining storage capacity until Tmax (Wh)."""

    WaterMeanTemperatureInStorage = "WaterMeanTemperatureInStorage"
    BufferRemainingCapacity = "BufferRemainingCapacity"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: BufferRemainingCapacityConfig,
        my_display_config: cp.DisplayConfig = cp.DisplayConfig(),
    ) -> None:
        super().__init__(
            name=config.name,
            my_simulation_parameters=my_simulation_parameters,
            my_config=config,
            my_display_config=my_display_config,
        )
        self.my_simulation_parameters = my_simulation_parameters
        self.config = config

        self.t_mean_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.WaterMeanTemperatureInStorage,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )

        self.remaining_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.BufferRemainingCapacity,
            lt.LoadTypes.HEATING,
            lt.Units.WATT_HOUR,
            output_description="Remaining storable thermal energy in buffer until Tmax (Wh).",
        )

        self._mass_kg = float(self.config.volume_heating_water_storage_in_liter) * float(
            self.config.water_density_kg_per_liter
        )

    def i_prepare_simulation(self) -> None:
        pass

    def i_save_state(self) -> None:
        pass

    def i_restore_state(self) -> None:
        pass

    def i_doublecheck(self, timestep: int, stsv: cp.SingleTimeStepValues) -> None:  # noqa: ARG002
        pass

    def i_simulate(self, timestep: int, stsv: cp.SingleTimeStepValues, force_convergence: bool) -> None:  # noqa: ARG002
        t_mean = float(stsv.get_input_value(self.t_mean_channel))
        dt_k = max(0.0, float(self.config.max_temperature_in_celsius) - t_mean)
        e_j = self._mass_kg * float(self.config.water_specific_heat_capacity_j_per_kg_per_k) * dt_k
        e_wh = e_j / 3600.0
        stsv.set_output_value(self.remaining_out, e_wh)

    def get_cost_opex(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(config: cp.ConfigBase, simulation_parameters: SimulationParameters) -> CapexCostDataClass:  # noqa: ARG002
        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        return []

