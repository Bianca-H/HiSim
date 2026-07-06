"""BP10 household system setup (pellet boiler + DHW + buffer + solar thermal, no PV, no cooling).

Pellet + solar DHW behaviour (typical plant representation):
- **Long minimum run / rest** on the boiler controller: once lit, the pellet cannot be cut off until the minimum
  run time has elapsed; it may **modulate down** (not only full load). Between cycles, minimum rest applies.
- **Winter** (daily mean outdoor temperature below the seasonal threshold): modulation **floor** at **1/12 of
  nominal** thermal power — typical minimum stable load while the burner is on.
- **Summer** (warm daily mean): modulation floor **0** on the controller, and **plant** `minimal_thermal_power_in_watt = 0`
  on the boiler so the pellet can stay **off** for long stretches when **solar thermal** covers DHW.
"""

from __future__ import annotations

from typing import Any, Optional

from hisim import cli_overrides, heating_system_selection, loadtypes, log
from hisim.postprocessingoptions import PostProcessingOptions
from hisim.simulator import SimulationParameters
from hisim.component import Coordinates

from hisim.components import electricity_meter
from hisim.components import flexibility_potential
from hisim.components import heat_distribution_system
from hisim.components import loadprofilegenerator_utsp_connector
from hisim.components import generic_boiler
from hisim.components import fuel_meter
from hisim.components import simple_water_storage
from hisim.components import sia2024_occupancy
from hisim.components import generic_heat_pump
from hisim.components import strict_comfort_recovery_modifier
from hisim.components import buffer_remaining_capacity
from hisim.components import comfort_band_heating_demand
from hisim.components import solar_thermal_system
from hisim.components import sumbuilder
from hisim.components import weather
from hisim.components import building


