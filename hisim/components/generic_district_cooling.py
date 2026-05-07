"""Generic district cooling component.

This component provides cooling power from a district cooling network based on a cooling demand signal.
It is intentionally simple: it limits delivered cooling to a configured connected load and outputs
both delivered cooling power (negative sign convention, like split AC) and delivered cooling energy.

The delivered cooling energy output is intended to be metered via `FuelMeter` (using district heating/cooling
cost/emission factors) by tagging it as "heat consumption" via FuelMeter's dynamic default connections.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dataclasses_json import dataclass_json

from hisim import component as cp
from hisim import loadtypes as lt
from hisim.simulationparameters import SimulationParameters


@dataclass_json
@dataclass
class DistrictCoolingConfig(cp.ConfigBase):
    """Config for district cooling."""

    building_name: str
    name: str
    connected_load_w: float

    @classmethod
    def get_main_classname(cls):
        return DistrictCooling.get_full_classname()

    @classmethod
    def get_default_config(
        cls,
        building_name: str = "BUI1",
        name: str = "DistrictCooling",
        connected_load_w: float = 20000.0,
    ) -> Any:
        return DistrictCoolingConfig(
            building_name=building_name,
            name=name,
            connected_load_w=connected_load_w,
        )


class DistrictCooling(cp.Component):
    """District cooling network (cooling-only)."""

    # Inputs
    CoolingDemand = "CoolingDemand"

    # Outputs
    ThermalOutputCoolingPower = "ThermalOutputCoolingPower"
    ThermalOutputCoolingEnergy = "ThermalOutputCoolingEnergy"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: DistrictCoolingConfig,
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

        self.cooling_demand_in: cp.ComponentInput = self.add_input(
            self.component_name,
            self.CoolingDemand,
            lt.LoadTypes.COOLING,
            lt.Units.WATT,
            mandatory=True,
        )

        # Cooling delivered to the building. Use negative sign convention (cooling is negative thermal power).
        self.cooling_power_out: cp.ComponentOutput = self.add_output(
            object_name=self.component_name,
            field_name=self.ThermalOutputCoolingPower,
            load_type=lt.LoadTypes.COOLING,
            unit=lt.Units.WATT,
            output_description="Cooling power delivered from district cooling network (negative sign).",
        )

        # Energy consumption for metering (positive magnitude). Use HEATING load type to match FuelMeter expectations.
        self.cooling_energy_out: cp.ComponentOutput = self.add_output(
            object_name=self.component_name,
            field_name=self.ThermalOutputCoolingEnergy,
            load_type=lt.LoadTypes.HEATING,
            unit=lt.Units.WATT_HOUR,
            output_description="Cooling energy delivered from district cooling network (positive magnitude, Wh).",
        )

    def i_prepare_simulation(self) -> None:
        pass

    def i_save_state(self) -> None:
        pass

    def i_restore_state(self) -> None:
        pass

    def i_doublecheck(self, timestep: int, stsv: cp.SingleTimeStepValues) -> None:
        pass

    def write_to_report(self):
        return self.config.get_string_dict()

    def i_simulate(self, timestep: int, stsv: cp.SingleTimeStepValues, force_convergence: bool) -> None:
        demand_w = float(stsv.get_input_value(self.cooling_demand_in) or 0.0)
        demand_w = max(demand_w, 0.0)
        delivered_w = min(demand_w, float(self.config.connected_load_w))

        seconds = float(self.my_simulation_parameters.seconds_per_timestep)
        delivered_wh = delivered_w * seconds / 3600.0

        stsv.set_output_value(self.cooling_power_out, -delivered_w)
        stsv.set_output_value(self.cooling_energy_out, delivered_wh)

    @staticmethod
    def get_cost_capex(config: DistrictCoolingConfig, simulation_parameters: SimulationParameters) -> cp.CapexCostDataClass:  # pylint: disable=unused-argument
        return cp.CapexCostDataClass.get_default_capex_cost_data_class()

    def get_cost_opex(
        self,
        all_outputs: list,  # pylint: disable=unused-argument
        postprocessing_results,  # pylint: disable=unused-argument
    ) -> cp.OpexCostDataClass:
        return cp.OpexCostDataClass.get_default_opex_cost_data_class()

    def get_component_kpi_entries(
        self,
        all_outputs: list,  # pylint: disable=unused-argument
        postprocessing_results,  # pylint: disable=unused-argument
    ):
        return []

