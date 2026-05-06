"""Compute a building/flow temperature modifier with comfort recovery boost.

Goal: avoid comfort violations caused by short buffer charging spikes.

We use:
- a strict target heating setpoint (from strict comfort controller)
- the current operative temperature
- the current comfort lower bound

to generate a BuildingTemperatureModifier that:
1) aligns the building setpoint with the strict target
2) adds an additional boost when operative temperature falls below comfort lower bound
   so the hydronic loop requests higher flow temperatures and runs longer.
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
class StrictComfortRecoveryModifierConfig(cp.ConfigBase):
    """Config."""

    building_name: str
    name: str
    base_heating_setpoint_in_celsius: float = 20.5
    recovery_gain: float = 2.0
    max_recovery_boost_in_celsius: float = 3.0
    clamp_min_modifier_in_celsius: float = 0.0
    clamp_max_modifier_in_celsius: float = 10.0

    @classmethod
    def get_main_classname(cls):
        return StrictComfortRecoveryModifier.get_full_classname()

    @classmethod
    def get_default_config(
        cls,
        building_name: str = "BUI1",
        name: str = "StrictComfortRecoveryModifier",
        base_heating_setpoint_in_celsius: float = 20.5,
    ) -> "StrictComfortRecoveryModifierConfig":
        return StrictComfortRecoveryModifierConfig(
            building_name=building_name,
            name=name,
            base_heating_setpoint_in_celsius=base_heating_setpoint_in_celsius,
        )


class StrictComfortRecoveryModifier(cp.Component):
    """Outputs a temperature modifier (°C) for Building and HeatDistributionController."""

    StrictLowerSetpoint = "StrictLowerSetpoint"
    OperativeTemperature = "OperativeTemperature"
    ComfortLowerBound = "ComfortLowerBound"
    TemperatureModifier = "TemperatureModifier"
    ComfortDeficit = "ComfortDeficit"
    RecoveryBoost = "RecoveryBoost"
    TargetHeatingSetpoint = "TargetHeatingSetpoint"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: StrictComfortRecoveryModifierConfig,
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

        self.strict_lower_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.StrictLowerSetpoint,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )
        self.operative_temp_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.OperativeTemperature,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )
        self.comfort_lower_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.ComfortLowerBound,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )

        self.modifier_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.TemperatureModifier,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            output_description="Temperature modifier (°C): strict target alignment + recovery boost when below comfort.",
        )
        self.deficit_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.ComfortDeficit,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            output_description="Comfort deficit (°C): max(0, comfort_lower - operative).",
        )
        self.boost_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.RecoveryBoost,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            output_description="Recovery boost added to modifier (°C).",
        )
        self.target_setpoint_out: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.TargetHeatingSetpoint,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            output_description="Effective target heating setpoint (°C): base + modifier.",
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
        strict_lower = float(stsv.get_input_value(self.strict_lower_channel))
        operative = float(stsv.get_input_value(self.operative_temp_channel))
        comfort_lower = float(stsv.get_input_value(self.comfort_lower_channel))

        base = float(self.config.base_heating_setpoint_in_celsius)
        # Align building setpoint with strict lower target.
        modifier = strict_lower - base

        # Recovery boost if actually below comfort lower bound.
        deficit = max(0.0, comfort_lower - operative)
        boost = min(float(self.config.max_recovery_boost_in_celsius), float(self.config.recovery_gain) * deficit)
        modifier += boost

        modifier = max(float(self.config.clamp_min_modifier_in_celsius), modifier)
        modifier = min(float(self.config.clamp_max_modifier_in_celsius), modifier)
        stsv.set_output_value(self.modifier_out, modifier)
        stsv.set_output_value(self.deficit_out, deficit)
        stsv.set_output_value(self.boost_out, boost)
        stsv.set_output_value(self.target_setpoint_out, base + modifier)

    def get_cost_opex(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(config: cp.ConfigBase, simulation_parameters: SimulationParameters) -> CapexCostDataClass:  # noqa: ARG002
        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(self, all_outputs, postprocessing_results):  # type: ignore[override]  # noqa: ARG002
        return []

