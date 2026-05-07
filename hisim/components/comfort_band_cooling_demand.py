"""Comfort-band driven cooling demand controller.

Computes a cooling power request (W) based on the deviation of operative temperature
above an upper comfort setpoint (°C). Intended to drive cooling-only devices such as
district cooling or split AC when you want cooling to be aligned with adaptive comfort bounds.
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
class ComfortBandCoolingDemandConfig(cp.ConfigBase):
    """Config for comfort-band cooling demand controller."""

    building_name: str
    name: str
    max_cooling_power_in_watt: float
    proportional_gain_in_watt_per_kelvin: float = 10000.0
    relaxation_factor: float = 0.3

    @classmethod
    def get_main_classname(cls):
        return ComfortBandCoolingDemand.get_full_classname()

    @classmethod
    def get_default_config(
        cls,
        building_name: str = "BUI1",
        name: str = "ComfortBandCoolingDemand",
        max_cooling_power_in_watt: float = 20000.0,
        proportional_gain_in_watt_per_kelvin: float = 10000.0,
        relaxation_factor: float = 0.3,
    ) -> Any:
        return ComfortBandCoolingDemandConfig(
            building_name=building_name,
            name=name,
            max_cooling_power_in_watt=max_cooling_power_in_watt,
            proportional_gain_in_watt_per_kelvin=proportional_gain_in_watt_per_kelvin,
            relaxation_factor=relaxation_factor,
        )


class ComfortBandCoolingDemand(cp.Component):
    """Convert comfort-band temperature exceedance into cooling power demand (W)."""

    # Inputs
    OperativeTemperature = "OperativeTemperature"
    UpperComfortSetpoint = "UpperComfortSetpoint"
    CoolingAllowed = "CoolingAllowed"

    # Output
    CoolingDemand = "CoolingDemand"

    @dataclass
    class _State:
        last_demand_w: float

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: ComfortBandCoolingDemandConfig,
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
        self.t_upper_in: cp.ComponentInput = self.add_input(
            self.component_name,
            self.UpperComfortSetpoint,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )
        self.cooling_allowed_in: cp.ComponentInput = self.add_input(
            self.component_name,
            self.CoolingAllowed,
            lt.LoadTypes.ANY,
            lt.Units.ANY,
            mandatory=False,
        )

        self.cooling_demand_out: cp.ComponentOutput = self.add_output(
            object_name=self.component_name,
            field_name=self.CoolingDemand,
            load_type=lt.LoadTypes.COOLING,
            unit=lt.Units.WATT,
            output_description="Cooling power request derived from operative temperature above upper comfort setpoint.",
        )

        # Internal state to break algebraic loops via under-relaxation of the demand signal.
        self.state = ComfortBandCoolingDemand._State(last_demand_w=0.0)
        self.previous_state = ComfortBandCoolingDemand._State(last_demand_w=self.state.last_demand_w)

        # Hold operative temperature constant within a timestep to avoid algebraic loops during solver iterations.
        # This value is NOT part of the state on purpose (it should not be affected by i_restore_state during iterations).
        self._sampled_timestep: int = -1
        self._sampled_t_op_c: float = 0.0

    def i_prepare_simulation(self) -> None:
        pass

    def i_save_state(self) -> None:
        self.previous_state = ComfortBandCoolingDemand._State(last_demand_w=self.state.last_demand_w)

    def i_restore_state(self) -> None:
        self.state = ComfortBandCoolingDemand._State(last_demand_w=self.previous_state.last_demand_w)

    def i_doublecheck(self, timestep: int, stsv: cp.SingleTimeStepValues) -> None:
        pass

    def write_to_report(self):
        return self.config.get_string_dict()

    def i_simulate(self, timestep: int, stsv: cp.SingleTimeStepValues, force_convergence: bool) -> None:
        # Sample operative temperature once per timestep and hold it fixed for all solver iterations in this timestep.
        if timestep != self._sampled_timestep:
            self._sampled_timestep = int(timestep)
            self._sampled_t_op_c = float(stsv.get_input_value(self.t_op_in) or 0.0)

        t_op = float(self._sampled_t_op_c)
        t_upper = float(stsv.get_input_value(self.t_upper_in) or 0.0)
        allowed_raw: Optional[float] = stsv.get_input_value(self.cooling_allowed_in) if self.cooling_allowed_in.source_output is not None else None
        cooling_allowed = True
        if allowed_raw is not None:
            cooling_allowed = float(allowed_raw) > 0.0

        if not cooling_allowed:
            stsv.set_output_value(self.cooling_demand_out, 0.0)
            return

        # If the simulator forces convergence, freeze to last accepted value.
        if force_convergence:
            stsv.set_output_value(self.cooling_demand_out, float(self.previous_state.last_demand_w))
            self.state.last_demand_w = float(self.previous_state.last_demand_w)
            return

        exceed_k = max(0.0, t_op - t_upper)
        raw_w = exceed_k * float(self.config.proportional_gain_in_watt_per_kelvin)
        raw_w = max(0.0, min(raw_w, float(self.config.max_cooling_power_in_watt)))

        alpha = float(self.config.relaxation_factor)
        alpha = max(0.0, min(alpha, 1.0))
        demand_w = alpha * raw_w + (1.0 - alpha) * float(self.previous_state.last_demand_w)

        stsv.set_output_value(self.cooling_demand_out, demand_w)
        self.state.last_demand_w = demand_w

    @staticmethod
    def get_cost_capex(config: ComfortBandCoolingDemandConfig, simulation_parameters: SimulationParameters) -> cp.CapexCostDataClass:  # pylint: disable=unused-argument
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

