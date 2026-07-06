"""BO04 household system setup (BO03 + battery + electric vehicle).

This is based on `hp01`, but replaces the heat pump with a conventional oil boiler
sized via the same ideal lookup sizing.
"""

from __future__ import annotations

from typing import Any, Optional

from hisim import cli_overrides, heating_system_selection, loadtypes, log
from hisim.simulator import SimulationParameters

from hisim.components import electricity_meter
from hisim.components import flexibility_potential
from hisim.components import heat_distribution_system
from hisim.components import loadprofilegenerator_utsp_connector
from hisim.components import generic_boiler
from hisim.components import fuel_meter
from hisim.components import air_conditioner
from hisim.components import simple_water_storage
from hisim.components import sia2024_occupancy
from hisim.components import generic_heat_pump
from hisim.components import strict_comfort_recovery_modifier
from hisim.components import buffer_remaining_capacity
from hisim.components import comfort_band_heating_demand
from hisim.components import comfort_band_cooling_demand
from hisim.components import generic_pv_system
from hisim.components import advanced_battery_bslib
from hisim.components import advanced_ev_battery_bslib
from hisim.components import controller_l1_generic_ev_charge
from hisim.components import controller_l2_energy_management_system
from hisim.components import generic_car
from hisim.components import stored_energy_from_soc
from hisim.components import sumbuilder
from hisim.components import weather
from hisim.components import building


