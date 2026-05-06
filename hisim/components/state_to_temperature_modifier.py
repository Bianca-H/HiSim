"""Convert a controller state into a temperature modifier (°C).

Typical use: apply a positive modifier to a building's heating setpoint whenever a
strict comfort controller requests heating. This makes the building compute a
non-zero theoretical heating demand earlier (within the comfort band), while the
rest of the system remains demand-driven.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dataclasses_json import dataclass_json

from hisim import component as cp
from hisim import loadtypes as lt
from hisim.component import CapexCostDataClass, OpexCostDataClass
from hisim.simulationparameters import SimulationParameters


@dataclass_json
@dataclass
class StateToTemperatureModifierConfig(cp.ConfigBase):
    """Config."""

    building_name: str
    name: str
    modifier_when_heating_in_celsius: float = 0.5

    @classmethod
    def get_main_classname(cls):
        return StateToTemperatureModifier.get_full_classname()

    @classmethod
    def get_default_config(
        cls,
        building_name: str = "BUI1",
        name: str = "StateToTemperatureModifier",
        modifier_when_heating_in_celsius: float = 0.5,
    ) -> "StateToTemperatureModifierConfig":
        return StateToTemperatureModifierConfig(
            building_name=building_name,
            name=name,
            modifier_when_heating_in_celsius=modifier_when_heating_in_celsius,
        )


class StateToTemperatureModifier(cp.Component):
    """Outputs a temperature modifier (°C) from a controller state."""

    State = "State"
    TemperatureModifier = "TemperatureModifier"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: StateToTemperatureModifierConfig,
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

        self.state_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.State,
            lt.LoadTypes.ANY,
            lt.Units.ANY,
            mandatory=True,
        )

        self.modifier_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.TemperatureModifier,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            output_description="Temperature modifier (°C) derived from controller state.",
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
        state_raw: Any = stsv.get_input_value(self.state_channel)
        try:
            state = float(state_raw)
        except Exception:
            state = 0.0

        modifier = float(self.config.modifier_when_heating_in_celsius) if state > 0 else 0.0
        stsv.set_output_value(self.modifier_out, modifier)

    def get_cost_opex(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(config: cp.ConfigBase, simulation_parameters: SimulationParameters) -> CapexCostDataClass:  # noqa: ARG002
        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        return []

