"""HP04 household system setup (HP03 + battery + electric vehicle).

HP04 is based on `hp03` (advanced HP + buffer + DHW + cooling + PV + strict comfort recovery),
and adds:
- a stationary battery (bslib) controlled via an L2 EMS
- an electric vehicle (generic car + EV battery + L1 EV charge controller) controlled via the same L2 EMS
"""

from __future__ import annotations

from typing import Any, Optional

from hisim import cli_overrides, heating_system_selection, loadtypes, log
from hisim.postprocessingoptions import PostProcessingOptions
from hisim.simulator import SimulationParameters

from hisim.components import electricity_meter
from hisim.components import flexibility_potential
from hisim.components import heat_distribution_system
from hisim.components import loadprofilegenerator_utsp_connector
from hisim.components import more_advanced_heat_pump_hplib
from hisim.components import advanced_battery_bslib
from hisim.components import advanced_ev_battery_bslib
from hisim.components import controller_l1_generic_ev_charge
from hisim.components import controller_l2_energy_management_system
from hisim.components import generic_car
from hisim.components import simple_water_storage
from hisim.components import sia2024_occupancy
from hisim.components import generic_heat_pump
from hisim.components import strict_comfort_recovery_modifier
from hisim.components import buffer_remaining_capacity
from hisim.components import generic_pv_system
from hisim.components import stored_energy_from_soc
from hisim.components import sumbuilder
from hisim.components import weather
from hisim.components import building