def setup_function(my_sim: Any, my_simulation_parameters: Optional[SimulationParameters] = None) -> None:  # noqa: too-many-statements
    """Set up BP10 (BP01 + solar DHW on primary DHW path, pellet secondary; no PV)."""

    # =============================================================================================================================
    # Set simulation parameters
    year = cli_overrides.get_economic_year()
    seconds_per_timestep = 900.0  # 15 min timesteps (3600s would be 1h, but can cause stability issues with large HP time constants)

    if my_simulation_parameters is None:
        my_simulation_parameters = SimulationParameters.full_year_with_minimal_variant_artifacts(
            year=year, seconds_per_timestep=seconds_per_timestep
        )

    batch_open_explorer = (cli_overrides.get_override("BATCH_OPEN_EXPLORER") or "").strip()
    if batch_open_explorer == "0":
        my_simulation_parameters.post_processing_options = [
            opt
            for opt in my_simulation_parameters.post_processing_options
            if opt != PostProcessingOptions.OPEN_DIRECTORY_IN_EXPLORER
        ]

    my_sim.set_simulation_parameters(my_simulation_parameters)

    # =============================================================================================================================
    # Defaults for optional CLI overrides (defined once, reused below)
    default_arch = "01_CH"
    default_weather = "ZUESTA"

    # Determine ARCH / WEATHER used
    arch_override = cli_overrides.get_override("ARCH")
    arch_used = arch_override if arch_override is not None else default_arch

    weather_override = cli_overrides.get_override("WEATHER")
    weather_used = weather_override if weather_override is not None else default_weather

    cli_overrides.set_used_value("ARCH", arch_used)
    cli_overrides.set_used_value("WEATHER", weather_used)

    time_horizon_used = cli_overrides.get_time_horizon()
    cli_overrides.set_used_value("TIME_HORIZON", time_horizon_used)

    active_scenarios = cli_overrides.get_active_scenarios()
    cli_overrides.set_used_value(
        "SCENARIO",
        ",".join(sorted(active_scenarios)) if active_scenarios else "none",
    )
    if cli_overrides.has_scenario(cli_overrides.SCENARIO_FOSSIL_CRISIS):
        log.information(
            "Applied scenario SCENARIO=fossil_Crisis: elevated gas/oil prices and lowered adaptive comfort lower bound."
        )
    if cli_overrides.has_scenario(cli_overrides.SCENARIO_HEATWAVE):
        log.information(
            f"Applied scenario SCENARIO=heatwave with TIME_HORIZON={time_horizon_used}: using heatwave future weather file."
        )

    # =============================================================================================================================
    # Build Weather
    my_weather_config = cli_overrides.apply_weather_location_override(
        weather_module=weather,
        weather_value=weather_used,
        name="Weather",
        building_name="BUI1",
    )
    my_weather = weather.Weather(config=my_weather_config, my_simulation_parameters=my_simulation_parameters)

    if my_weather_config.latitude is None or my_weather_config.longitude is None:
        solar_site_latitude = 47.37759
        solar_site_longitude = 8.530352
    else:
        solar_site_latitude = float(my_weather_config.latitude)
        solar_site_longitude = float(my_weather_config.longitude)

    # =============================================================================================================================
    # Build Building (with ARCH override + location-dependent heating reference temperature mapping)
    my_building_config = cli_overrides.apply_building_archetype_override(
        building_module=building,
        arch_value=default_arch,
    )
    if arch_override is not None:
        try:
            my_building_config = cli_overrides.apply_building_archetype_override(
                building_module=building,
                arch_value=arch_override,
            )
            log.information(f"Applied CLI override ARCH={arch_override} to building configuration.")
        except Exception:
            log.warning(
                f"CLI override ARCH={arch_override} was provided, but no matching "
                f"`BuildingConfig.get_{arch_override}_single_family_home()` exists. Using default building config."
            )
            my_building_config = cli_overrides.apply_building_archetype_override(
                building_module=building,
                arch_value=default_arch,
            )
            arch_used = default_arch
            cli_overrides.set_used_value("ARCH", arch_used)

    weather_to_ref_temp_c = {
        "ZUESTA": -8.0,  # Zurich
        "BASSTA": -7.0,  # Basel
        "KLO": -9.0,  # Kloten
        "RUE": -10.0,  # Ruenenberg
    }
    if weather_used in weather_to_ref_temp_c:
        my_building_config.heating_reference_temperature_in_celsius = weather_to_ref_temp_c[weather_used]

    # Ensure building demand is based on (at least) the comfort-lower level used in basic_household.
    # The adaptive comfort bounds are exposed as outputs, but the building demand setpoint is driven by these config values.
    my_building_config.set_heating_temperature_in_celsius = cli_overrides.DEFAULT_HEATING_SETPOINT_IN_CELSIUS
    cli_overrides.apply_scenario_building_settings(my_building_config)
    # Prevent any cooling demand in BP10 (no cooling system in this variant).
    my_building_config.set_cooling_temperature_in_celsius = 99.0
    cli_overrides.apply_swiss_sia_natural_ventilation_settings(my_building_config)

    my_building_information = building.BuildingInformation(config=my_building_config)
    my_building = building.Building(config=my_building_config, my_simulation_parameters=my_simulation_parameters)

    roof_area_m2 = float(my_building_information.roof_area_in_m2 or 0.0)
    solar_thermal_area_m2 = float((cli_overrides.get_override("SOLAR_THERMAL_AREA_M2") or "12.0").strip())
    solar_thermal_area_m2 = max(0.0, min(solar_thermal_area_m2, max(0.0, roof_area_m2 - 1.0)))
    log.information(
        f"BP10 roof: total roof {roof_area_m2:.1f} m2, solar thermal collector area {solar_thermal_area_m2:.1f} m2 (no PV)."
    )
    cli_overrides.set_used_value("SOLAR_THERMAL_AREA_M2", str(int(round(solar_thermal_area_m2))))

    # =============================================================================================================================
    # Build Occupancy (SIA2024 or LPG/UTSP)
    occ_mode = (cli_overrides.get_override("OCC") or "SIA2024").strip().upper()
    cli_overrides.set_used_value("OCC", occ_mode)
    # Default for batch runs (even if not used in this setup)
    cli_overrides.set_used_value("CAR_SCHEDULE", (cli_overrides.get_override("CAR_SCHEDULE") or "LPG").strip().upper())
    cli_overrides.set_used_value("HP_SHARE_OF_IDEAL", str(float(cli_overrides.get_override("HP_SHARE_OF_IDEAL") or 0.8)))
    if occ_mode == "SIA2024":
        floor_area_m2 = float(my_building_config.absolute_conditioned_floor_area_in_m2 or 0.0)
        sia_use_type = (cli_overrides.get_override("SIA_USE") or "residential").strip()
        my_occupancy_config = sia2024_occupancy.SIA2024OccupancyConfig.get_default_for_use_type(
            conditioned_floor_area_in_m2=floor_area_m2,
            use_type=sia_use_type,
            building_name="BUI1",
            name="SIA2024Occupancy",
        )
        my_occupancy = sia2024_occupancy.SIA2024Occupancy(
            config=my_occupancy_config, my_simulation_parameters=my_simulation_parameters
        )
        log.information(f"Using SIA 2024 schedules (SIA_USE={sia_use_type}, A_f={floor_area_m2:.1f} m2).")
    else:
        my_occupancy_config = loadprofilegenerator_utsp_connector.UtspLpgConnectorConfig.get_default_utsp_connector_config()
        my_occupancy = loadprofilegenerator_utsp_connector.UtspLpgConnector(
            config=my_occupancy_config, my_simulation_parameters=my_simulation_parameters
        )
        log.information("Using LPG/UTSP schedules (default). Set OCC=SIA2024 to switch.")

    # =============================================================================================================================
    # Heat distribution, buffer tank, DHW storage, boiler, controllers

    # Heat Distribution Controller
    my_hds_controller_config = heat_distribution_system.HeatDistributionControllerConfig.get_default_heat_distribution_controller_config(
        set_heating_temperature_for_building_in_celsius=my_building_information.set_heating_temperature_for_building_in_celsius,
        set_cooling_temperature_for_building_in_celsius=my_building_information.set_cooling_temperature_for_building_in_celsius,
        heating_load_of_building_in_watt=my_building_information.max_thermal_building_demand_in_watt,
        heating_reference_temperature_in_celsius=my_building_information.heating_reference_temperature_in_celsius,
    )
    my_heat_distribution_controller = heat_distribution_system.HeatDistributionController(
        my_simulation_parameters=my_simulation_parameters, config=my_hds_controller_config
    )
    my_hds_controller_information = heat_distribution_system.HeatDistributionControllerInformation(
        config=my_hds_controller_config
    )

    # Heat Distribution System
    my_heat_distribution_config = heat_distribution_system.HeatDistributionConfig.get_default_heatdistributionsystem_config(
        water_mass_flow_rate_in_kg_per_second=my_hds_controller_information.water_mass_flow_rate_in_kp_per_second,
        absolute_conditioned_floor_area_in_m2=my_building_information.scaled_conditioned_floor_area_in_m2,
        heating_system=my_hds_controller_information.hds_controller_config.heating_system,
    )
    my_heat_distribution = heat_distribution_system.HeatDistribution(
        config=my_heat_distribution_config, my_simulation_parameters=my_simulation_parameters
    )

    # Buffer tank (hot water storage for space heating)
    my_hot_water_storage_config = simple_water_storage.SimpleHotWaterStorageConfig.get_scaled_hot_water_storage(
        max_thermal_power_in_watt_of_heating_system=my_building_information.max_thermal_building_demand_in_watt,
        sizing_option=simple_water_storage.HotWaterStorageSizingEnum.SIZE_ACCORDING_TO_HEAT_PUMP,
    )
    my_hot_water_storage = simple_water_storage.SimpleHotWaterStorage(
        config=my_hot_water_storage_config, my_simulation_parameters=my_simulation_parameters
    )

    # Buffer remaining capacity until 60°C (Wh) for flexibility potential
    my_buffer_remaining = buffer_remaining_capacity.BufferRemainingCapacity(
        my_simulation_parameters=my_simulation_parameters,
        config=buffer_remaining_capacity.BufferRemainingCapacityConfig.get_default_config(
            building_name="BUI1",
            name="BufferRemainingCapacity60C",
            volume_heating_water_storage_in_liter=my_hot_water_storage_config.volume_heating_water_storage_in_liter,
            max_temperature_in_celsius=60.0,
        ),
    )

    # DHW storage
    my_dhw_storage_config = simple_water_storage.SimpleDHWStorageConfig.get_default_simpledhwstorage_config()
    my_dhw_storage_config.name = "DHWStorage"
    my_dhw_storage = simple_water_storage.SimpleDHWStorage(
        my_simulation_parameters=my_simulation_parameters, config=my_dhw_storage_config
    )

    # --- BP10: Solar thermal as primary DHW heat generator (pellet on secondary circuit) ---
    my_solar_thermal_system_config = solar_thermal_system.SolarThermalSystemConfig.get_default_solar_thermal_system(
        building_name="BUI1",
        coordinates=Coordinates(latitude=solar_site_latitude, longitude=solar_site_longitude),
        area_m2=solar_thermal_area_m2,
    )
    my_solar_thermal_system_config.name = "SolarThermalDHW_BP10"
    my_solar_thermal_system = solar_thermal_system.SolarThermalSystem(
        config=my_solar_thermal_system_config,
        my_simulation_parameters=my_simulation_parameters,
    )
    my_solar_thermal_system_controller_config = (
        solar_thermal_system.SolarThermalSystemControllerConfig.get_solar_thermal_system_controller_config()
    )
    my_solar_thermal_system_controller_config.name = "SolarThermalSystemController_BP10"
    my_solar_thermal_system_controller = solar_thermal_system.SolarThermalSystemController(
        my_simulation_parameters=my_simulation_parameters,
        config=my_solar_thermal_system_controller_config,
    )
    my_solar_thermal_system.connect_only_predefined_connections(
        my_weather, my_dhw_storage, my_solar_thermal_system_controller
    )
    my_solar_thermal_system_controller.connect_only_predefined_connections(my_dhw_storage, my_solar_thermal_system)

    # Pellet boiler sizing (ideal lookup)
    sizing_mode = (cli_overrides.get_override("HEATGEN_SIZING") or "IDEAL_LOOKUP").strip().upper()
    if sizing_mode == "IDEAL_LOOKUP":
        ideal_heating_power_in_watt = heating_system_selection.get_ideal_power_from_lookup(
            arch=arch_used, weather=weather_used
        )
        boiler_power_w = float(ideal_heating_power_in_watt)
        log.information(
            f"Using ideal lookup boiler sizing for ARCH={arch_used} WEATHER={weather_used}: "
            f"ideal {boiler_power_w / 1e3:.2f} kW."
        )
        cli_overrides.set_used_value("BOILER_NOMINAL_POWER_W", str(int(round(boiler_power_w))))
    else:
        boiler_power_w = float(my_building_information.max_thermal_building_demand_in_watt)

    my_oil_boiler_config = generic_boiler.GenericBoilerConfig.get_scaled_conventional_pellet_boiler_config(
        heating_load_of_building_in_watt=boiler_power_w,
        building_name="BUI1",
    )
    my_oil_boiler_config.name = "PelletBoiler"
    # Be explicit: ensure OPEX/CO2 use pellet factors.
    my_oil_boiler_config.energy_carrier = loadtypes.LoadTypes.PELLETS
    # BP10: allow true thermal off at low modulation (solar can cover DHW in summer).
    my_oil_boiler_config.minimal_thermal_power_in_watt = 0.0
    my_oil_boiler = generic_boiler.GenericBoiler(
        my_simulation_parameters=my_simulation_parameters,
        config=my_oil_boiler_config,
    )

    # Fuel meter (pellets) for operational cost/emission postprocessing aggregation
    my_fuel_meter_config = fuel_meter.FuelMeterConfig.get_fuel_meter_default_config(
        building_name="BUI1",
        fuel_loadtype=loadtypes.LoadTypes.PELLETS,
        heating_value_of_fuel_in_kwh_per_liter=my_oil_boiler.heating_value_of_fuel_in_kwh_per_liter,
        fuel_density_in_kg_per_m3=my_oil_boiler.fuel_density_in_kg_per_m3,
    )
    my_fuel_meter = fuel_meter.FuelMeter(
        my_simulation_parameters=my_simulation_parameters,
        config=my_fuel_meter_config,
    )
    my_oil_boiler_controller_config = (
        generic_boiler.GenericBoilerControllerConfig.get_default_modulating_generic_boiler_controller_config(
            minimal_thermal_power_in_watt=my_oil_boiler_config.minimal_thermal_power_in_watt,
            maximal_thermal_power_in_watt=my_oil_boiler_config.maximal_thermal_power_in_watt,
            building_name="BUI1",
            with_domestic_hot_water_preparation=True,
            set_heating_threshold_outside_temperature_in_celsius=16.0,
            secondary_mode=True,
        )
    )
    # Typical pellet boiler controller: long min run/rest; winter modulation floor 1/12 while on; summer floor 0
    # so the boiler can remain off when solar carries DHW (see module docstring).
    my_oil_boiler_controller_config.seasonal_modulation_minimum_enabled = True
    my_oil_boiler_controller_config.winter_modulation_minimum_thermal_power_in_watt = (
        my_oil_boiler_config.maximal_thermal_power_in_watt / 60.0
    )
    my_oil_boiler_controller_config.summer_modulation_minimum_thermal_power_in_watt = 0.0
    my_oil_boiler_controller_config.summer_modulation_daily_avg_outdoor_temperature_threshold_celsius = 16.0
    my_oil_boiler_controller_config.minimum_runtime_in_seconds = 120.0 * 60.0  # 2 h — pellet cannot stop mid-cycle earlier
    my_oil_boiler_controller_config.minimum_resting_time_in_seconds = 90.0 * 60.0  # 1.5 h between firing cycles
    my_oil_boiler_controller_config.name = "PelletBoilerController"
    my_oil_boiler_controller = generic_boiler.GenericBoilerController(
        my_simulation_parameters=my_simulation_parameters,
        config=my_oil_boiler_controller_config,
    )

    # KPI splits for postprocessing (aligned across HP/BO/BG/BP/GR setups):
    # - `HeatGeneratorTotalThermalPower`: space heating only (pellet SH).
    # - `HeatGeneratorPlantDhwThermalPower`: generator-side DHW from the pellet boiler (`ThermalOutputPowerDhw`).
    # - `SolarDhwThermalPower`: solar thermal delivered into the DHW store (primary heat generator path).
    my_heatgen_total_thermal_power = sumbuilder.SumBuilderForOneInput(
        my_simulation_parameters=my_simulation_parameters,
        config=sumbuilder.SumBuilderConfig(
            building_name="BUI1",
            name="HeatGeneratorTotalThermalPower",
            loadtype=loadtypes.LoadTypes.ANY,
            unit=loadtypes.Units.WATT,
        ),
    )
    my_heatgen_plant_dhw_thermal_power = sumbuilder.SumBuilderForOneInput(
        my_simulation_parameters=my_simulation_parameters,
        config=sumbuilder.SumBuilderConfig(
            building_name="BUI1",
            name="HeatGeneratorPlantDhwThermalPower",
            loadtype=loadtypes.LoadTypes.ANY,
            unit=loadtypes.Units.WATT,
        ),
    )
    my_solar_dhw_thermal_power = sumbuilder.SumBuilderForOneInput(
        my_simulation_parameters=my_simulation_parameters,
        config=sumbuilder.SumBuilderConfig(
            building_name="BUI1",
            name="SolarDhwThermalPower",
            loadtype=loadtypes.LoadTypes.ANY,
            unit=loadtypes.Units.WATT,
        ),
    )

    # Electricity meter
    my_electricity_meter = electricity_meter.ElectricityMeter(
        my_simulation_parameters=my_simulation_parameters,
        config=electricity_meter.ElectricityMeterConfig.get_electricity_meter_default_config(),
    )

    # Strict comfort-band demand logic (same thermostat logic as in basic_household)
    my_strict_comfort_controller = generic_heat_pump.GenericHeatPumpController(
        config=generic_heat_pump.GenericHeatPumpControllerConfig(
            building_name="BUI1",
            name="StrictComfortDemandController",
            temperature_air_heating_in_celsius=20.5,
            temperature_air_cooling_in_celsius=24.0,
            offset=0.5,
            # Mode 2 supports heating/cooling/off, but strict strategy + thresholds below disables cooling in practice.
            mode=2,
            use_adaptive_comfort_band=True,
            control_strategy="strict_comfort_band_v1",
            # Strict inner offsets: lower stricter than upper
            comfort_band_inner_offset_in_celsius=0.5,
            comfort_band_inner_offset_lower_in_celsius=1.0,
            comfort_band_inner_offset_upper_in_celsius=0.5,
            heating_disabled_above_running_mean_outdoor_temperature_in_celsius=18.0,
            # keep very high so strict controller never requests cooling
            cooling_enabled_above_running_mean_outdoor_temperature_in_celsius=99.0,
        ),
        my_simulation_parameters=my_simulation_parameters,
    )
    my_strict_comfort_controller.connect_only_predefined_connections(my_building)
    my_strict_comfort_controller.connect_input(
        my_strict_comfort_controller.ElectricityInput,
        my_electricity_meter.component_name,
        my_electricity_meter.ElectricityAvailable,
    )

    # Compute a modifier that aligns with the strict lower setpoint and adds recovery boost
    # when operative temperature is below the comfort lower bound.
    my_setpoint_modifier = strict_comfort_recovery_modifier.StrictComfortRecoveryModifier(
        my_simulation_parameters=my_simulation_parameters,
        config=strict_comfort_recovery_modifier.StrictComfortRecoveryModifierConfig.get_default_config(
            building_name="BUI1",
            name="StrictComfortSetpointModifier",
            base_heating_setpoint_in_celsius=20.5,
        ),
    )
    # Make recovery stronger to actually avoid underheating.
    my_setpoint_modifier.config.recovery_gain = 5.0
    my_setpoint_modifier.config.max_recovery_boost_in_celsius = 5.0
    my_setpoint_modifier.config.clamp_max_modifier_in_celsius = 15.0
    my_setpoint_modifier.connect_input(
        my_setpoint_modifier.StrictLowerSetpoint,
        my_strict_comfort_controller.component_name,
        my_strict_comfort_controller.AppliedControlLowerTemperature,
    )
    my_setpoint_modifier.connect_input(
        my_setpoint_modifier.OperativeTemperature,
        my_building.component_name,
        my_building.TemperatureOperative,
    )
    my_setpoint_modifier.connect_input(
        my_setpoint_modifier.ComfortLowerBound,
        my_building.component_name,
        my_building.TemperatureComfortLowerBound,
    )

    # Proportional comfort-band heating demand (TOP undershoot vs adaptive comfort lower bound).
    max_sh_power_w = float(my_building_information.max_thermal_building_demand_in_watt or 0.0)
    proportional_band_k = 0.5  # full request at 0.5 K below lower comfort bound
    my_comfort_heating_demand = comfort_band_heating_demand.ComfortBandHeatingDemand(
        my_simulation_parameters=my_simulation_parameters,
        config=comfort_band_heating_demand.ComfortBandHeatingDemandConfig.get_default_config(
            building_name="BUI1",
            name="ComfortBandHeatingDemand",
            max_heating_power_in_watt=max_sh_power_w,
            proportional_gain_in_watt_per_kelvin=(max_sh_power_w / max(proportional_band_k, 1e-6)),
        ),
    )
    my_comfort_heating_demand.connect_input(
        my_comfort_heating_demand.OperativeTemperature,
        my_building.component_name,
        my_building.TemperatureOperative,
    )
    my_comfort_heating_demand.connect_input(
        my_comfort_heating_demand.LowerComfortSetpoint,
        my_building.component_name,
        my_building.TemperatureComfortLowerBound,
    )
    my_comfort_heating_demand.connect_input(
        my_comfort_heating_demand.HeatingAllowed,
        my_strict_comfort_controller.component_name,
        my_strict_comfort_controller.ControlHeatingAllowed,
    )

    # Apply the same modifier to the heat distribution controller so that
    # required flow temperatures increase when strict comfort requires higher indoor temperatures.
    # Otherwise, the system may keep the buffer tank only barely warm (low flow temp),
    # causing too little heat delivery even if the building is below adaptive comfort.
    my_heat_distribution_controller.connect_input(
        my_heat_distribution_controller.BuildingTemperatureModifier,
        my_setpoint_modifier.component_name,
        my_setpoint_modifier.TemperatureModifier,
    )

    # Flexibility potentials (comfort-band headroom + buffer stored energy + derived COP)
    my_flex_potential = flexibility_potential.FlexibilityPotential(
        my_simulation_parameters=my_simulation_parameters,
        config=flexibility_potential.FlexibilityPotentialConfig(
            building_name="BUI1",
            name="FlexibilityPotential",
            fallback_heating_cop=1.0,
            fallback_cooling_eer=0.0,
        ),
    )

    # =============================================================================================================================
    # Connect components

    # Building: weather defaults; occupancy depends on chosen mode
    my_building.connect_only_predefined_connections(my_weather)
    if occ_mode == "SIA2024":
        my_building.connect_input(
            my_building.HeatingByResidents,
            my_occupancy.component_name,
            my_occupancy.HeatingByResidents,
        )
        my_building.connect_input(
            my_building.HeatingByDevices,
            my_occupancy.component_name,
            my_occupancy.HeatingByDevices,
        )
        my_building.connect_input(
            my_building.NumberOfResidents,
            my_occupancy.component_name,
            my_occupancy.NumberOfResidents,
        )
    else:
        my_building.connect_only_predefined_connections(my_occupancy)
    my_building.connect_input(
        my_building.BuildingTemperatureModifier,
        my_setpoint_modifier.component_name,
        my_setpoint_modifier.TemperatureModifier,
    )

    # Building gets thermal power via HDS
    my_building.connect_input(
        my_building.ThermalPowerDelivered,
        my_heat_distribution.component_name,
        my_heat_distribution.ThermalPowerDelivered,
    )

    # HeatDistributionController: connect weather by default; connect heating-only demand explicitly
    # so no cooling mode is considered in BP10.
    my_heat_distribution_controller.connect_only_predefined_connections(my_weather)
    my_heat_distribution_controller.connect_input(
        my_heat_distribution_controller.TheoreticalThermalBuildingDemand,
        my_comfort_heating_demand.component_name,
        my_comfort_heating_demand.HeatingDemand,
    )
    my_heat_distribution.connect_only_predefined_connections(
        my_building, my_heat_distribution_controller, my_hot_water_storage
    )

    # Oil boiler controller needs weather + storages + HDS controller (for target flow temperature)
    my_oil_boiler_controller.connect_only_predefined_connections(
        my_weather, my_hot_water_storage, my_dhw_storage, my_heat_distribution_controller
    )
    # Oil boiler connects to controller + storages
    my_oil_boiler.connect_only_predefined_connections(my_oil_boiler_controller, my_hot_water_storage, my_dhw_storage)

    my_heatgen_total_thermal_power.connect_input(
        my_heatgen_total_thermal_power.SumInput1,
        my_oil_boiler.component_name,
        my_oil_boiler.ThermalPowerGenerationSh,
    )
    my_heatgen_plant_dhw_thermal_power.connect_input(
        my_heatgen_plant_dhw_thermal_power.SumInput1,
        my_oil_boiler.component_name,
        my_oil_boiler.ThermalOutputPowerDhw,
    )
    my_solar_dhw_thermal_power.connect_input(
        my_solar_dhw_thermal_power.SumInput1,
        my_dhw_storage.component_name,
        my_dhw_storage.ThermalPowerFromHeatGenerator,
    )

    # Buffer tank: explicit connections used by the HDS / boiler
    my_hot_water_storage.connect_input(
        my_hot_water_storage.WaterTemperatureFromHeatDistribution,
        my_heat_distribution.component_name,
        my_heat_distribution.WaterTemperatureOutput,
    )
    my_hot_water_storage.connect_input(
        my_hot_water_storage.WaterTemperatureFromHeatGenerator,
        my_oil_boiler.component_name,
        my_oil_boiler.WaterOutputTemperatureSh,
    )
    my_hot_water_storage.connect_input(
        my_hot_water_storage.WaterMassFlowRateFromHeatGenerator,
        my_oil_boiler.component_name,
        my_oil_boiler.WaterOutputMassFlowSh,
    )

    # DHW storage: solar primary, pellet secondary; occupancy water draw.
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterTemperatureFromHeatGenerator,
        my_solar_thermal_system.component_name,
        my_solar_thermal_system.WaterTemperatureOutput,
    )
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterMassFlowRateFromHeatGenerator,
        my_solar_thermal_system.component_name,
        my_solar_thermal_system.WaterMassFlowOutput,
    )
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterTemperatureFromSecondaryHeatGenerator,
        my_oil_boiler.component_name,
        my_oil_boiler.WaterOutputTemperatureDhw,
    )
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterMassFlowRateFromSecondaryHeatGenerator,
        my_oil_boiler.component_name,
        my_oil_boiler.WaterOutputMassFlowDhw,
    )
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterConsumption,
        my_occupancy.component_name,
        my_occupancy.WaterConsumption,
    )

    # Electricity grid aggregation (occupancy, solar thermal pump; no PV in this variant)
    my_electricity_meter.add_component_input_and_connect(
        source_object_name=my_occupancy.component_name,
        source_component_output=my_occupancy.ElectricalPowerConsumption,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[loadtypes.InandOutputType.ELECTRICITY_CONSUMPTION_UNCONTROLLED],
        source_weight=999,
    )
    my_electricity_meter.add_component_input_and_connect(
        source_object_name=my_solar_thermal_system.component_name,
        source_component_output=my_solar_thermal_system.ElectricityConsumptionOutput,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[loadtypes.InandOutputType.ELECTRICITY_CONSUMPTION_UNCONTROLLED],
        source_weight=999,
    )

    # FlexibilityPotential connections
    my_flex_potential.connect_input(
        my_flex_potential.PotentialHeatingEnergyUntilUpperComfortBound,
        my_building.component_name,
        my_building.PotentialHeatingEnergyUntilUpperComfortBound,
    )
    my_flex_potential.connect_input(
        my_flex_potential.PotentialCoolingEnergyUntilLowerComfortBound,
        my_building.component_name,
        my_building.PotentialCoolingEnergyUntilLowerComfortBound,
    )
    my_flex_potential.connect_input(
        my_flex_potential.BufferStoredEnergy,
        my_hot_water_storage.component_name,
        my_hot_water_storage.ThermalEnergyInStorage,
    )
    my_buffer_remaining.connect_input(
        my_buffer_remaining.WaterMeanTemperatureInStorage,
        my_hot_water_storage.component_name,
        my_hot_water_storage.WaterMeanTemperatureInStorage,
    )
    my_flex_potential.connect_input(
        my_flex_potential.BufferRemainingCapacity,
        my_buffer_remaining.component_name,
        my_buffer_remaining.BufferRemainingCapacity,
    )
    my_flex_potential.connect_input(
        my_flex_potential.HvacHeatingPower,
        my_oil_boiler.component_name,
        my_oil_boiler.ThermalPowerGenerationSh,
    )

    # =============================================================================================================================
    # Add components
    my_sim.add_component(my_occupancy)
    my_sim.add_component(my_weather)
    my_sim.add_component(my_electricity_meter)
    my_sim.add_component(my_building)
    my_sim.add_component(my_strict_comfort_controller)
    my_sim.add_component(my_comfort_heating_demand)
    my_sim.add_component(my_setpoint_modifier)
    my_sim.add_component(my_heat_distribution_controller)
    my_sim.add_component(my_heat_distribution)
    my_sim.add_component(my_hot_water_storage)
    my_sim.add_component(my_buffer_remaining)
    my_sim.add_component(my_dhw_storage)
    my_sim.add_component(my_solar_thermal_system)
    my_sim.add_component(my_solar_thermal_system_controller)
    my_sim.add_component(my_oil_boiler_controller)
    my_sim.add_component(my_oil_boiler)
    my_sim.add_component(my_fuel_meter, connect_automatically=True)
    my_sim.add_component(my_heatgen_total_thermal_power)
    my_sim.add_component(my_heatgen_plant_dhw_thermal_power)
    my_sim.add_component(my_solar_dhw_thermal_power)
    my_sim.add_component(my_flex_potential)

