"""Comfort-band driven heating demand controller.

Computes a heating power request (W) based on the deviation of operative temperature
below a lower comfort setpoint (°C). Intended to drive heating-only systems when you
want heating to be aligned with adaptive comfort bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from dataclasses_json import dataclass_json

from hisim import component as cp
from hisim import loadtypes as lt
from hisim.simulationparameters import SimulationParameters


@dataclass_json
@dataclass
class ComfortBandHeatingDemandConfig(cp.ConfigBase):
    """Config for comfort-band heating demand controller."""

    building_name: str
    name: str
    max_heating_power_in_watt: float
    proportional_gain_in_watt_per_kelvin: float = 10000.0

    @classmethod
    def get_main_classname(cls):
        return ComfortBandHeatingDemand.get_full_classname()

    @classmethod
    def get_default_config(
        cls,
        building_name: str = "BUI1",
        name: str = "ComfortBandHeatingDemand",
        max_heating_power_in_watt: float = 20000.0,
        proportional_gain_in_watt_per_kelvin: float = 10000.0,
    ) -> Any:
        return ComfortBandHeatingDemandConfig(
            building_name=building_name,
            name=name,
            max_heating_power_in_watt=max_heating_power_in_watt,
            proportional_gain_in_watt_per_kelvin=proportional_gain_in_watt_per_kelvin,
        )


class ComfortBandHeatingDemand(cp.Component):
    """Convert comfort-band temperature deficit into heating power demand (W)."""

    # Inputs
    OperativeTemperature = "OperativeTemperature"
    LowerComfortSetpoint = "LowerComfortSetpoint"
    HeatingAllowed = "HeatingAllowed"

    # Output
    HeatingDemand = "HeatingDemand"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: ComfortBandHeatingDemandConfig,
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

        self.t_op_in: cp.ComponentInput = self.add_input(
            self.component_name,
            self.OperativeTemperature,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )
        self.t_lower_in: cp.ComponentInput = self.add_input(
            self.component_name,
            self.LowerComfortSetpoint,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )
        self.heating_allowed_in: cp.ComponentInput = self.add_input(
            self.component_name,
            self.HeatingAllowed,
            lt.LoadTypes.ANY,
            lt.Units.ANY,
            mandatory=False,
        )

        self.heating_demand_out: cp.ComponentOutput = self.add_output(
            object_name=self.component_name,
            field_name=self.HeatingDemand,
            load_type=lt.LoadTypes.HEATING,
            unit=lt.Units.WATT,
            output_description="Heating power request derived from operative temperature below lower comfort setpoint.",
        )

        # Hold operative temperature constant within a timestep to avoid algebraic loops during solver iterations.
        self._sampled_timestep: int = -1
        self._sampled_t_op_c: float = 0.0

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
        if timestep != self._sampled_timestep:
            self._sampled_timestep = int(timestep)
            self._sampled_t_op_c = float(stsv.get_input_value(self.t_op_in) or 0.0)

        t_op = float(self._sampled_t_op_c)
        t_lower = float(stsv.get_input_value(self.t_lower_in) or 0.0)

        allowed_raw: Optional[float] = (
            stsv.get_input_value(self.heating_allowed_in)
            if self.heating_allowed_in.source_output is not None
            else None
        )
        heating_allowed = True
        if allowed_raw is not None:
            heating_allowed = float(allowed_raw) > 0.0

        if not heating_allowed:
            stsv.set_output_value(self.heating_demand_out, 0.0)
            return

        deficit_k = max(0.0, t_lower - t_op)
        demand_w = deficit_k * float(self.config.proportional_gain_in_watt_per_kelvin)
        demand_w = max(0.0, min(demand_w, float(self.config.max_heating_power_in_watt)))
        stsv.set_output_value(self.heating_demand_out, demand_w)

    @staticmethod
    def get_cost_capex(config: ComfortBandHeatingDemandConfig, simulation_parameters: SimulationParameters) -> cp.CapexCostDataClass:  # pylint: disable=unused-argument
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

