"""Compute comfort-band + system-storage flexibility potentials.

This component aggregates:
- Building comfort-band headroom (Wh per timestep) for heating/cooling
- Thermal storage headroom (optional, Wh)
- Available electrical energy (PV, battery, EV; optional, Wh)
and converts electrical energy to thermal energy using efficiencies derived from
existing HVAC devices (e.g., heat pump) when possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from dataclasses_json import dataclass_json

import hisim.component as cp
from hisim import loadtypes as lt
from hisim.component import CapexCostDataClass, OpexCostDataClass
from hisim.simulationparameters import SimulationParameters


@dataclass_json
@dataclass
class FlexibilityPotentialConfig(cp.ConfigBase):
    """Configuration for flexibility potential aggregation."""

    building_name: str
    name: str

    #: If no heating device efficiency can be derived from inputs, use this as COP for converting electricity->heat.
    fallback_heating_cop: float = 1.0
    #: If no cooling device efficiency can be derived from inputs, use this as EER for converting electricity->cooling.
    #: Set to 0.0 if you want "no cooling conversion unless device is present".
    fallback_cooling_eer: float = 0.0

    @classmethod
    def get_main_classname(cls):
        """Return the full class name of the base class."""
        return FlexibilityPotential.get_full_classname()


class FlexibilityPotential(cp.Component):
    """Aggregates upper/lower flexibility potentials per timestep (Wh)."""

    # Inputs (all optional except building potentials)
    PotentialHeatingEnergyUntilUpperComfortBound = "PotentialHeatingEnergyUntilUpperComfortBound"
    PotentialCoolingEnergyUntilLowerComfortBound = "PotentialCoolingEnergyUntilLowerComfortBound"

    BufferRemainingCapacity = "BufferRemainingCapacity"
    BufferStoredEnergy = "BufferStoredEnergy"

    PvElectricityEnergy = "PvElectricityEnergy"
    BatteryStoredEnergy = "BatteryStoredEnergy"
    EvStoredEnergy = "EvStoredEnergy"

    HvacThermalPowerDelivered = "HvacThermalPowerDelivered"
    HvacHeatingPower = "HvacHeatingPower"
    HvacCoolingPower = "HvacCoolingPower"
    HvacElectricPower = "HvacElectricPower"

    # Outputs
    UpperFlexibilityPotential = "UpperFlexibilityPotential"
    LowerFlexibilityPotential = "LowerFlexibilityPotential"

    UpperFlexibilityPotentialHeatingOnly = "UpperFlexibilityPotentialHeatingOnly"
    UpperFlexibilityPotentialCoolingFromElectricity = "UpperFlexibilityPotentialCoolingFromElectricity"
    LowerFlexibilityPotentialCoolingOnly = "LowerFlexibilityPotentialCoolingOnly"
    LowerFlexibilityPotentialHeatingFromElectricity = "LowerFlexibilityPotentialHeatingFromElectricity"

    DerivedHeatingCOP = "DerivedHeatingCOP"
    DerivedCoolingEER = "DerivedCoolingEER"

    def __init__(
        self,
        my_simulation_parameters: SimulationParameters,
        config: FlexibilityPotentialConfig,
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

        # Inputs
        self.potential_heating_energy_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.PotentialHeatingEnergyUntilUpperComfortBound,
            lt.LoadTypes.HEATING,
            lt.Units.WATT_HOUR,
            mandatory=True,
        )
        self.potential_cooling_energy_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.PotentialCoolingEnergyUntilLowerComfortBound,
            lt.LoadTypes.COOLING,
            lt.Units.WATT_HOUR,
            mandatory=True,
        )

        self.buffer_remaining_capacity_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.BufferRemainingCapacity,
            lt.LoadTypes.HEATING,
            lt.Units.WATT_HOUR,
            mandatory=False,
        )
        self.buffer_stored_energy_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.BufferStoredEnergy,
            lt.LoadTypes.HEATING,
            lt.Units.WATT_HOUR,
            mandatory=False,
        )

        self.pv_electricity_energy_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.PvElectricityEnergy,
            lt.LoadTypes.ELECTRICITY,
            lt.Units.WATT_HOUR,
            mandatory=False,
        )
        self.battery_stored_energy_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.BatteryStoredEnergy,
            lt.LoadTypes.ELECTRICITY,
            lt.Units.WATT_HOUR,
            mandatory=False,
        )
        self.ev_stored_energy_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.EvStoredEnergy,
            lt.LoadTypes.ELECTRICITY,
            lt.Units.WATT_HOUR,
            mandatory=False,
        )

        # HVAC signals (optional; used to derive COP/EER)
        self.hvac_heating_power_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.HvacHeatingPower,
            lt.LoadTypes.HEATING,
            lt.Units.WATT,
            mandatory=False,
        )
        self.hvac_cooling_power_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.HvacCoolingPower,
            lt.LoadTypes.COOLING,
            lt.Units.WATT,
            mandatory=False,
        )
        self.hvac_electric_power_channel: cp.ComponentInput = self.add_input(
            self.component_name,
            self.HvacElectricPower,
            lt.LoadTypes.ELECTRICITY,
            lt.Units.WATT,
            mandatory=False,
        )

        # Outputs
        self.upper_flex_channel: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.UpperFlexibilityPotential,
            lt.LoadTypes.ANY,
            lt.Units.WATT_HOUR,
            output_description="Upper flexibility potential (Wh per timestep): heating headroom + buffer remaining capacity + electricity-to-cooling converted using derived EER/COP if available.",
        )
        self.lower_flex_channel: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.LowerFlexibilityPotential,
            lt.LoadTypes.ANY,
            lt.Units.WATT_HOUR,
            output_description="Lower flexibility potential (Wh per timestep): cooling headroom + buffer stored energy + electricity-to-heating converted using derived COP if available.",
        )

        self.upper_heat_only_channel: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.UpperFlexibilityPotentialHeatingOnly,
            lt.LoadTypes.HEATING,
            lt.Units.WATT_HOUR,
            output_description="Upper flexibility contribution from comfort-band heating headroom only (Wh per timestep).",
        )
        self.upper_cool_from_el_channel: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.UpperFlexibilityPotentialCoolingFromElectricity,
            lt.LoadTypes.COOLING,
            lt.Units.WATT_HOUR,
            output_description="Upper flexibility contribution from electricity converted to cooling (Wh per timestep).",
        )
        self.lower_cool_only_channel: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.LowerFlexibilityPotentialCoolingOnly,
            lt.LoadTypes.COOLING,
            lt.Units.WATT_HOUR,
            output_description="Lower flexibility contribution from comfort-band cooling headroom only (Wh per timestep).",
        )
        self.lower_heat_from_el_channel: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.LowerFlexibilityPotentialHeatingFromElectricity,
            lt.LoadTypes.HEATING,
            lt.Units.WATT_HOUR,
            output_description="Lower flexibility contribution from electricity converted to heating (Wh per timestep).",
        )

        self.derived_heating_cop_channel: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.DerivedHeatingCOP,
            lt.LoadTypes.ANY,
            lt.Units.ANY,
            output_description="Derived effective heating COP from HVAC power signals (dimensionless).",
        )
        self.derived_cooling_eer_channel: cp.ComponentOutput = self.add_output(
            self.component_name,
            self.DerivedCoolingEER,
            lt.LoadTypes.ANY,
            lt.Units.ANY,
            output_description="Derived effective cooling EER from HVAC power signals (dimensionless).",
        )

    def i_prepare_simulation(self) -> None:
        pass

    def i_save_state(self) -> None:
        pass

    def i_restore_state(self) -> None:
        pass

    def i_simulate(self, timestep: int, stsv: cp.SingleTimeStepValues, force_convergence: bool) -> None:
        # Building headroom (Wh per timestep)
        pot_heat_wh = float(stsv.get_input_value(self.potential_heating_energy_channel))
        pot_cool_wh = float(stsv.get_input_value(self.potential_cooling_energy_channel))

        # Thermal storage (optional)
        buffer_remaining_wh = float(stsv.get_input_value(self.buffer_remaining_capacity_channel) or 0.0)
        buffer_stored_wh = float(stsv.get_input_value(self.buffer_stored_energy_channel) or 0.0)

        # Electricity available this timestep (PV generation + optionally stored electricity that could be discharged).
        # Note: Stored energy is treated as "available" here (no min-SoC limits), because components differ widely.
        pv_el_wh = float(stsv.get_input_value(self.pv_electricity_energy_channel) or 0.0)
        batt_el_wh = float(stsv.get_input_value(self.battery_stored_energy_channel) or 0.0)
        ev_el_wh = float(stsv.get_input_value(self.ev_stored_energy_channel) or 0.0)
        available_el_wh = max(0.0, pv_el_wh) + max(0.0, batt_el_wh) + max(0.0, ev_el_wh)

        # Derive device efficiencies from HVAC signals (if connected).
        # HVAC signals can be provided as signed thermal power:
        # - heating: positive
        # - cooling: often negative (e.g., heat pump / AC)
        hvac_heat_w_raw = float(stsv.get_input_value(self.hvac_heating_power_channel) or 0.0)
        hvac_cool_w_raw = float(stsv.get_input_value(self.hvac_cooling_power_channel) or 0.0)
        hvac_el_w = float(stsv.get_input_value(self.hvac_electric_power_channel) or 0.0)

        hvac_heat_w = max(0.0, hvac_heat_w_raw)
        # interpret negative signal as cooling magnitude
        hvac_cool_w = max(0.0, hvac_cool_w_raw) if hvac_cool_w_raw >= 0.0 else max(0.0, -hvac_cool_w_raw)

        derived_heating_cop = 0.0
        derived_cooling_eer = 0.0
        if hvac_el_w > 1e-6:
            derived_heating_cop = hvac_heat_w / hvac_el_w
            derived_cooling_eer = hvac_cool_w / hvac_el_w

        heating_cop = derived_heating_cop if derived_heating_cop > 0.0 else float(self.config.fallback_heating_cop)
        cooling_eer = derived_cooling_eer if derived_cooling_eer > 0.0 else float(self.config.fallback_cooling_eer)

        # Upper flexibility (as specified): add heat headroom + buffer remaining + convert available electricity to cooling energy.
        upper_from_heating_wh = max(0.0, pot_heat_wh)
        # Only count electricity->cooling conversion if cooling is enabled (indicated by non-zero cooling headroom).
        upper_from_cooling_el_wh = (
            max(0.0, available_el_wh * max(0.0, cooling_eer)) if pot_cool_wh > 0.0 else 0.0
        )
        upper_total_wh = upper_from_heating_wh + max(0.0, buffer_remaining_wh) + upper_from_cooling_el_wh

        # Lower flexibility (as specified): remove heat (cooling headroom) + buffer stored + convert available electricity to heating energy.
        lower_from_cooling_wh = max(0.0, pot_cool_wh)
        lower_from_heating_el_wh = max(0.0, available_el_wh * max(0.0, heating_cop))
        lower_total_wh = lower_from_cooling_wh + max(0.0, buffer_stored_wh) + lower_from_heating_el_wh

        stsv.set_output_value(self.upper_flex_channel, upper_total_wh)
        stsv.set_output_value(self.lower_flex_channel, lower_total_wh)

        stsv.set_output_value(self.upper_heat_only_channel, upper_from_heating_wh)
        stsv.set_output_value(self.upper_cool_from_el_channel, upper_from_cooling_el_wh)
        stsv.set_output_value(self.lower_cool_only_channel, lower_from_cooling_wh)
        stsv.set_output_value(self.lower_heat_from_el_channel, lower_from_heating_el_wh)

        stsv.set_output_value(self.derived_heating_cop_channel, heating_cop)
        stsv.set_output_value(self.derived_cooling_eer_channel, cooling_eer)

    def write_to_report(self) -> List[str]:
        return [
            "FlexibilityPotential aggregates comfort-band headroom + storage + electricity conversion."
        ]

    def get_cost_opex(  # type: ignore[override]
        self,
        all_outputs: List,  # noqa: ARG002
        postprocessing_results,  # noqa: ARG002
    ) -> OpexCostDataClass:
        """Return zero opex costs (analysis-only component)."""
        return OpexCostDataClass.get_default_opex_cost_data_class()

    @staticmethod
    def get_cost_capex(config: cp.ConfigBase, simulation_parameters: SimulationParameters) -> CapexCostDataClass:  # noqa: ARG002
        """Return zero capex costs (analysis-only component)."""
        return CapexCostDataClass.get_default_capex_cost_data_class()

    def get_component_kpi_entries(  # type: ignore[override]
        self,
        all_outputs: List,  # noqa: ARG002
        postprocessing_results,  # noqa: ARG002
    ) -> List:
        """Return no KPIs (analysis-only component)."""
        return []

