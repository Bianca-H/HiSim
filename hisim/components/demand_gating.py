"""Gate a thermal demand signal by a controller state.

Used to combine:
- a (possibly continuous) theoretical building demand [W]
- a controller state (heating/cooling/off)

into a demand that is zero when the controller is off, and only keeps the
matching sign when heating/cooling is requested.
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
class DemandGatingConfig(cp.ConfigBase):
    """Config for demand gating."""

    building_name: str
    name: str

    @classmethod
    def get_main_classname(cls):
        """Return full class name."""
        return DemandGating.get_full_classname()

    @classmethod
    def get_default_config(cls, building_name: str = "BUI1", name: str = "DemandGating") -> "DemandGatingConfig":
        """Get default config."""
        return DemandGatingConfig(building_name=building_name, name=name)


class DemandGating(cp.Component):
    """Gate demand by controller state.

    Inputs:
    - DemandInWatt: signed demand (positive=heating, negative=cooling)
    - State: controller state (positive=heating, negative=cooling, zero=off)

    Output:
    - GatedDemandInWatt: demand after gating
    """

    DemandInWatt = "DemandInWatt"
    State = "State"
    GatedDemandInWatt = "GatedDemandInWatt"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: DemandGatingConfig,
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

        self.demand_in_watt_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.DemandInWatt,
            lt.LoadTypes.HEATING,
            lt.Units.WATT,
            mandatory=True,
        )
        self.state_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.State,
            lt.LoadTypes.ANY,
            lt.Units.ANY,
            mandatory=True,
        )

        self.gated_demand_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.GatedDemandInWatt,
            lt.LoadTypes.HEATING,
            lt.Units.WATT,
            output_description="Demand after gating by controller state (W).",
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
        demand = float(stsv.get_input_value(self.demand_in_watt_channel))
        state_raw: Any = stsv.get_input_value(self.state_channel)
        try:
            state = float(state_raw)
        except Exception:
            state = 0.0

        gated = 0.0
        if state > 0:
            gated = demand if demand > 0 else 0.0
        elif state < 0:
            gated = demand if demand < 0 else 0.0
        else:
            gated = 0.0

        stsv.set_output_value(self.gated_demand_out, gated)

    def get_cost_opex(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        """Return zero opex costs (signal processing component)."""
        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(config: cp.ConfigBase, simulation_parameters: SimulationParameters) -> CapexCostDataClass:  # noqa: ARG002
        """Return zero capex costs (signal processing component)."""
        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        """Return no KPIs (signal processing component)."""
        return []

