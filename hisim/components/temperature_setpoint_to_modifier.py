"""Convert a target temperature setpoint into a building temperature modifier (°C).

The Building component uses:
  effective_heating_setpoint = building_config.set_heating_temperature_in_celsius + BuildingTemperatureModifier

This helper lets us drive the modifier so that the effective setpoint matches a
desired target setpoint provided by another controller (e.g., strict comfort bounds).
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
class TemperatureSetpointToModifierConfig(cp.ConfigBase):
    """Config."""

    building_name: str
    name: str
    base_heating_setpoint_in_celsius: float = 20.5
    clamp_min_modifier_in_celsius: float = 0.0
    clamp_max_modifier_in_celsius: float = 10.0

    @classmethod
    def get_main_classname(cls):
        return TemperatureSetpointToModifier.get_full_classname()

    @classmethod
    def get_default_config(
        cls,
        building_name: str = "BUI1",
        name: str = "TemperatureSetpointToModifier",
        base_heating_setpoint_in_celsius: float = 20.5,
    ) -> "TemperatureSetpointToModifierConfig":
        return TemperatureSetpointToModifierConfig(
            building_name=building_name,
            name=name,
            base_heating_setpoint_in_celsius=base_heating_setpoint_in_celsius,
        )


class TemperatureSetpointToModifier(cp.Component):
    """Compute modifier = target_setpoint - base_setpoint (clamped)."""

    TargetSetpointInCelsius = "TargetSetpointInCelsius"
    TemperatureModifier = "TemperatureModifier"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: TemperatureSetpointToModifierConfig,
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

        self.target_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.TargetSetpointInCelsius,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )

        self.modifier_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.TemperatureModifier,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            output_description="Building temperature modifier (°C) derived from a target heating setpoint.",
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
        target = float(stsv.get_input_value(self.target_channel))
        base = float(self.config.base_heating_setpoint_in_celsius)
        mod = target - base
        mod = max(float(self.config.clamp_min_modifier_in_celsius), mod)
        mod = min(float(self.config.clamp_max_modifier_in_celsius), mod)
        stsv.set_output_value(self.modifier_out, mod)

    def get_cost_opex(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(config: cp.ConfigBase, simulation_parameters: SimulationParameters) -> CapexCostDataClass:  # noqa: ARG002
        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        return []