def setup_function(my_sim: Any, my_simulation_parameters: Optional[SimulationParameters] = None) -> None:  # noqa: too-many-statements
    """Set up BO04.

    - Conventional oil boiler for space heating + DHW
    - Buffer tank (SimpleHotWaterStorage)
    - Split AC unit for cooling (AirConditioner component)
    - PV system (scaled by roof area)
    - Stationary battery + EV (controlled via L2 EMS, reference: HP04)
    - Keeps basic-household CLI overrides (ARCH/WEATHER, batch explorer suppression) and occupancy choice (SIA2024/LPG)
    """

    # =============================================================================================================================
    # Set simulation parameters
    year = cli_overrides.get_economic_year()
    seconds_per_timestep = 900.0  # 15 min timesteps (3600s would be 1h, but can cause stability issues with large HP time constants)

    if my_simulation_parameters is None:
        my_simulation_parameters = SimulationParameters.full_year_with_minimal_variant_artifacts(
            year=year, seconds_per_timestep=seconds_per_timestep
        )

    cli_overrides.apply_batch_open_explorer_setting(my_simulation_parameters)

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
    # Cooling enabled (same baseline as HP02; adaptive comfort band is handled by the controller logic).
    my_building_config.set_cooling_temperature_in_celsius = 25.0
    cli_overrides.apply_swiss_sia_natural_ventilation_settings(my_building_config)

    my_building_information = building.BuildingInformation(config=my_building_config)
    my_building = building.Building(config=my_building_config, my_simulation_parameters=my_simulation_parameters)

    # =============================================================================================================================
    # Build Occupancy (SIA2024 or LPG/UTSP)
    occ_mode = (cli_overrides.get_override("OCC") or "SIA2024").strip().upper()
    cli_overrides.set_used_value("OCC", occ_mode)
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

    # =============================================================================================================================
    # PV system (reference: HP03)
    # Use a scaled PV system based on roof area and the selected weather location.
    try:
        pv_location = weather_used
    except Exception:
        pv_location = default_weather
    my_pv_config = generic_pv_system.PVSystemConfig.get_scaled_pv_system(
        rooftop_area_in_m2=my_building_information.roof_area_in_m2,
        location=pv_location,
    )
    my_pv = generic_pv_system.PVSystem(
        config=my_pv_config,
        my_simulation_parameters=my_simulation_parameters,
    )
    my_pv.connect_only_predefined_connections(my_weather)

    # =============================================================================================================================
    # Battery + Electric Vehicle (via EMS) (reference: HP04)
    my_ems = controller_l2_energy_management_system.L2GenericEnergyManagementSystem(
        my_simulation_parameters=my_simulation_parameters,
        config=controller_l2_energy_management_system.EMSConfig.get_default_config_ems(
            building_name="BUI1",
            name="L2EMSElectricityController",
        ),
    )

    my_battery_config = advanced_battery_bslib.BatteryConfig.get_scaled_battery(
        total_pv_power_in_watt_peak=my_pv_config.power_in_watt,
        building_name="BUI1",
        name="Battery",
    )
    my_battery = advanced_battery_bslib.Battery(
        my_simulation_parameters=my_simulation_parameters,
        config=my_battery_config,
    )

    my_cars: list[generic_car.Car] = []
    my_car_batteries: list[advanced_ev_battery_bslib.CarBattery] = []
    my_car_battery_controllers: list[controller_l1_generic_ev_charge.L1Controller] = []

    car_schedule_mode = (cli_overrides.get_override("CAR_SCHEDULE") or "LPG").strip().upper()
    cli_overrides.set_used_value("CAR_SCHEDULE", car_schedule_mode)
    # Supported:
    # - "" / "AUTO": use detailed LPG schedule only if available from main occupancy
    # - "LPG": always use detailed LPG mobility schedule (even when OCC=SIA2024)
    # - "DEFAULT": simple fallback EV (always home, no driving)
    if car_schedule_mode in ("", "AUTO"):
        if hasattr(my_occupancy, "car_data_dict"):
            my_car_information = generic_car.GenericCarInformation(my_occupancy_instance=my_occupancy)  # type: ignore[arg-type]
            car_information_dicts = list(my_car_information.data_dict_for_car_component.values())
        else:
            car_schedule_mode = "DEFAULT"

    if car_schedule_mode == "LPG" and not hasattr(my_occupancy, "car_data_dict"):
        my_mobility_occupancy_config = loadprofilegenerator_utsp_connector.UtspLpgConnectorConfig.get_default_utsp_connector_config(
            building_name="BUI1"
        )
        my_mobility_occupancy_config.name = "UTSPConnectorMobilityOnly"
        my_mobility_occupancy_config.data_acquisition_mode = (
            loadprofilegenerator_utsp_connector.LpgDataAcquisitionMode.USE_LOCAL_LPG
        )
        my_mobility_occupancy = loadprofilegenerator_utsp_connector.UtspLpgConnector(
            config=my_mobility_occupancy_config, my_simulation_parameters=my_simulation_parameters
        )
        try:
            my_car_information = generic_car.GenericCarInformation(my_occupancy_instance=my_mobility_occupancy)
            car_information_dicts = list(my_car_information.data_dict_for_car_component.values())
            log.information(
                "Using detailed LPG mobility schedule for EV while keeping SIA2024 for household occupancy."
            )
        except ValueError as exc:
            log.warning(
                "Requested detailed LPG mobility schedule for EV (CAR_SCHEDULE=LPG), "
                "but no mobility data was available from the selected LPG data source. "
                "Falling back to default EV profile (always home, zero driving)."
            )
            log.warning(f"Mobility schedule fallback reason: {exc}")
            car_schedule_mode = "DEFAULT"

    if car_schedule_mode == "DEFAULT":
        steps_desired = int(my_simulation_parameters.timesteps)
        minutes_per_timestep = int(float(my_simulation_parameters.seconds_per_timestep) / 60.0)
        steps_desired_in_minutes = steps_desired * minutes_per_timestep
        car_information_dicts = [
            {
                "car_name": "ElectricCar",
                "household_name": "Default",
                "time_resolution": "00:01:00",
                "car_location": ["Home"] * steps_desired_in_minutes,
                "driven_meters": [0.0] * steps_desired_in_minutes,
            }
        ]

    car_number = 1
    for car_information_dict in car_information_dicts:
        my_car_config = generic_car.CarConfig.get_default_ev_config(building_name="BUI1")
        my_car_config.name = car_information_dict["car_name"] + f"_{car_number}"
        my_car = generic_car.Car(
            my_simulation_parameters=my_simulation_parameters,
            config=my_car_config,
            data_dict_with_car_information=car_information_dict,
        )
        my_cars.append(my_car)

        my_car_battery_config = advanced_ev_battery_bslib.CarBatteryConfig.get_default_config(
            building_name="BUI1",
            name=f"CarBattery_{car_number}",
        )
        my_car_battery_config.source_weight = 4
        my_car_battery = advanced_ev_battery_bslib.CarBattery(
            my_simulation_parameters=my_simulation_parameters,
            config=my_car_battery_config,
        )
        my_car_batteries.append(my_car_battery)

        my_car_battery_controller_config = controller_l1_generic_ev_charge.ChargingStationConfig.get_default_config(
            building_name="BUI1"
        )
        my_car_battery_controller_config.name = f"L1EVChargeControl_{car_number}"
        my_car_battery_controller_config.source_weight = 4
        my_car_battery_controller = controller_l1_generic_ev_charge.L1Controller(
            my_simulation_parameters=my_simulation_parameters,
            config=my_car_battery_controller_config,
        )
        my_car_battery_controllers.append(my_car_battery_controller)
        car_number += 1

    # Oil boiler sizing (ideal lookup)
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
        chosen_hp_for_ac_sizing = heating_system_selection.pick_heat_pump_closest_to_ideal(
            ideal_power_in_watt=ideal_heating_power_in_watt
        )
        ac_sizing_power_w = float(chosen_hp_for_ac_sizing.nominal_heating_power_in_watt)
        log.information(
            f"Split AC sized to discrete heat pump nominal {ac_sizing_power_w / 1e3:.2f} kW "
            f"({chosen_hp_for_ac_sizing.manufacturer} / {chosen_hp_for_ac_sizing.name}, same rule as HP02)."
        )
        cli_overrides.set_used_value("AC_SIZING_POWER_W", str(int(round(ac_sizing_power_w))))
    else:
        boiler_power_w = float(my_building_information.max_thermal_building_demand_in_watt)
        ac_sizing_power_w = float(my_building_information.max_thermal_building_demand_in_watt)
        cli_overrides.set_used_value("AC_SIZING_POWER_W", str(int(round(ac_sizing_power_w))))

    my_oil_boiler_config = generic_boiler.GenericBoilerConfig.get_scaled_conventional_oil_boiler_config(
        heating_load_of_building_in_watt=boiler_power_w,
        building_name="BUI1",
    )
    my_oil_boiler_config.name = "OilBoiler"
    my_oil_boiler = generic_boiler.GenericBoiler(
        my_simulation_parameters=my_simulation_parameters,
        config=my_oil_boiler_config,
    )

    # Fuel meter (oil) for operational cost/emission postprocessing aggregation
    my_fuel_meter_config = fuel_meter.FuelMeterConfig.get_fuel_meter_default_config(
        building_name="BUI1",
        fuel_loadtype=loadtypes.LoadTypes.OIL,
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
        )
    )
    my_oil_boiler_controller_config.name = "OilBoilerController"
    my_oil_boiler_controller = generic_boiler.GenericBoilerController(
        my_simulation_parameters=my_simulation_parameters,
        config=my_oil_boiler_controller_config,
    )

    # Split AC for cooling (and potentially heating, but we configure it cooling-only here).
    # `heating_load` for catalogue scaling matches HP02: discrete HP nominal under IDEAL_LOOKUP, else Tabula peak.
    my_air_conditioner_config = air_conditioner.AirConditionerConfig.get_scaled_air_conditioner_config(
        heating_load=float(ac_sizing_power_w),
        heating_reference_temperature=float(my_building_information.heating_reference_temperature_in_celsius),
        building_name="BUI1",
    )
    my_air_conditioner_config.name = "SplitAC"
    my_air_conditioner = air_conditioner.AirConditioner(
        my_simulation_parameters=my_simulation_parameters,
        config=my_air_conditioner_config,
    )

    my_air_conditioner_controller_config = air_conditioner.AirConditionerControllerConfig.get_default_air_conditioner_controller_config(
        building_name="BUI1"
    )
    my_air_conditioner_controller_config.name = "SplitACController"
    # Cooling-only: keep heating setpoint far below any realistic indoor temperature.
    my_air_conditioner_controller_config.heating_set_temperature_deg_c = 0.0
    my_air_conditioner_controller_config.cooling_set_temperature_deg_c = 25.0
    my_air_conditioner_controller_config.minimum_modulation = 0.0
    my_air_conditioner_controller = air_conditioner.AirConditionerController(
        my_simulation_parameters=my_simulation_parameters,
        config=my_air_conditioner_controller_config,
    )

    ac_cooling_cap_w = heating_system_selection.get_cooling_plant_cap_from_hp_nominal(ac_sizing_power_w)
    log.information(
        f"Split AC cooling plant cap {ac_cooling_cap_w / 1e3:.2f} kW "
        f"({heating_system_selection.DEFAULT_COOLING_PLANT_NOMINAL_FRACTION:.0%} of HP nominal "
        f"{ac_sizing_power_w / 1e3:.2f} kW, aligned with HP cooling KPI scale)."
    )
    cli_overrides.set_used_value("COOLING_PLANT_CAP_W", str(int(round(ac_cooling_cap_w))))
    ac_cooling_p_gain = heating_system_selection.get_cooling_comfort_proportional_gain_w_per_k(
        ac_cooling_cap_w
    )
    my_comfort_cooling_demand = comfort_band_cooling_demand.ComfortBandCoolingDemand(
        my_simulation_parameters=my_simulation_parameters,
        config=comfort_band_cooling_demand.ComfortBandCoolingDemandConfig.get_default_config(
            building_name="BUI1",
            name="ComfortBandCoolingDemand",
            max_cooling_power_in_watt=float(ac_cooling_cap_w),
            proportional_gain_in_watt_per_kelvin=ac_cooling_p_gain,
            relaxation_factor=heating_system_selection.DEFAULT_COOLING_COMFORT_RELAXATION_FACTOR,
            theoretical_blend=heating_system_selection.DEFAULT_COOLING_THEORETICAL_BLEND,
        ),
    )

    # KPI splits for postprocessing (aligned across HP/BO/BG/BP/GR setups):
    # - `HeatGeneratorTotalThermalPower`: space heating + split-AC cooling (negative during cooling); no DHW.
    # - `HeatGeneratorPlantDhwThermalPower`: generator-side DHW (fossil / HP / district).
    # - `SolarDhwThermalPower`: solar thermal into DHW (0 W here — no solar primary on DHW).
    my_heatgen_total_thermal_power = sumbuilder.SumBuilderForTwoInputs(
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
    my_solar_dhw_thermal_power = sumbuilder.ConstantThermalPowerOutput(
        my_simulation_parameters=my_simulation_parameters,
        config=sumbuilder.ConstantThermalPowerConfig(
            building_name="BUI1",
            name="SolarDhwThermalPower",
            value_watt=0.0,
            loadtype=loadtypes.LoadTypes.ANY,
            unit=loadtypes.Units.WATT,
        ),
    )

    # Sum of heating (HDS) and cooling (AC) delivered to the building.
    # AC delivers negative thermal power during cooling, so summing works out-of-the-box.
    my_thermal_power_sum = sumbuilder.SumBuilderForTwoInputs(
        my_simulation_parameters=my_simulation_parameters,
        config=sumbuilder.SumBuilderConfig(
            building_name="BUI1",
            name="BuildingThermalPowerSum",
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
            mode=2,
            use_adaptive_comfort_band=True,
            control_strategy="strict_comfort_band_v1",
            # Strict inner offsets: lower stricter than upper
            comfort_band_inner_offset_in_celsius=0.5,
            comfort_band_inner_offset_lower_in_celsius=1.0,
            comfort_band_inner_offset_upper_in_celsius=0.5,
            heating_disabled_above_running_mean_outdoor_temperature_in_celsius=18.0,
            # Match HP02: enable cooling when 48h running-mean outdoor temp is high enough
            cooling_enabled_above_running_mean_outdoor_temperature_in_celsius=22.0,
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

    # Building gets net thermal power (heating + cooling) via sum builder
    my_building.connect_input(
        my_building.ThermalPowerDelivered,
        my_thermal_power_sum.component_name,
        my_thermal_power_sum.SumOutput,
    )

    # HeatDistributionController: connect weather by default; connect heating-only demand explicitly
    # so the hydronic heat distribution remains heating-only (cooling is handled by the split AC).
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
    my_heatgen_total_thermal_power.connect_input(
        my_heatgen_total_thermal_power.SumInput2,
        my_air_conditioner.component_name,
        my_air_conditioner.ThermalPowerDelivered,
    )
    my_heatgen_plant_dhw_thermal_power.connect_input(
        my_heatgen_plant_dhw_thermal_power.SumInput1,
        my_oil_boiler.component_name,
        my_oil_boiler.ThermalOutputPowerDhw,
    )

    # Split AC controller reads indoor temperature from building
    my_air_conditioner_controller.connect_only_predefined_connections(my_building)
    # Use the same adaptive cooling upper setpoint logic as in HP02 (48h running-mean dependent)
    my_air_conditioner_controller.connect_input(
        my_air_conditioner_controller.CoolingSetpoint,
        my_strict_comfort_controller.component_name,
        my_strict_comfort_controller.AppliedControlUpperTemperature,
    )
    my_air_conditioner_controller.connect_input(
        my_air_conditioner_controller.CoolingAllowed,
        my_strict_comfort_controller.component_name,
        my_strict_comfort_controller.ControlCoolingAllowed,
    )

    my_comfort_cooling_demand.connect_input(
        my_comfort_cooling_demand.OperativeTemperature,
        my_building.component_name,
        my_building.TemperatureOperative,
    )
    my_comfort_cooling_demand.connect_input(
        my_comfort_cooling_demand.UpperComfortSetpoint,
        my_strict_comfort_controller.component_name,
        my_strict_comfort_controller.AppliedControlUpperTemperature,
    )
    my_comfort_cooling_demand.connect_input(
        my_comfort_cooling_demand.CoolingAllowed,
        my_strict_comfort_controller.component_name,
        my_strict_comfort_controller.ControlCoolingAllowed,
    )
    my_comfort_cooling_demand.connect_input(
        my_comfort_cooling_demand.TheoreticalCoolingDemandFromBuilding,
        my_building.component_name,
        my_building.TheoreticalCoolingDemand,
    )

    # Split AC connects to weather + its controller (defaults exist for both)
    my_air_conditioner.connect_only_predefined_connections(my_weather, my_air_conditioner_controller)
    my_air_conditioner.connect_input(
        my_air_conditioner.CoolingPowerRequestLimit,
        my_comfort_cooling_demand.component_name,
        my_comfort_cooling_demand.CoolingDemand,
    )

    # Sum builder inputs: heat distribution (heating) + split AC (cooling)
    my_thermal_power_sum.connect_input(
        my_thermal_power_sum.SumInput1,
        my_heat_distribution.component_name,
        my_heat_distribution.ThermalPowerDelivered,
    )
    my_thermal_power_sum.connect_input(
        my_thermal_power_sum.SumInput2,
        my_air_conditioner.component_name,
        my_air_conditioner.ThermalPowerDelivered,
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

    # DHW storage: connect occupancy (water draw) + boiler DHW outputs.
    # `SimpleDHWStorage` provides default connections for UTSP, but not for SIA2024, so we connect explicitly.
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterTemperatureFromHeatGenerator,
        my_oil_boiler.component_name,
        my_oil_boiler.WaterOutputTemperatureDhw,
    )
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterMassFlowRateFromHeatGenerator,
        my_oil_boiler.component_name,
        my_oil_boiler.WaterOutputMassFlowDhw,
    )
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterConsumption,
        my_occupancy.component_name,
        my_occupancy.WaterConsumption,
    )

    # -----------------------------------------------------------------------------------------------------------------
    # Connect EV components (reference: HP04)
    for car, car_battery, car_battery_controller in zip(my_cars, my_car_batteries, my_car_battery_controllers):
        car_battery_controller.connect_only_predefined_connections(car)
        car_battery_controller.connect_only_predefined_connections(car_battery)
        car_battery.connect_only_predefined_connections(car_battery_controller)

        my_ems.add_component_input_and_connect(
            source_object_name=car_battery_controller.component_name,
            source_component_output=car_battery_controller.BatteryChargingPowerToEMS,
            source_load_type=loadtypes.LoadTypes.ELECTRICITY,
            source_unit=loadtypes.Units.WATT,
            source_tags=[
                loadtypes.ComponentType.CAR_BATTERY,
                loadtypes.InandOutputType.ELECTRICITY_CONSUMPTION_EMS_CONTROLLED,
            ],
            source_weight=4,
        )
        ev_electricity_target = my_ems.add_component_output(
            source_output_name=loadtypes.InandOutputType.ELECTRICITY_TARGET,
            source_tags=[
                loadtypes.ComponentType.CAR_BATTERY,
                loadtypes.InandOutputType.ELECTRICITY_TARGET,
            ],
            source_weight=4,
            source_load_type=loadtypes.LoadTypes.ELECTRICITY,
            source_unit=loadtypes.Units.WATT,
            output_description="Target electricity for EV Battery Controller.",
        )
        car_battery_controller.connect_dynamic_input(
            input_fieldname=controller_l1_generic_ev_charge.L1Controller.ElectricityTarget,
            src_object=ev_electricity_target,
        )

    # -----------------------------------------------------------------------------------------------------------------
    # Connect EMS with fixed consumers/producers (used only for surplus balance)
    my_ems.add_component_input_and_connect(
        source_object_name=my_occupancy.component_name,
        source_component_output=my_occupancy.ElectricalPowerConsumption,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[loadtypes.InandOutputType.ELECTRICITY_CONSUMPTION_UNCONTROLLED],
        source_weight=999,
    )
    my_ems.add_component_input_and_connect(
        source_object_name=my_pv.component_name,
        source_component_output=my_pv.ElectricityOutput,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[
            loadtypes.ComponentType.PV,
            loadtypes.InandOutputType.ELECTRICITY_PRODUCTION,
        ],
        source_weight=999,
    )
    my_ems.add_component_input_and_connect(
        source_object_name=my_air_conditioner.component_name,
        source_component_output=my_air_conditioner.ElectricalPowerConsumption,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[loadtypes.InandOutputType.ELECTRICITY_CONSUMPTION_UNCONTROLLED],
        source_weight=999,
    )

    # -----------------------------------------------------------------------------------------------------------------
    # Connect EMS with stationary battery (flexible)
    my_ems.add_component_input_and_connect(
        source_object_name=my_battery.component_name,
        source_component_output=my_battery.AcBatteryPowerUsed,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[loadtypes.ComponentType.BATTERY, loadtypes.InandOutputType.ELECTRICITY_CONSUMPTION_EMS_CONTROLLED],
        source_weight=5,
    )
    battery_target = my_ems.add_component_output(
        source_output_name=loadtypes.InandOutputType.ELECTRICITY_TARGET,
        source_tags=[
            loadtypes.ComponentType.BATTERY,
            loadtypes.InandOutputType.ELECTRICITY_TARGET,
        ],
        source_weight=5,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        output_description="Target electricity for Battery Control.",
    )
    my_battery.connect_dynamic_input(
        input_fieldname=advanced_battery_bslib.Battery.LoadingPowerInput,
        src_object=battery_target,
    )

    # -----------------------------------------------------------------------------------------------------------------
    # Electricity meter: measure net grid exchange (from EMS)
    my_electricity_meter.add_component_input_and_connect(
        source_object_name=my_ems.component_name,
        source_component_output=my_ems.TotalElectricityToOrFromGrid,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[loadtypes.InandOutputType.ELECTRICITY_PRODUCTION],
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
        my_flex_potential.PvElectricityEnergy,
        my_pv.component_name,
        my_pv.ElectricityEnergyOutput,
    )
    my_flex_potential.connect_input(
        my_flex_potential.HvacHeatingPower,
        my_oil_boiler.component_name,
        my_oil_boiler.ThermalPowerGenerationSh,
    )

    # Cooling device signals for deriving EER/COP conversion from electricity (signed thermal power is supported).
    my_flex_potential.connect_input(
        my_flex_potential.HvacCoolingPower,
        my_air_conditioner.component_name,
        my_air_conditioner.ThermalPowerDelivered,
    )
    my_flex_potential.connect_input(
        my_flex_potential.HvacElectricPower,
        my_air_conditioner.component_name,
        my_air_conditioner.ElectricalPowerConsumption,
    )

    # Stored electricity in battery / EV (Wh) for flexibility potentials (reference: HP04)
    my_battery_energy_wh = stored_energy_from_soc.StoredEnergyFromSoc(
        my_simulation_parameters=my_simulation_parameters,
        config=stored_energy_from_soc.StoredEnergyFromSocConfig(
            building_name="BUI1",
            name="BatteryStoredEnergyWh",
            capacity_in_kwh=float(my_battery_config.custom_battery_capacity_generic_in_kilowatt_hour or 0.0),
        ),
    )
    my_battery_energy_wh.connect_input(
        my_battery_energy_wh.StateOfCharge,
        my_battery.component_name,
        my_battery.StateOfCharge,
    )
    my_flex_potential.connect_input(
        my_flex_potential.BatteryStoredEnergy,
        my_battery_energy_wh.component_name,
        my_battery_energy_wh.StoredEnergy,
    )

    my_ev_energy_wh_sum = sumbuilder.CalculateOperation(
        my_simulation_parameters=my_simulation_parameters,
        config=sumbuilder.SumBuilderConfig(
            building_name="BUI1",
            name="EvStoredEnergyWhSum",
            loadtype=loadtypes.LoadTypes.ELECTRICITY,
            unit=loadtypes.Units.WATT_HOUR,
        ),
    )
    for idx, car_battery in enumerate(my_car_batteries):
        my_ev_energy_wh = stored_energy_from_soc.StoredEnergyFromSoc(
            my_simulation_parameters=my_simulation_parameters,
            config=stored_energy_from_soc.StoredEnergyFromSocConfig(
                building_name="BUI1",
                name=f"EvStoredEnergyWh_{idx+1}",
                capacity_in_kwh=float(getattr(car_battery.config, "e_bat_custom", 0.0) or 0.0),
            ),
        )
        my_ev_energy_wh.connect_input(
            my_ev_energy_wh.StateOfCharge,
            car_battery.component_name,
            car_battery.StateOfCharge,
        )
        my_sim.add_component(my_ev_energy_wh)
        my_ev_energy_wh_sum.connect_arbitrary_input(
            src_object_name=my_ev_energy_wh.component_name,
            src_field_name=my_ev_energy_wh.StoredEnergy,
        )
        if idx >= 1:
            my_ev_energy_wh_sum.add_operation("Sum")

    my_flex_potential.connect_input(
        my_flex_potential.EvStoredEnergy,
        my_ev_energy_wh_sum.component_name,
        my_ev_energy_wh_sum.Output,
    )

    # =============================================================================================================================
    # Add components
    my_sim.add_component(my_occupancy)
    my_sim.add_component(my_weather)
    my_sim.add_component(my_pv)
    my_sim.add_component(my_electricity_meter)
    my_sim.add_component(my_ems)
    my_sim.add_component(my_battery)
    my_sim.add_component(my_building)
    my_sim.add_component(my_strict_comfort_controller)
    my_sim.add_component(my_comfort_heating_demand)
    my_sim.add_component(my_setpoint_modifier)
    my_sim.add_component(my_heat_distribution_controller)
    my_sim.add_component(my_heat_distribution)
    my_sim.add_component(my_air_conditioner_controller)
    my_sim.add_component(my_comfort_cooling_demand)
    my_sim.add_component(my_air_conditioner)
    my_sim.add_component(my_thermal_power_sum)
    my_sim.add_component(my_heatgen_total_thermal_power)
    my_sim.add_component(my_heatgen_plant_dhw_thermal_power)
    my_sim.add_component(my_solar_dhw_thermal_power)
    my_sim.add_component(my_hot_water_storage)
    my_sim.add_component(my_buffer_remaining)
    my_sim.add_component(my_dhw_storage)
    my_sim.add_component(my_oil_boiler_controller)
    my_sim.add_component(my_oil_boiler)
    my_sim.add_component(my_fuel_meter, connect_automatically=True)
    my_sim.add_component(my_flex_potential)
    my_sim.add_component(my_battery_energy_wh)
    my_sim.add_component(my_ev_energy_wh_sum)

    for car in my_cars:
        my_sim.add_component(car)
    for car_battery in my_car_batteries:
        my_sim.add_component(car_battery)
    for car_battery_controller in my_car_battery_controllers:
        my_sim.add_component(car_battery_controller)

