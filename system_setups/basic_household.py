"""Basic household system setup. Shows how to set up a standard system."""

# clean

from typing import Optional, Any
from hisim.simulator import SimulationParameters
from hisim.components import loadprofilegenerator_utsp_connector
from hisim.components import weather
from hisim.components import generic_pv_system
from hisim.components import building
from hisim.components import generic_heat_pump
from hisim.components import electricity_meter
from hisim.components import sia2024_occupancy
from hisim import loadtypes
from hisim import cli_overrides
from hisim import log
from hisim import heating_system_selection


__authors__ = "Vitor Hugo Bellotto Zago, Noah Pflugradt"
__copyright__ = "Copyright 2022, FZJ-IEK-3"
__credits__ = ["Noah Pflugradt"]
__license__ = "MIT"
__version__ = "1.0"
__maintainer__ = "Noah Pflugradt"
__status__ = "development"


def setup_function(
    my_sim: Any, my_simulation_parameters: Optional[SimulationParameters] = None
) -> None:  # noqa: too-many-statements
    """Basic household system setup.

    This setup function emulates an household including the basic components. Here the residents have their
    electricity and heating needs covered by the photovoltaic system and the heat pump.

    - Simulation Parameters
    - Components
        - Occupancy (Residents' Demands)
        - Weather
        - Photovoltaic System
        - Building
        - Heat Pump
    """

    # =================================================================================================================================
    # Set System Parameters

    # Set Simulation Parameters
    year = 2021
    seconds_per_timestep = 3600

    # Set Heat Pump Controller
    temperature_air_heating_in_celsius = 20.5
    temperature_air_cooling_in_celsius = 24.0
    offset = 0.5
    hp_mode = 2

    # Defaults for optional CLI overrides (defined once, reused below)
    default_arch = "01_CH"
    default_weather = "ZUESTA"

    # =================================================================================================================================
    # Build Components

    # Build Simulation Parameters
    if my_simulation_parameters is None:
        
        my_simulation_parameters = SimulationParameters.full_year_with_only_csv(
            year=year, seconds_per_timestep=seconds_per_timestep
        )
        #my_simulation_parameters = SimulationParameters.full_year_all_options(
            #year=year, seconds_per_timestep=seconds_per_timestep
        #)
        
        #my_simulation_parameters = SimulationParameters.full_year_with_only_plots(
            #year=year, seconds_per_timestep=seconds_per_timestep
        #)

    my_sim.set_simulation_parameters(my_simulation_parameters)
    print(my_simulation_parameters.post_processing_options)

    # -----------------------------
    # Determine ARCH / WEATHER used
    # -----------------------------
    arch_override = cli_overrides.get_override("ARCH")
    arch_used = arch_override if arch_override is not None else default_arch

    weather_override = cli_overrides.get_override("WEATHER")
    weather_used = weather_override if weather_override is not None else default_weather

    # Record used values for batch runners / reporting
    cli_overrides.set_used_value("ARCH", arch_used)
    cli_overrides.set_used_value("WEATHER", weather_used)

    # -----------------------------
    # Build Weather (needed for sizing assumptions)
    # -----------------------------
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

    # -----------------------------
    # Build Building
    # -----------------------------
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
    #my_building_config = building.BuildingConfig.get_default_german_single_family_home()

    # Use a location-dependent design temperature (heating reference temperature) if available.
    # The `LocationEnum` names in the batch matrix (ZUESTA/BASSTA/KLO/RUE) are Swiss custom CSVs.
    # For now we map them to typical Swiss design temperatures (°C). Users can refine these later.
    weather_to_ref_temp_c = {
        "ZUESTA": -8.0,   # Zurich
        "BASSTA": -7.0,   # Basel
        "KLO": -9.0,      # Kloten (airport, slightly colder)
        "RUE": -10.0,     # Ruenenberg (higher/colder)
    }
    if weather_used in weather_to_ref_temp_c:
        my_building_config.heating_reference_temperature_in_celsius = weather_to_ref_temp_c[weather_used]

    my_building = building.Building(config=my_building_config, my_simulation_parameters=my_simulation_parameters)

    # Build Occupancy (choice: LPG/UTSP default, or SIA2024)
    occ_mode = "SIA2024" #(cli_overrides.get_override("OCC") or "LPG").strip().upper()
    if occ_mode == "SIA2024":
        # `BuildingConfig.absolute_conditioned_floor_area_in_m2` is the user-configured size input.
        # The Building later derives `scaled_conditioned_floor_area_in_m2` by scaling TABULA reference
        # values with this configured area (so they match unless the config omits/overrides size inputs).
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

    # Build PV
    my_photovoltaic_system_config = generic_pv_system.PVSystemConfig.get_default_pv_system()

    my_photovoltaic_system = generic_pv_system.PVSystem(
        config=my_photovoltaic_system_config,
        my_simulation_parameters=my_simulation_parameters,
    )

    # Build Electricity Meter
    my_electricity_meter = electricity_meter.ElectricityMeter(
        my_simulation_parameters=my_simulation_parameters,
        config=electricity_meter.ElectricityMeterConfig.get_electricity_meter_default_config(),
    )

    # Build Heat Pump Controller
    my_heat_pump_controller = generic_heat_pump.GenericHeatPumpController(
        config=generic_heat_pump.GenericHeatPumpControllerConfig(
            building_name="BUI1",
            name="GenericHeatPumpController",
            temperature_air_heating_in_celsius=temperature_air_heating_in_celsius,
            temperature_air_cooling_in_celsius=temperature_air_cooling_in_celsius,
            offset=offset,
            mode=hp_mode,
            use_adaptive_comfort_band=True,
        ),
        my_simulation_parameters=my_simulation_parameters,
    )

    # Build Heat Pump (optional: select model closest to ideal size from lookup)
    sizing_mode = (cli_overrides.get_override("HEATGEN_SIZING") or "").strip().upper()
    if sizing_mode == "IDEAL_LOOKUP":
        ideal_heating_power_in_watt = heating_system_selection.get_ideal_power_from_lookup(
            arch=arch_used, weather=weather_used
        )
        chosen_hp = heating_system_selection.pick_heat_pump_closest_to_ideal(
            ideal_power_in_watt=ideal_heating_power_in_watt
        )
        log.information(
            f"Selected heat pump model for ARCH={arch_used} WEATHER={weather_used}: "
            f"{chosen_hp.manufacturer} / {chosen_hp.name} "
            f"(nominal {chosen_hp.nominal_heating_power_in_watt / 1e3:.1f} kW), "
            f"ideal {ideal_heating_power_in_watt / 1e3:.2f} kW."
        )
        my_heat_pump_config = generic_heat_pump.GenericHeatPumpConfig(
            building_name="BUI1",
            name="HeatPump",
            manufacturer=chosen_hp.manufacturer,
            heat_pump_name=chosen_hp.name,
            min_operation_time=60 * 60,
            min_idle_time=15 * 60,
        )
    else:
        my_heat_pump_config = generic_heat_pump.GenericHeatPumpConfig.get_default_generic_heat_pump_config()

    my_heat_pump = generic_heat_pump.GenericHeatPump(
        config=my_heat_pump_config,
        my_simulation_parameters=my_simulation_parameters,
    )

    # =================================================================================================================================
    # Connect Component Inputs with Outputs

    my_photovoltaic_system.connect_only_predefined_connections(my_weather)

    # Electricity Grid
    my_electricity_meter.add_component_input_and_connect(
        source_object_name=my_photovoltaic_system.component_name,
        source_component_output=my_photovoltaic_system.ElectricityOutput,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[
            loadtypes.ComponentType.PV,
            loadtypes.InandOutputType.ELECTRICITY_PRODUCTION,
        ],
        source_weight=999,
    )

    my_electricity_meter.add_component_input_and_connect(
        source_object_name=my_occupancy.component_name,
        source_component_output=my_occupancy.ElectricalPowerConsumption,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[loadtypes.InandOutputType.ELECTRICITY_CONSUMPTION_UNCONTROLLED],
        source_weight=999,
    )

    my_electricity_meter.add_component_input_and_connect(
        source_object_name=my_heat_pump.component_name,
        source_component_output=my_heat_pump.ElectricityOutput,
        source_load_type=loadtypes.LoadTypes.ELECTRICITY,
        source_unit=loadtypes.Units.WATT,
        source_tags=[
            loadtypes.ComponentType.HEAT_PUMP,
            loadtypes.InandOutputType.ELECTRICITY_CONSUMPTION_UNCONTROLLED,
        ],
        source_weight=999,
    )

    # Building: always connect weather defaults; occupancy depends on chosen mode
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
    else:
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
        my_building.ThermalPowerDelivered,
        my_heat_pump.component_name,
        my_heat_pump.ThermalPowerDelivered,
    )

    my_heat_pump_controller.connect_only_predefined_connections(my_building)

    my_heat_pump_controller.connect_input(
        my_heat_pump_controller.ElectricityInput,
        my_electricity_meter.component_name,
        my_electricity_meter.ElectricityAvailable,
    )
    my_heat_pump.connect_only_predefined_connections(my_weather, my_heat_pump_controller)
    my_heat_pump.get_default_connections_heatpump_controller()
    # =================================================================================================================================
    # Add Components to Simulation Parameters

    my_sim.add_component(my_occupancy)
    my_sim.add_component(my_weather)
    my_sim.add_component(my_photovoltaic_system)
    my_sim.add_component(my_electricity_meter)
    my_sim.add_component(my_building)
    my_sim.add_component(my_heat_pump_controller)
    my_sim.add_component(my_heat_pump)