def setup_function(my_sim: Any, my_simulation_parameters: Optional[SimulationParameters] = None) -> None:  # noqa: too-many-statements
    """Set up HP04 (HP03 + battery + EV)."""

    # =============================================================================================================================
    # Set simulation parameters
    year = 2021
    seconds_per_timestep = 900.0  # 15 min timesteps

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
    # Defaults for optional CLI overrides
    default_arch = "01_CH"
    default_weather = "ZUESTA"

    arch_override = cli_overrides.get_override("ARCH")
    arch_used = arch_override if arch_override is not None else default_arch

    weather_override = cli_overrides.get_override("WEATHER")
    weather_used = weather_override if weather_override is not None else default_weather

    cli_overrides.set_used_value("ARCH", arch_used)
    cli_overrides.set_used_value("WEATHER", weather_used)

    # =============================================================================================================================
    # Weather
    my_weather_config = weather.WeatherConfig.get_default(
        location_entry=getattr(weather.LocationEnum, default_weather)
    )
    if weather_override is not None:
        try:
            my_weather_config = cli_overrides.apply_weather_location_override(
                weather_module=weather,
                weather_value=weather_override,
                name="Weather",
                building_name="BUI1",
            )
            log.information(f"Applied CLI override WEATHER={weather_override} to weather configuration.")
        except Exception:
            log.warning(
                f"CLI override WEATHER={weather_override} was provided, but no matching "
                f"`LocationEnum.{weather_override}` exists in `hisim.components.weather`. Using default weather config."
            )
            my_weather_config = weather.WeatherConfig.get_default(
                location_entry=getattr(weather.LocationEnum, default_weather)
            )
            weather_used = default_weather
            cli_overrides.set_used_value("WEATHER", weather_used)
    my_weather = weather.Weather(config=my_weather_config, my_simulation_parameters=my_simulation_parameters)

    # =============================================================================================================================
    # Building
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
        "ZUESTA": -8.0,
        "BASSTA": -7.0,
        "KLO": -9.0,
        "RUE": -10.0,
    }
    if weather_used in weather_to_ref_temp_c:
        my_building_config.heating_reference_temperature_in_celsius = weather_to_ref_temp_c[weather_used]

    # Heating baseline setpoint (will be overridden toward strict comfort via modifier).
    my_building_config.set_heating_temperature_in_celsius = 20.5
    # Cooling enabled (same as HP03/HP02).
    my_building_config.set_cooling_temperature_in_celsius = 25.0

    my_building_information = building.BuildingInformation(config=my_building_config)
    my_building = building.Building(config=my_building_config, my_simulation_parameters=my_simulation_parameters)

    # =============================================================================================================================
    # Occupancy
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
    # Heat distribution + buffer + DHW storage
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

    my_heat_distribution_config = heat_distribution_system.HeatDistributionConfig.get_default_heatdistributionsystem_config(
        water_mass_flow_rate_in_kg_per_second=my_hds_controller_information.water_mass_flow_rate_in_kp_per_second,
        absolute_conditioned_floor_area_in_m2=my_building_information.scaled_conditioned_floor_area_in_m2,
        heating_system=my_hds_controller_information.hds_controller_config.heating_system,
    )
    my_heat_distribution = heat_distribution_system.HeatDistribution(
        config=my_heat_distribution_config, my_simulation_parameters=my_simulation_parameters
    )

    my_hot_water_storage_config = simple_water_storage.SimpleHotWaterStorageConfig.get_scaled_hot_water_storage(
        max_thermal_power_in_watt_of_heating_system=my_building_information.max_thermal_building_demand_in_watt,
        sizing_option=simple_water_storage.HotWaterStorageSizingEnum.SIZE_ACCORDING_TO_HEAT_PUMP,
    )
    my_hot_water_storage = simple_water_storage.SimpleHotWaterStorage(
        config=my_hot_water_storage_config, my_simulation_parameters=my_simulation_parameters
    )

    my_buffer_remaining = buffer_remaining_capacity.BufferRemainingCapacity(
        my_simulation_parameters=my_simulation_parameters,
        config=buffer_remaining_capacity.BufferRemainingCapacityConfig.get_default_config(
            building_name="BUI1",
            name="BufferRemainingCapacity60C",
            volume_heating_water_storage_in_liter=my_hot_water_storage_config.volume_heating_water_storage_in_liter,
            max_temperature_in_celsius=60.0,
        ),
    )

    my_dhw_storage_config = simple_water_storage.SimpleDHWStorageConfig.get_default_simpledhwstorage_config()
    my_dhw_storage_config.name = "DHWStorage"
    my_dhw_storage = simple_water_storage.SimpleDHWStorage(
        my_simulation_parameters=my_simulation_parameters, config=my_dhw_storage_config
    )

    # =============================================================================================================================
    # PV system
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
    # Space heating/cooling controller (mode 2 -> heating/cooling/off)
    hp_controller_mode = 2
    my_heatpump_controller_sh_config = more_advanced_heat_pump_hplib.MoreAdvancedHeatPumpHPLibControllerSpaceHeatingConfig(
        name="HeatPumpControllerSH",
        building_name="BUI1",
        mode=hp_controller_mode,
        set_heating_threshold_outside_temperature_in_celsius=16.0,
        set_cooling_threshold_outside_temperature_in_celsius=22.0,
        upper_temperature_offset_for_state_conditions_in_celsius=1.0,
        lower_temperature_offset_for_state_conditions_in_celsius=1.0,
        heat_distribution_system_type=my_hds_controller_information.heat_distribution_system_type,
    )
    my_heatpump_controller_space_heating = more_advanced_heat_pump_hplib.MoreAdvancedHeatPumpHPLibControllerSpaceHeating(
        config=my_heatpump_controller_sh_config, my_simulation_parameters=my_simulation_parameters
    )

    my_heatpump_controller_dhw_config = (
        more_advanced_heat_pump_hplib.MoreAdvancedHeatPumpHPLibControllerDHWConfig.get_default_dhw_controller_config()
    )
    my_heatpump_controller_dhw = more_advanced_heat_pump_hplib.MoreAdvancedHeatPumpHPLibControllerDHW(
        config=my_heatpump_controller_dhw_config, my_simulation_parameters=my_simulation_parameters
    )

    # =============================================================================================================================
    # Heat pump sizing (discrete smart_devices choice, then HPLib initialization robustness)
    sizing_mode = (cli_overrides.get_override("HEATGEN_SIZING") or "IDEAL_LOOKUP").strip().upper()
    if sizing_mode == "IDEAL_LOOKUP":
        ideal_heating_power_in_watt = heating_system_selection.get_ideal_power_from_lookup(
            arch=arch_used, weather=weather_used
        )
        chosen_hp = heating_system_selection.pick_heat_pump_closest_to_ideal(
            ideal_power_in_watt=ideal_heating_power_in_watt
        )
        p_th_set = float(chosen_hp.nominal_heating_power_in_watt)
        log.information(
            f"Using ideal lookup + discrete HP library selection for ARCH={arch_used} WEATHER={weather_used}: "
            f"ideal {ideal_heating_power_in_watt / 1e3:.2f} kW, "
            f"chosen {chosen_hp.manufacturer} / {chosen_hp.name} ({p_th_set / 1e3:.2f} kW)."
        )
        cli_overrides.set_used_value("HP_MODEL", f"{chosen_hp.manufacturer} / {chosen_hp.name}")
        cli_overrides.set_used_value("HP_NOMINAL_POWER_W", str(int(round(p_th_set))))
        my_heatpump_config = more_advanced_heat_pump_hplib.MoreAdvancedHeatPumpHPLibConfig.get_scaled_advanced_hp_lib(
            heating_load_of_building_in_watt=p_th_set,
            heating_reference_temperature_in_celsius=my_building_information.heating_reference_temperature_in_celsius,
        )
        my_heatpump_config.set_thermal_output_power_in_watt = float(p_th_set)  # type: ignore[assignment]
    else:
        my_heatpump_config = more_advanced_heat_pump_hplib.MoreAdvancedHeatPumpHPLibConfig.get_scaled_advanced_hp_lib(
            heating_load_of_building_in_watt=my_building_information.max_thermal_building_demand_in_watt,
            heating_reference_temperature_in_celsius=my_building_information.heating_reference_temperature_in_celsius,
        )

    my_heatpump_config.name = "HeatPumpHPLib"
    my_heatpump_config.group_id = 1
    my_heatpump_config.flow_temperature_in_celsius = 35.0
    my_heatpump_config.with_domestic_hot_water_preparation = True
    my_heatpump_config.cycling_mode = True
    # Keep heating-side cycling behavior identical to HP01
    my_heatpump_config.minimum_running_time_in_seconds = 14400
    my_heatpump_config.minimum_idle_time_in_seconds = 900

    last_exc: Exception | None = None
    p0 = float(getattr(my_heatpump_config, "set_thermal_output_power_in_watt", 0.0) or 0.0)
    power_candidates = [p0] if p0 > 0.0 else [0.0]
    if p0 > 0.0:
        power_candidates = [p0, p0 * 0.9, p0 * 0.8, p0 * 0.7, p0 * 0.6, p0 * 0.5]
        power_candidates = [p for p in power_candidates if p >= 500.0]

    t_out_candidates = [35.0, 30.0, 25.0, 21.0, 18.0, 15.0]
    t_in0 = float(getattr(my_heatpump_config, "heating_reference_temperature_in_celsius", -7.0) or -7.0)
    t_in_candidates = [t_in0, -7.0, -5.0, -3.0, 0.0]
    # keep order unique
    t_in_unique: list[float] = []
    for t in t_in_candidates:
        t = float(t)
        if t not in t_in_unique:
            t_in_unique.append(t)

    for t_in in t_in_unique:
        my_heatpump_config.heating_reference_temperature_in_celsius = float(t_in)
        for t_out in t_out_candidates:
            my_heatpump_config.flow_temperature_in_celsius = float(t_out)
            for p in power_candidates:
                if p > 0.0:
                    my_heatpump_config.set_thermal_output_power_in_watt = float(p)  # type: ignore[assignment]
                try:
                    my_heatpump = more_advanced_heat_pump_hplib.MoreAdvancedHeatPumpHPLib(
                        config=my_heatpump_config, my_simulation_parameters=my_simulation_parameters
                    )
                    if t_in != t_in0:
                        log.warning(
                            "HPLib could not parameterize the heat pump at the exact design outdoor temperature. "
                            f"Adjusted t_in from {t_in0:.1f}°C to {t_in:.1f}°C for HPLib initialization only "
                            "(weather time series unchanged)."
                        )
                    last_exc = None
                    break
                except ValueError as exc:
                    last_exc = exc
                    continue
            if last_exc is None:
                break
        if last_exc is None:
            break
    if last_exc is not None:
        raise last_exc

    # =============================================================================================================================
    # Electricity meter + strict comfort (heating recovery only)
    my_electricity_meter = electricity_meter.ElectricityMeter(
        my_simulation_parameters=my_simulation_parameters,
        config=electricity_meter.ElectricityMeterConfig.get_electricity_meter_default_config(),
    )

    # KPI splits for postprocessing (aligned across HP/BO/BG/BP/GR setups):
    # - `HeatGeneratorTotalThermalPower`: space heating and cooling (negative during cooling) via `ThermalOutputPowerSH`; no DHW.
    # - `HeatGeneratorPlantDhwThermalPower`: generator-side DHW (fossil / HP / district).
    # - `SolarDhwThermalPower`: solar thermal into DHW (0 W here — no solar primary on DHW).
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

    # =============================================================================================================================
    # Battery + Electric Vehicle (via EMS)
    #
    # Battery + EV charger need a target power signal. We use the existing L2 EMS to distribute PV surplus
    # (and grid power, if needed) to those flexible consumers.
    my_ems = controller_l2_energy_management_system.L2GenericEnergyManagementSystem(
        my_simulation_parameters=my_simulation_parameters,
        config=controller_l2_energy_management_system.EMSConfig.get_default_config_ems(
            building_name="BUI1",
            name="L2EMSElectricityController",
        ),
    )

    # Battery sized ~ PV peak power (kWh approx = kWp, with default c-rate 0.5C).
    my_battery_config = advanced_battery_bslib.BatteryConfig.get_scaled_battery(
        total_pv_power_in_watt_peak=my_pv_config.power_in_watt,
        building_name="BUI1",
        name="Battery",
    )
    my_battery = advanced_battery_bslib.Battery(
        my_simulation_parameters=my_simulation_parameters,
        config=my_battery_config,
    )

    # One (or more) electric cars based on the occupancy schedules.
    #
    # Note: `GenericCarInformation` only works with LPG/UTSP occupancy (it reads `occupancy.car_data_dict`).
    # For SIA2024 we fall back to a simple default EV profile (always at home, zero driving),
    # so HP04 remains runnable with OCC=SIA2024.
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
        # Create an extra LPG/UTSP connector instance only to access car mobility data.
        # Important: we do NOT use its electricity/heat/water outputs anywhere to avoid double-counting.
        my_mobility_occupancy_config = loadprofilegenerator_utsp_connector.UtspLpgConnectorConfig.get_default_utsp_connector_config(
            building_name="BUI1"
        )
        my_mobility_occupancy_config.name = "UTSPConnectorMobilityOnly"
        # Force local LPG to actually generate mobility time series (predefined profiles do not include car data).
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
            # Typical case: predefined LPG profiles have no car mobility data.
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
        # prioritize EV after heat pump, before stationary battery
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
            comfort_band_inner_offset_in_celsius=0.5,
            comfort_band_inner_offset_lower_in_celsius=1.0,
            comfort_band_inner_offset_upper_in_celsius=0.5,
            heating_disabled_above_running_mean_outdoor_temperature_in_celsius=18.0,
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

    # Heating recovery modifier: we still apply this as heating-side bias.
    my_setpoint_modifier = strict_comfort_recovery_modifier.StrictComfortRecoveryModifier(
        my_simulation_parameters=my_simulation_parameters,
        config=strict_comfort_recovery_modifier.StrictComfortRecoveryModifierConfig.get_default_config(
            building_name="BUI1",
            name="StrictComfortSetpointModifier",
            base_heating_setpoint_in_celsius=20.5,
        ),
    )
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

    # Apply modifier to the heat distribution controller as well (same as HP01) so heating-side behavior matches.
    # The HDS controller uses this modifier primarily in heating mode (flow temperature calculation),
    # so this should not materially alter cooling operation.
    my_heat_distribution_controller.connect_input(
        my_heat_distribution_controller.BuildingTemperatureModifier,
        my_setpoint_modifier.component_name,
        my_setpoint_modifier.TemperatureModifier,
    )

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
    my_building.connect_only_predefined_connections(my_weather)
    if occ_mode == "SIA2024":
        my_building.connect_input(my_building.HeatingByResidents, my_occupancy.component_name, my_occupancy.HeatingByResidents)
        my_building.connect_input(my_building.HeatingByDevices, my_occupancy.component_name, my_occupancy.HeatingByDevices)
        my_building.connect_input(my_building.NumberOfResidents, my_occupancy.component_name, my_occupancy.NumberOfResidents)
    else:
        my_building.connect_only_predefined_connections(my_occupancy)
    my_building.connect_input(
        my_building.BuildingTemperatureModifier,
        my_setpoint_modifier.component_name,
        my_setpoint_modifier.TemperatureModifier,
    )
    my_building.connect_input(
        my_building.ThermalPowerDelivered,
        my_heat_distribution.component_name,
        my_heat_distribution.ThermalPowerDelivered,
    )

    # HeatDistributionController: connect Weather + Building defaults (thermal demand includes cooling)
    my_heat_distribution_controller.connect_only_predefined_connections(my_weather, my_building)
    my_heat_distribution.connect_only_predefined_connections(
        my_building, my_heat_distribution_controller, my_hot_water_storage
    )

    my_heatpump.connect_only_predefined_connections(
        my_heatpump_controller_space_heating,
        my_heatpump_controller_dhw,
        my_weather,
        my_hot_water_storage,
        my_dhw_storage,
    )

    # Cooling is included in `ThermalOutputPowerSH` as negative power during cooling mode
    # (see `MoreAdvancedHeatPumpHPLib` KPI split on SH > 0 vs SH < 0); do not add SH twice.
    my_heatgen_total_thermal_power.connect_input(
        my_heatgen_total_thermal_power.SumInput1,
        my_heatpump.component_name,
        my_heatpump.ThermalOutputPowerSH,
    )
    my_heatgen_plant_dhw_thermal_power.connect_input(
        my_heatgen_plant_dhw_thermal_power.SumInput1,
        my_heatpump.component_name,
        my_heatpump.ThermalOutputPowerDHW,
    )

    if float(my_heatpump.parameters["Group"].iloc[0]) in (1.0, 4.0):
        my_heatpump.connect_input(
            my_heatpump.TemperatureInputPrimary,
            my_weather.component_name,
            my_weather.DailyAverageOutsideTemperatures,
        )
    else:
        raise KeyError("HP04 is configured as air-source heat pump.")

    my_heatpump_controller_space_heating.connect_only_predefined_connections(
        my_heat_distribution_controller, my_weather, my_hot_water_storage
    )
    my_heatpump_controller_dhw.connect_only_predefined_connections(my_dhw_storage)

    my_hot_water_storage.connect_input(
        my_hot_water_storage.WaterTemperatureFromHeatDistribution,
        my_heat_distribution.component_name,
        my_heat_distribution.WaterTemperatureOutput,
    )
    my_hot_water_storage.connect_input(
        my_hot_water_storage.WaterTemperatureFromHeatGenerator,
        my_heatpump.component_name,
        my_heatpump.TemperatureOutputSH,
    )
    my_hot_water_storage.connect_input(
        my_hot_water_storage.WaterMassFlowRateFromHeatGenerator,
        my_heatpump.component_name,
        my_heatpump.MassFlowOutputSH,
    )

    my_dhw_storage.connect_input(
        my_dhw_storage.WaterTemperatureFromHeatGenerator,
        my_heatpump.component_name,
        my_heatpump.TemperatureOutputDHW,
    )
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterMassFlowRateFromHeatGenerator,
        my_heatpump.component_name,
        my_heatpump.MassFlowOutputDHW,
    )
    my_dhw_storage.connect_input(
        my_dhw_storage.WaterConsumption,
        my_occupancy.component_name,
        my_occupancy.WaterConsumption,
    )

    # -----------------------------------------------------------------------------------------------------------------
    # Connect EV components
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
        source_object_name=my_heatpump.component_name,
        source_component_output=my_heatpump.ElectricalInputPowerTotal,
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
        my_heatpump.component_name,
        my_heatpump.ThermalOutputPowerSH,
    )
    my_flex_potential.connect_input(
        my_flex_potential.HvacCoolingPower,
        my_heatpump.component_name,
        my_heatpump.ThermalOutputPowerSH,  # cooling signal not separately available here; keep same channel
    )
    my_flex_potential.connect_input(
        my_flex_potential.HvacElectricPower,
        my_heatpump.component_name,
        my_heatpump.ElectricalInputPowerTotal,
    )

    # Stored electricity in battery / EV (Wh) for flexibility potentials
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

    # Sum EV stored energy across all car batteries
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
    my_sim.add_component(my_setpoint_modifier)
    my_sim.add_component(my_heat_distribution_controller)
    my_sim.add_component(my_heat_distribution)
    my_sim.add_component(my_hot_water_storage)
    my_sim.add_component(my_buffer_remaining)
    my_sim.add_component(my_dhw_storage)
    my_sim.add_component(my_heatpump_controller_space_heating)
    my_sim.add_component(my_heatpump_controller_dhw)
    my_sim.add_component(my_heatpump)
    my_sim.add_component(my_heatgen_total_thermal_power)
    my_sim.add_component(my_heatgen_plant_dhw_thermal_power)
    my_sim.add_component(my_solar_dhw_thermal_power)
    my_sim.add_component(my_flex_potential)
    my_sim.add_component(my_battery_energy_wh)
    my_sim.add_component(my_ev_energy_wh_sum)

    for car in my_cars:
        my_sim.add_component(car)
    for car_battery in my_car_batteries:
        my_sim.add_component(car_battery)
    for car_battery_controller in my_car_battery_controllers:
        my_sim.add_component(car_battery_controller)

