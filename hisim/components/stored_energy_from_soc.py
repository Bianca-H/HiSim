"""Convert state of charge (SoC) to stored energy (Wh)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from dataclasses_json import dataclass_json

import hisim.component as cp
from hisim.component import CapexCostDataClass, OpexCostDataClass
from hisim.loadtypes import LoadTypes, Units
from hisim.simulationparameters import SimulationParameters


@dataclass_json
@dataclass
class StoredEnergyFromSocConfig(cp.ConfigBase):
    """Config for SOC→Wh conversion."""

    building_name: str
    name: str
    capacity_in_kwh: float

    @classmethod
    def get_main_classname(cls):
        return StoredEnergyFromSoc.get_full_classname()


class StoredEnergyFromSoc(cp.Component):
    """Outputs stored energy in Wh from SoC and a fixed capacity."""

    StateOfCharge = "StateOfCharge"
    StoredEnergy = "StoredEnergy"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: StoredEnergyFromSocConfig,
        my_display_config: cp.DisplayConfig = cp.DisplayConfig(display_in_webtool=True),
    ) -> None:
        super().__init__(
            name=config.name,
            my_simulation_parameters=my_simulation_parameters,
            my_config=config,
            my_display_config=my_display_config,
        )
        self.my_simulation_parameters = my_simulation_parameters
        self.config = config

        self.soc_in: cp.ComponentInput = self.add_input(
            self.component_name,
            self.StateOfCharge,
            LoadTypes.ANY,
            Units.ANY,
            mandatory=True,
        )
        self.energy_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.StoredEnergy,
            LoadTypes.ELECTRICITY,
            Units.WATT_HOUR,
            output_description="Stored electrical energy (Wh) computed from SoC * capacity.",
        )

    def i_prepare_simulation(self) -> None:
        pass

    def i_save_state(self) -> None:
        pass

    def i_restore_state(self) -> None:
        pass

    def i_doublecheck(self, timestep: int, stsv: cp.SingleTimeStepValues) -> None:
        pass

    def i_simulate(self, timestep: int, stsv: cp.SingleTimeStepValues, force_convergence: bool) -> None:
        soc = float(stsv.get_input_value(self.soc_in) or 0.0)
        soc = min(1.0, max(0.0, soc))
        stored_wh = soc * float(self.config.capacity_in_kwh) * 1000.0
        stsv.set_output_value(self.energy_out, stored_wh)

    def write_to_report(self) -> List[str]:
        return [
            f"{self.component_name}: capacity={self.config.capacity_in_kwh} kWh (SoC→Wh converter).",
        ]

    def get_cost_opex(self, all_outputs: List, postprocessing_results) -> OpexCostDataClass:  # noqa: ARG002
        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(config: cp.ConfigBase, simulation_parameters: SimulationParameters) -> CapexCostDataClass:  # noqa: ARG002
        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(self, all_outputs: List, postprocessing_results) -> List:  # noqa: ARG002
        return []

