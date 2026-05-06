"""Hydraulic mixer for two heat generators.

This component combines two hydraulic heat generator outputs (temperature + mass flow)
into one mixed temperature + total mass flow. It is useful when multiple generators
feed the same buffer tank, but the buffer tank has only a single heat-generator inlet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pandas as pd

from hisim import component as cp
from hisim import loadtypes as lt
from hisim.simulationparameters import SimulationParameters
from hisim.component import OpexCostDataClass, CapexCostDataClass
from hisim.postprocessing.kpi_computation.kpi_structure import KpiEntry


@dataclass
class HydraulicTemperatureMassflowMixerConfig(cp.ConfigBase):
    """Config for the hydraulic mixer."""

    building_name: str
    name: str

    @classmethod
    def get_main_classname(cls) -> str:
        return HydraulicTemperatureMassflowMixer.get_full_classname()

    @classmethod
    def get_default_config(
        cls,
        building_name: str = "BUI1",
        name: str = "HydraulicMixer",
    ) -> "HydraulicTemperatureMassflowMixerConfig":
        return HydraulicTemperatureMassflowMixerConfig(building_name=building_name, name=name)


class HydraulicTemperatureMassflowMixer(cp.Component):
    """Mix two (T, m_dot) streams into one."""

    Temperature1 = "Temperature1"
    MassFlow1 = "MassFlow1"
    Temperature2 = "Temperature2"
    MassFlow2 = "MassFlow2"

    MixedTemperature = "MixedTemperature"
    MixedMassFlow = "MixedMassFlow"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: HydraulicTemperatureMassflowMixerConfig,
        my_display_config: cp.DisplayConfig = cp.DisplayConfig(),
    ) -> None:
        self.my_simulation_parameters = my_simulation_parameters
        self.config = config
        super().__init__(
            name=self.get_component_name(),
            my_simulation_parameters=my_simulation_parameters,
            my_config=config,
            my_display_config=my_display_config,
        )

        self.t1_in = self.add_input(
            self.component_name,
            self.Temperature1,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )
        self.m1_in = self.add_input(
            self.component_name,
            self.MassFlow1,
            lt.LoadTypes.WATER,
            lt.Units.KG_PER_SEC,
            mandatory=True,
        )
        self.t2_in = self.add_input(
            self.component_name,
            self.Temperature2,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            mandatory=True,
        )
        self.m2_in = self.add_input(
            self.component_name,
            self.MassFlow2,
            lt.LoadTypes.WATER,
            lt.Units.KG_PER_SEC,
            mandatory=True,
        )

        self.t_mix_out = self.add_output(
            self.component_name,
            self.MixedTemperature,
            lt.LoadTypes.TEMPERATURE,
            lt.Units.CELSIUS,
            output_description="Mass-flow weighted mixed temperature.",
        )
        self.m_mix_out = self.add_output(
            self.component_name,
            self.MixedMassFlow,
            lt.LoadTypes.WATER,
            lt.Units.KG_PER_SEC,
            output_description="Total mixed mass flow.",
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
        t1 = float(stsv.get_input_value(self.t1_in))
        m1 = float(stsv.get_input_value(self.m1_in))
        t2 = float(stsv.get_input_value(self.t2_in))
        m2 = float(stsv.get_input_value(self.m2_in))

        m_total = m1 + m2
        if m_total > 0:
            t_mix = (m1 * t1 + m2 * t2) / m_total
        else:
            # No flow -> temperature is irrelevant for the buffer inlet. Use 0 to avoid NaNs.
            t_mix = 0.0

        stsv.set_output_value(self.m_mix_out, m_total)
        stsv.set_output_value(self.t_mix_out, t_mix)

    def write_to_report(self) -> List[str]:
        return self.config.get_string_dict()

    def get_cost_opex(self, all_outputs: List, postprocessing_results: pd.DataFrame) -> OpexCostDataClass:
        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(
        config: HydraulicTemperatureMassflowMixerConfig, simulation_parameters: SimulationParameters
    ) -> CapexCostDataClass:  # pylint: disable=unused-argument
        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(self, all_outputs: List, postprocessing_results: pd.DataFrame) -> List[KpiEntry]:
        return []

