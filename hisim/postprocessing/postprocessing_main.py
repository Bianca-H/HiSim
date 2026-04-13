"""Main postprocessing module that starts all other modules."""

import json

# clean
import os
import pickle
import string
import sys
from timeit import default_timer as timer
from typing import Any, Optional, List, Dict, Tuple

import pandas as pd

from hisim import log
from hisim import utils
from hisim.component import ComponentOutput
from hisim.components.configuration import EmissionFactorsAndCostsForFuelsConfig
from hisim.components.electricity_meter import ElectricityMeter
from hisim.components.gas_meter import GasMeter
from hisim.components.fuel_meter import FuelMeter
from hisim.components import building, loadprofilegenerator_utsp_connector
from hisim.json_generator import JsonConfigurationGenerator
from hisim.building_sizer_utils.interface_configs.kpi_config import KPIConfig
from hisim.postprocessing import charts
from hisim.postprocessing import reportgenerator
from hisim.postprocessing.chart_singleday import ChartSingleDay
from hisim.postprocessing.kpi_computation.compute_kpis import KpiGenerator
from hisim.postprocessing.generate_csv_for_housing_database import generate_csv_for_database
from hisim.postprocessing.cost_and_emission_computation.opex_and_capex_cost_calculation import (
    opex_calculation,
    capex_calculation,
)
from hisim.postprocessing.postprocessing_datatransfer import PostProcessingDataTransfer
from hisim.postprocessing.report_image_entries import ReportImageEntry, SystemChartEntry
from hisim.postprocessing.system_chart import SystemChart
from hisim.postprocessing.webtool_entries import WebtoolDict
from hisim.postprocessingoptions import PostProcessingOptions
from hisim.sim_repository_singleton import SingletonSimRepository, SingletonDictKeyEnum
from hisim import loadtypes as lt
from hisim.loadtypes import OutputPostprocessingRules


class PostProcessor:
    """Core Post processor class."""

    @utils.measure_execution_time
    def __init__(self):
        """Initializes the post processing."""
        self.dirname: str
        self.chapter_counter: int = 1
        self.figure_counter: int = 1
        self.result_data_folder_for_scenario_evaluation: str = ""
        self.model: str = "HiSim"
        self.scenario: str = ""
        self.region: str = ""
        self.year: int = 2021

    def set_dir_results(self, dirname: Optional[str] = None) -> None:
        """Sets the results directory."""
        if dirname is None:
            raise ValueError("No results directory name was defined.")
        self.dirname = dirname

    @utils.measure_execution_time
    @utils.measure_memory_leak
    def run(self, ppdt: PostProcessingDataTransfer) -> None:  # noqa: MC0001
        """Runs the main post processing."""
        # Define the directory name
        log.information("Main post processing function")
        report_image_entries: List[ReportImageEntry] = []
        # Check whether HiSim is running in a docker container
        docker_flag = os.getenv("HISIM_IN_DOCKER_CONTAINER", "false")
        if docker_flag.lower() in ("true", "yes", "y", "1"):
            # Charts etc. are not needed when executing HiSim in a container. Allow only csv files and KPI.
            allowed_options_for_docker = {
                PostProcessingOptions.EXPORT_TO_CSV,
                PostProcessingOptions.COMPUTE_KPIS,
                PostProcessingOptions.GENERATE_CSV_FOR_HOUSING_DATA_BASE,
                PostProcessingOptions.COMPUTE_OPEX,
                PostProcessingOptions.COMPUTE_CAPEX,
                PostProcessingOptions.MAKE_RESULT_JSON_FOR_WEBTOOL,
                PostProcessingOptions.MAKE_OPERATION_RESULTS_FOR_WEBTOOL,
                PostProcessingOptions.WRITE_COMPONENT_CONFIGS_TO_JSON,
                PostProcessingOptions.WRITE_KPIS_TO_JSON,
                PostProcessingOptions.WRITE_KPIS_TO_JSON_FOR_BUILDING_SIZER,
            }
            # Of all specified options, select those that are allowed
            valid_options = list(set(ppdt.post_processing_options) & allowed_options_for_docker)
            if len(valid_options) < len(ppdt.post_processing_options):
                # At least one invalid option was set
                ppdt.post_processing_options = valid_options
                log.warning("Hisim is running in a docker container. Disabled invalid postprocessing options.")
        report: Optional[reportgenerator.ReportGenerator] = None
        days = {"month": 0, "day": 0}
        system_chart_entries: List[SystemChartEntry] = []
        building_objects_in_district_list = self.get_building_object_in_district(ppdt)

        # Make plots
        if PostProcessingOptions.PLOT_LINE in ppdt.post_processing_options:
            log.information("Making line plots.")
            start = timer()
            self.make_line_plots(ppdt, report_image_entries=report_image_entries)
            end = timer()
            duration = end - start
            log.information("Making line plots took " + f"{duration:1.2f}s.")
        if PostProcessingOptions.PLOT_CARPET in ppdt.post_processing_options:
            log.information("Making carpet plots.")
            start = timer()
            self.make_carpet_plots(ppdt, report_image_entries=report_image_entries)
            end = timer()
            duration = end - start
            log.information("Making carpet plots took " + f"{duration:1.2f}s.")
        if PostProcessingOptions.PLOT_SINGLE_DAYS in ppdt.post_processing_options:
            log.information("Making single day plots.")
            start = timer()
            self.make_single_day_plots(days, ppdt, report_image_entries=report_image_entries)
            end = timer()
            duration = end - start
            log.information("Making single day plots took " + f"{duration:1.2f}s.")

        # make monthly bar plots only if simulation duration approximately a year
        if (
            PostProcessingOptions.PLOT_MONTHLY_BAR_CHARTS in ppdt.post_processing_options
            and ppdt.simulation_parameters.duration.days >= 360
        ):
            log.information("Making monthly bar charts.")
            start = timer()
            self.make_monthly_bar_charts(ppdt, report_image_entries=report_image_entries)
            end = timer()
            duration = end - start
            log.information("Making monthly bar plots took " + f"{duration:1.2f}s.")

        # Export all results to CSV
        if PostProcessingOptions.EXPORT_TO_CSV in ppdt.post_processing_options:
            self.add_operational_emissions_outputs(ppdt)
            self.add_operational_costs_outputs(ppdt)
            self.add_car_operational_outputs(ppdt)
            self.add_actual_heat_supply_split_outputs(ppdt)
            log.information("Making CSV exports.")
            start = timer()
            self.make_csv_export(ppdt)
            end = timer()
            duration = end - start
            log.information("Making CSV export took " + f"{duration:1.2f}s.")
        if PostProcessingOptions.EXPORT_TO_PKL in ppdt.post_processing_options:
            log.information("Making pkl exports.")
            start = timer()
            self.make_pkl_export(ppdt)
            end = timer()
            duration = end - start
            log.information("Making PKL export took " + f"{duration:1.2f}s.")
        if PostProcessingOptions.MAKE_NETWORK_CHARTS in ppdt.post_processing_options:
            log.information("Computing network charts.")
            start = timer()
            system_chart_entries = self.make_network_charts(ppdt)
            end = timer()
            duration = end - start
            log.information("Computing network charts took " + f"{duration:1.2f}s.")
        # Generate Pdf report
        if PostProcessingOptions.GENERATE_PDF_REPORT in ppdt.post_processing_options:
            log.information("Making PDF report and writing simulation parameters to report.")
            start = timer()
            report = reportgenerator.ReportGenerator(dirpath=ppdt.simulation_parameters.result_directory)
            self.write_simulation_parameters_to_report(ppdt, report)
            end = timer()
            duration = end - start
            log.information(
                "Making PDF report and writing simulation parameters to report took " + f"{duration:1.2f}s."
            )

        if PostProcessingOptions.WRITE_COMPONENTS_TO_REPORT in ppdt.post_processing_options:
            log.information("Writing components to report.")
            start = timer()
            if report is not None:
                self.write_components_to_report(ppdt, report, report_image_entries)
            else:
                raise ValueError(
                    "report is None but should be a ReportGenerator object. "
                    "You probably need to set the GENERATE_PDF_REPORT option."
                )
            end = timer()
            duration = end - start
            log.information("Writing components to report took " + f"{duration:1.2f}s.")

        if PostProcessingOptions.WRITE_ALL_OUTPUTS_TO_REPORT in ppdt.post_processing_options:
            log.information("Writing all outputs to report.")
            start = timer()
            if report is not None:
                self.write_all_outputs_to_report(ppdt, report)
            else:
                raise ValueError(
                    "report is None but should be a ReportGenerator object. "
                    "You probably need to set the GENERATE_PDF_REPORT option."
                )
            end = timer()
            duration = end - start
            log.information("Writing all outputs to report took " + f"{duration:1.2f}s.")
        if PostProcessingOptions.WRITE_NETWORK_CHARTS_TO_REPORT in ppdt.post_processing_options:
            log.information("Writing network charts to report.")
            start = timer()
            if report is not None:
                self.write_network_charts_to_report(ppdt, report, system_chart_entries=system_chart_entries)
            else:
                raise ValueError(
                    "report is None but should be a ReportGenerator object. "
                    "You probably need to set the GENERATE_PDF_REPORT option."
                )
            end = timer()
            duration = end - start
            log.information("Writing network charts to report took " + f"{duration:1.2f}s.")
        if PostProcessingOptions.COMPUTE_OPEX in ppdt.post_processing_options:
            log.information(
                "Computing and writing operational costs and C02 emissions produced in operation to report."
            )
            start = timer()
            self.compute_and_write_opex_costs_to_report(ppdt, report, building_objects_in_district_list)
            end = timer()
            duration = end - start
            log.information(
                "Computing and writing operational costs and C02 emissions produced in operation to report took "
                + f"{duration:1.2f}s."
            )
        if PostProcessingOptions.COMPUTE_CAPEX in ppdt.post_processing_options:
            log.information(
                "Computing and writing investment costs and C02 emissions from production of devices to report."
            )
            start = timer()
            self.compute_and_write_capex_costs_to_report(ppdt, report, building_objects_in_district_list)
            end = timer()
            duration = end - start
            log.information(
                "Computing and writing investment costs and C02 emissions from production of devices to report took "
                + f"{duration:1.2f}s."
            )
        if PostProcessingOptions.COMPUTE_KPIS in ppdt.post_processing_options:
            log.information("Computing KPIs and writing to report if option is chosen.")
            start = timer()
            ppdt = self.compute_kpis_and_write_to_report_and_to_ppdt(ppdt, report, building_objects_in_district_list)
            end = timer()
            duration = end - start
            log.information("Computing and writing KPIs to report took " + f"{duration:1.2f}s.")

        if PostProcessingOptions.GENERATE_CSV_FOR_HOUSING_DATA_BASE in ppdt.post_processing_options:
            all_building_data = pd.DataFrame()
            occupancy_config = None
            for elem in ppdt.wrapped_components:
                if isinstance(elem.my_component, building.Building):
                    building_data = elem.my_component.my_building_information.buildingdata_ref
                    for building_object in building_objects_in_district_list:
                        if (
                            building_object in str(elem.my_component.component_name)
                            or not ppdt.simulation_parameters.multiple_buildings
                        ):
                            building_data["Object_Name"] = building_object
                    all_building_data = pd.concat([all_building_data, building_data], ignore_index=True)

                elif isinstance(elem.my_component, loadprofilegenerator_utsp_connector.UtspLpgConnectorConfig):
                    occupancy_config = elem.my_component.occupancy_config
            if len(all_building_data) == 0:
                log.warning("Building needs to be defined to generate csv for housing data base.")
            else:
                all_building_data.set_index("Object_Name", inplace=True)
                log.information("Generating csv for housing data base. ")
                start = timer()
                generate_csv_for_database(
                    all_outputs=ppdt.all_outputs,
                    results=ppdt.results,
                    simulation_parameters=ppdt.simulation_parameters,
                    building_data=all_building_data,
                    occupancy_config=occupancy_config,
                    wrapped_components=ppdt.wrapped_components,
                )
                end = timer()
                duration = end - start
                log.information("Generating csv for housing data base took " + f"{duration:1.2f}s.")

        # only a single day has been calculated. This gets special charts for debugging.
        if (
            PostProcessingOptions.PLOT_SPECIAL_TESTING_SINGLE_DAY in ppdt.post_processing_options
            and len(ppdt.results) == 1440
        ):
            log.information("Making special single day plots for a single day calculation for testing.")
            start = timer()
            self.make_special_one_day_debugging_plots(ppdt, report_image_entries=report_image_entries)
            end = timer()
            duration = end - start
            log.information(
                "Making special single day plots for a single day calculation for testing took " + f"{duration:1.2f}s."
            )

        # Write Outputs to specific format for scenario evaluation (idea for format from pyam package)
        if PostProcessingOptions.PREPARE_OUTPUTS_FOR_SCENARIO_EVALUATION in ppdt.post_processing_options:
            log.information("Prepare results for scenario evaluation.")
            start = timer()
            self.prepare_results_for_scenario_evaluation(ppdt)
            end = timer()
            duration = end - start
            log.information("Preparing results for scenario evaluation took " + f"{duration:1.2f}s.")

        # Open file explorer
        if PostProcessingOptions.OPEN_DIRECTORY_IN_EXPLORER in ppdt.post_processing_options:
            log.information("Opening the explorer.")
            self.open_dir_in_file_explorer(ppdt)

        # Prepare webtool results
        if PostProcessingOptions.MAKE_RESULT_JSON_FOR_WEBTOOL in ppdt.post_processing_options:
            log.information("Make JSON file for webtool.")
            self.write_results_for_webtool_to_json_file(ppdt, building_objects_in_district_list)

        # Prepare webtool operation results
        if PostProcessingOptions.MAKE_OPERATION_RESULTS_FOR_WEBTOOL in ppdt.post_processing_options:
            log.information("Make JSON file for webtool (operation).")
            self.write_operation_data_for_webtool(ppdt)

        if PostProcessingOptions.WRITE_COMPONENT_CONFIGS_TO_JSON in ppdt.post_processing_options:
            log.information("Writing component configurations to JSON file.")
            self.write_component_configurations_to_json(ppdt)

        if PostProcessingOptions.WRITE_CONFIGS_FOR_SCENARIO_EVALUATION_TO_JSON in ppdt.post_processing_options:
            log.information("Writing component configurations for scenario evaluation to JSON file.")
            self.write_config_data_for_scenario_evaluation(ppdt)

        if PostProcessingOptions.WRITE_KPIS_TO_JSON_FOR_BUILDING_SIZER in ppdt.post_processing_options:
            log.information("Writing KPIs to JSON file for building sizer.")
            self.write_kpis_to_json_for_building_sizer(ppdt, building_objects_in_district_list)

        if PostProcessingOptions.WRITE_KPIS_TO_JSON in ppdt.post_processing_options:
            log.information("Write all KPIs to json file.")
            self.write_kpis_to_json_file(ppdt)

        log.information("Finished main post processing function.")

    def make_network_charts(self, ppdt: PostProcessingDataTransfer) -> List[SystemChartEntry]:
        """Generates the network charts that show the connection of the elements."""
        systemchart = SystemChart(ppdt)
        return systemchart.make_chart()

    def make_special_one_day_debugging_plots(
        self,
        ppdt: PostProcessingDataTransfer,
        report_image_entries: List[ReportImageEntry],
    ) -> None:
        """Makes special plots for debugging if only a single day was calculated."""
        for index, output in enumerate(ppdt.all_outputs):
            if output.full_name == "Dummy # Residence Temperature":
                my_days = ChartSingleDay(
                    output=output.full_name,
                    component_name=output.component_name,
                    units=output.unit,
                    directory_path=ppdt.simulation_parameters.result_directory,
                    time_correction_factor=ppdt.time_correction_factor,
                    data=ppdt.results.iloc[:, index],
                    day=0,
                    month=0,
                    output2=ppdt.results.iloc[:, 11],
                    output_description=output.output_description,
                    figure_format=ppdt.simulation_parameters.figure_format,
                )
            else:
                my_days = ChartSingleDay(
                    output=output.full_name,
                    component_name=output.component_name,
                    units=output.unit,
                    directory_path=ppdt.simulation_parameters.result_directory,
                    time_correction_factor=ppdt.time_correction_factor,
                    data=ppdt.results.iloc[:, index],
                    day=0,
                    month=0,
                    output_description=output.output_description,
                    figure_format=ppdt.simulation_parameters.figure_format,
                )
            my_entry = my_days.plot(close=True)
            report_image_entries.append(my_entry)

    def make_csv_export(self, ppdt: PostProcessingDataTransfer) -> None:
        """Exports all data to CSV."""
        log.information("Exporting to csv.")
        self.export_results_to_csv(ppdt)

    def add_operational_emissions_outputs(self, ppdt: PostProcessingDataTransfer) -> None:
        """Add operational emissions time series and monthly sums to ppdt results.

        Emissions include all energy carriers consumed via meters:
        - Electricity from grid (ElectricityMeter)
        - Gas / Green hydrogen from grid (GasMeter)
        - Other fuels and district heating (FuelMeter)

        Uses `EmissionFactorsAndCostsForFuelsConfig` factors for the simulation year/country.
        """
        if ppdt.results is None or len(ppdt.results.index) == 0:
            return

        factors = EmissionFactorsAndCostsForFuelsConfig.get_values_for_year(
            year=ppdt.simulation_parameters.year,
            country=ppdt.simulation_parameters.country,
        )

        total_emissions_kg = pd.Series(0.0, index=ppdt.results.index)

        # Helper to find a column by component name + field name + unit
        def find_output_col(component_name: str, field_name: str, unit: str) -> int | None:
            for idx, outp in enumerate(ppdt.all_outputs):
                if outp.component_name == component_name and outp.field_name == field_name and outp.unit == unit:
                    return idx
            return None

        for wrapper in ppdt.wrapped_components:
            comp = wrapper.my_component

            # Electricity (kWh) -> kg
            if isinstance(comp, ElectricityMeter):
                idx = find_output_col(comp.component_name, comp.ElectricityFromGrid, "Wh")
                if idx is not None:
                    kwh = ppdt.results.iloc[:, idx] * 1e-3
                    total_emissions_kg = total_emissions_kg.add(kwh * factors.electricity_footprint_in_kg_per_kwh, fill_value=0.0)

            # Gas / H2 (kWh) -> kg
            elif isinstance(comp, GasMeter):
                idx = find_output_col(comp.component_name, comp.GasFromGrid, "Wh")
                if idx is not None:
                    kwh = ppdt.results.iloc[:, idx] * 1e-3
                    if comp.config.gas_loadtype.value == "Gas":
                        co2_per_kwh = factors.gas_footprint_in_kg_per_kwh
                    else:
                        # green hydrogen
                        co2_per_kwh = factors.green_hydrogen_gas_footprint_in_kg_per_kwh
                    total_emissions_kg = total_emissions_kg.add(kwh * co2_per_kwh, fill_value=0.0)

            # Other fuels / district heating (metered as heat consumption in Wh)
            elif isinstance(comp, FuelMeter):
                idx = find_output_col(comp.component_name, comp.HeatConsumption, "Wh")
                if idx is not None:
                    kwh = ppdt.results.iloc[:, idx] * 1e-3
                    loadtype = comp.config.fuel_loadtype.value
                    if loadtype == "Oil":
                        if comp.config.heating_value_of_fuel_in_kwh_per_liter is None:
                            log.warning(
                                f"FuelMeter {comp.component_name} is Oil but has no heating value. Skipping oil emissions."
                            )
                        else:
                            liters = kwh / float(comp.config.heating_value_of_fuel_in_kwh_per_liter)
                            total_emissions_kg = total_emissions_kg.add(liters * factors.oil_footprint_in_kg_per_l, fill_value=0.0)
                    elif loadtype == "Pellets":
                        total_emissions_kg = total_emissions_kg.add(kwh * factors.pellet_footprint_in_kg_per_kwh, fill_value=0.0)
                    elif loadtype == "WoodChips":
                        total_emissions_kg = total_emissions_kg.add(kwh * factors.wood_chip_footprint_in_kg_per_kwh, fill_value=0.0)
                    elif loadtype == "DistrictHeating":
                        total_emissions_kg = total_emissions_kg.add(kwh * factors.district_heating_footprint_in_kg_per_kwh, fill_value=0.0)

        # Add per-timestep emissions to all_results
        emissions_col_name = "PostProcessing - OperationalEmissionsTotal [Any - kgCO2eq]"
        ppdt.results[emissions_col_name] = total_emissions_kg

        # Add monthly sums if monthly results exist (so it shows up in monthly exports)
        if ppdt.results_monthly is not None:
            monthly_emissions = total_emissions_kg.resample("M").sum()
            ppdt.results_monthly[emissions_col_name] = monthly_emissions

    def add_operational_costs_outputs(self, ppdt: PostProcessingDataTransfer) -> None:
        """Add operational energy cost time series and monthly sums to ppdt results.

        Costs include all energy carriers consumed via meters:
        - Electricity from grid (cost) and electricity to grid (revenue) via ElectricityMeter
        - Gas / Green hydrogen from grid via GasMeter
        - Other fuels and district heating via FuelMeter

        Uses `EmissionFactorsAndCostsForFuelsConfig` costs for the simulation year/country.
        """
        if ppdt.results is None or len(ppdt.results.index) == 0:
            return

        factors = EmissionFactorsAndCostsForFuelsConfig.get_values_for_year(
            year=ppdt.simulation_parameters.year,
            country=ppdt.simulation_parameters.country,
        )

        # Payable costs are positive. Revenues are positive in their own series.
        payable_costs_eur = pd.Series(0.0, index=ppdt.results.index)
        revenue_from_feed_in_eur = pd.Series(0.0, index=ppdt.results.index)

        def find_output_col(component_name: str, field_name: str, unit: str) -> int | None:
            for idx, outp in enumerate(ppdt.all_outputs):
                if outp.component_name == component_name and outp.field_name == field_name and outp.unit == unit:
                    return idx
            return None

        for wrapper in ppdt.wrapped_components:
            comp = wrapper.my_component

            # Electricity: kWh from grid costs, kWh to grid yields revenue (negative cost)
            if isinstance(comp, ElectricityMeter):
                idx_from = find_output_col(comp.component_name, comp.ElectricityFromGrid, "Wh")
                if idx_from is not None:
                    kwh_from = ppdt.results.iloc[:, idx_from] * 1e-3
                    payable_costs_eur = payable_costs_eur.add(
                        kwh_from * factors.electricity_costs_in_euro_per_kwh,
                        fill_value=0.0,
                    )
                idx_to = find_output_col(comp.component_name, comp.ElectricityToGrid, "Wh")
                if idx_to is not None:
                    kwh_to = ppdt.results.iloc[:, idx_to] * 1e-3
                    revenue_from_feed_in_eur = revenue_from_feed_in_eur.add(
                        kwh_to * factors.electricity_to_grid_revenue_in_euro_per_kwh,
                        fill_value=0.0,
                    )

            # Gas / H2: kWh from grid costs
            elif isinstance(comp, GasMeter):
                idx = find_output_col(comp.component_name, comp.GasFromGrid, "Wh")
                if idx is not None:
                    kwh = ppdt.results.iloc[:, idx] * 1e-3
                    if comp.config.gas_loadtype.value == "Gas":
                        euro_per_kwh = factors.gas_costs_in_euro_per_kwh
                    else:
                        euro_per_kwh = factors.green_hydrogen_gas_costs_in_euro_per_kwh
                    payable_costs_eur = payable_costs_eur.add(kwh * euro_per_kwh, fill_value=0.0)

            # Other fuels / district heating (metered as heat consumption in Wh)
            elif isinstance(comp, FuelMeter):
                idx = find_output_col(comp.component_name, comp.HeatConsumption, "Wh")
                if idx is not None:
                    kwh = ppdt.results.iloc[:, idx] * 1e-3
                    loadtype = comp.config.fuel_loadtype.value
                    if loadtype == "DistrictHeating":
                        payable_costs_eur = payable_costs_eur.add(
                            kwh * factors.district_heating_costs_in_euro_per_kwh, fill_value=0.0
                        )
                    elif loadtype == "Oil":
                        if comp.config.heating_value_of_fuel_in_kwh_per_liter is None:
                            log.warning(
                                f"FuelMeter {comp.component_name} is Oil but has no heating value. Skipping oil costs."
                            )
                        else:
                            liters = kwh / float(comp.config.heating_value_of_fuel_in_kwh_per_liter)
                            payable_costs_eur = payable_costs_eur.add(
                                liters * factors.oil_costs_in_euro_per_l, fill_value=0.0
                            )
                    elif loadtype in {"Pellets", "WoodChips"}:
                        # Convert kWh -> liter-equivalent via heating value, then -> kg via density, then -> t
                        if (
                            comp.config.heating_value_of_fuel_in_kwh_per_liter is None
                            or comp.config.fuel_density_in_kg_per_m3 is None
                        ):
                            log.warning(
                                f"FuelMeter {comp.component_name} is {loadtype} but has no heating value/density. Skipping costs."
                            )
                        else:
                            liters = kwh / float(comp.config.heating_value_of_fuel_in_kwh_per_liter)
                            kg = liters * 1e-3 * float(comp.config.fuel_density_in_kg_per_m3)
                            tons = kg / 1000.0
                            euro_per_t = (
                                factors.pellet_costs_in_euro_per_t
                                if loadtype == "Pellets"
                                else factors.wood_chip_costs_in_euro_per_t
                            )
                            payable_costs_eur = payable_costs_eur.add(tons * euro_per_t, fill_value=0.0)

        net_costs_eur = payable_costs_eur.sub(revenue_from_feed_in_eur, fill_value=0.0)

        payable_col = "PostProcessing - OperationalCostsPayable [Any - EUR]"
        revenue_col = "PostProcessing - OperationalRevenueElectricityToGrid [Any - EUR]"
        net_col = "PostProcessing - OperationalCostsNet [Any - EUR]"
        # Backwards-compatible alias (previously included revenue as negative cost)
        #total_col = "PostProcessing - OperationalCostsTotal [Any - EUR]"

        ppdt.results[payable_col] = payable_costs_eur
        ppdt.results[revenue_col] = revenue_from_feed_in_eur
        ppdt.results[net_col] = net_costs_eur
        #ppdt.results[total_col] = net_costs_eur

        if ppdt.results_monthly is not None:
            ppdt.results_monthly[payable_col] = payable_costs_eur.resample("M").sum()
            ppdt.results_monthly[revenue_col] = revenue_from_feed_in_eur.resample("M").sum()
            ppdt.results_monthly[net_col] = net_costs_eur.resample("M").sum()
            #ppdt.results_monthly[total_col] = net_costs_eur.resample("M").sum()

    def add_car_operational_outputs(self, ppdt: PostProcessingDataTransfer) -> None:
        """Add car-specific energy demand, operational costs, and operational emissions.

        Supports:
        - Diesel cars via `hisim.components.generic_car.Car` output `FuelConsumption [liter]`
        - EV charging via `hisim.components.generic_ev_charger.EVCharger` output `ElectricityOutput [W]`
        - (Optional) EV driving electricity directly from `Car.ElectricityOutput [W]` if used in a setup

        Electricity costs/emissions are computed using the electricity factors for the simulation year/country.
        Diesel costs/emissions are computed using diesel factors for the simulation year/country.
        """
        if ppdt.results is None or len(ppdt.results.index) == 0:
            return

        # Import locally to avoid heavier imports at module load time
        from hisim.components.generic_car import Car as GenericCar  # pylint: disable=import-outside-toplevel
        from hisim.components.generic_ev_charger import EVCharger  # pylint: disable=import-outside-toplevel

        factors = EmissionFactorsAndCostsForFuelsConfig.get_values_for_year(
            year=ppdt.simulation_parameters.year,
            country=ppdt.simulation_parameters.country,
        )

        car_energy_kwh = pd.Series(0.0, index=ppdt.results.index)
        car_costs_eur = pd.Series(0.0, index=ppdt.results.index)
        car_emissions_kg = pd.Series(0.0, index=ppdt.results.index)

        def find_output_col(component_name: str, field_name: str, unit: str) -> int | None:
            for idx, outp in enumerate(ppdt.all_outputs):
                if outp.component_name == component_name and outp.field_name == field_name and outp.unit == unit:
                    return idx
            return None

        # Diesel energy conversion (used in generic_car KPIs too)
        heating_value_diesel_kwh_per_l = 9.8

        for wrapper in ppdt.wrapped_components:
            comp = wrapper.my_component

            # Diesel car: liters per timestep
            if isinstance(comp, GenericCar) and getattr(comp.config, "fuel", None) == lt.LoadTypes.DIESEL:
                idx_l = find_output_col(comp.component_name, comp.FuelConsumption, "Liter")
                if idx_l is not None:
                    liters = ppdt.results.iloc[:, idx_l]
                    kwh = liters * heating_value_diesel_kwh_per_l
                    car_energy_kwh = car_energy_kwh.add(kwh, fill_value=0.0)
                    car_costs_eur = car_costs_eur.add(liters * factors.diesel_costs_in_euro_per_l, fill_value=0.0)
                    car_emissions_kg = car_emissions_kg.add(liters * factors.diesel_footprint_in_kg_per_l, fill_value=0.0)

            # EV car (driving electricity directly): W -> kWh
            elif isinstance(comp, GenericCar) and getattr(comp.config, "fuel", None) == lt.LoadTypes.ELECTRICITY:
                idx_w = find_output_col(comp.component_name, comp.ElectricityOutput, "W")
                if idx_w is not None:
                    w = ppdt.results.iloc[:, idx_w].clip(lower=0.0)
                    kwh = w * ppdt.simulation_parameters.seconds_per_timestep / 3.6e6
                    car_energy_kwh = car_energy_kwh.add(kwh, fill_value=0.0)
                    car_costs_eur = car_costs_eur.add(kwh * factors.electricity_costs_in_euro_per_kwh, fill_value=0.0)
                    car_emissions_kg = car_emissions_kg.add(kwh * factors.electricity_footprint_in_kg_per_kwh, fill_value=0.0)

            # EV charging electricity: W -> kWh
            elif isinstance(comp, EVCharger):
                idx_w = find_output_col(comp.component_name, comp.ElectricityOutput, "W")
                if idx_w is not None:
                    w = ppdt.results.iloc[:, idx_w].clip(lower=0.0)
                    kwh = w * ppdt.simulation_parameters.seconds_per_timestep / 3.6e6
                    car_energy_kwh = car_energy_kwh.add(kwh, fill_value=0.0)
                    car_costs_eur = car_costs_eur.add(kwh * factors.electricity_costs_in_euro_per_kwh, fill_value=0.0)
                    car_emissions_kg = car_emissions_kg.add(kwh * factors.electricity_footprint_in_kg_per_kwh, fill_value=0.0)

        # Only add columns if any car demand was found
        if float(car_energy_kwh.sum()) == 0.0 and float(car_costs_eur.sum()) == 0.0 and float(car_emissions_kg.sum()) == 0.0:
            return

        energy_col = "PostProcessing - CarEnergyDemand [Any - kWh]"
        costs_col = "PostProcessing - CarOperationalCosts [Any - EUR]"
        emissions_col = "PostProcessing - CarOperationalEmissions [Any - kgCO2eq]"

        ppdt.results[energy_col] = car_energy_kwh
        ppdt.results[costs_col] = car_costs_eur
        ppdt.results[emissions_col] = car_emissions_kg

        if ppdt.results_monthly is not None:
            ppdt.results_monthly[energy_col] = car_energy_kwh.resample("M").sum()
            ppdt.results_monthly[costs_col] = car_costs_eur.resample("M").sum()
            ppdt.results_monthly[emissions_col] = car_emissions_kg.resample("M").sum()

    def add_actual_heat_supply_split_outputs(self, ppdt: PostProcessingDataTransfer) -> None:
        """Split actual delivered heating into space heating vs DHW.

        Many thermal producers mark their outputs with `postprocessing_flag` containing
        `InandOutputType.HEATING` (space heating) or `InandOutputType.WATER_HEATING` (DHW).
        This method tries to aggregate DHW via tags, and computes space heating as:
        `SpaceHeating = TotalActualHeatingSupply - DHWHeatingSupply` (clipped to >= 0),
        so that results are still available even if thermal producers are not tagged.

        It provides *power* [W] time series per timestep and the corresponding *energy per timestep* [Wh].
        """
        if ppdt.results is None or len(ppdt.results.index) == 0:
            return

        dt_s = float(ppdt.simulation_parameters.seconds_per_timestep)

        def _unit_eq(u: Any, expected: Any) -> bool:
            return u == expected or str(u) == str(expected)

        def _loadtype_eq(t: Any, expected: Any) -> bool:
            return t == expected or str(t) == str(expected)

        def find_output_col(component_name: str, field_name: str, unit: Any) -> int | None:
            for idx, outp in enumerate(ppdt.all_outputs):
                if outp.component_name == component_name and outp.field_name == field_name and _unit_eq(outp.unit, unit):
                    return idx
            return None

        # Total actual heating supply to the building (prefer Building output; fallback to 0)
        total_heating_supply_w = pd.Series(0.0, index=ppdt.results.index)
        try:
            idx_total = find_output_col("Building", "ActualHeatingSupply", lt.Units.WATT)
            if idx_total is None:
                # In multi-building cases, Building instance name can be prefixed (e.g. "BUI1_Building")
                for out_idx, outp in enumerate(ppdt.all_outputs):
                    if outp.field_name == "ActualHeatingSupply" and _unit_eq(outp.unit, lt.Units.WATT):
                        idx_total = out_idx
                        break
            if idx_total is not None:
                total_heating_supply_w = ppdt.results.iloc[:, idx_total].astype(float).clip(lower=0.0)
        except Exception:
            total_heating_supply_w = pd.Series(0.0, index=ppdt.results.index)

        # pick only "thermal power" outputs (W) for DHW, not energies (Wh)
        dhw_cols: list[int] = []
        for idx, outp in enumerate(ppdt.all_outputs):
            if not _unit_eq(outp.unit, lt.Units.WATT):
                continue
            if not _loadtype_eq(outp.load_type, lt.LoadTypes.HEATING):
                continue
            flags = outp.postprocessing_flag or []
            if lt.InandOutputType.WATER_HEATING in flags:
                dhw_cols.append(idx)

        dhw_power_w = ppdt.results.iloc[:, dhw_cols].sum(axis=1) if len(dhw_cols) > 0 else pd.Series(0.0, index=ppdt.results.index)

        # Keep only heating (positive part) to match "ActualHeatingSupply" semantics.
        dhw_power_w = dhw_power_w.clip(lower=0.0)

        # Space heating as residual (still gives results when components aren't tagged)
        sh_power_w = total_heating_supply_w.sub(dhw_power_w, fill_value=0.0).clip(lower=0.0)

        sh_energy_wh = sh_power_w * dt_s / 3600.0
        dhw_energy_wh = dhw_power_w * dt_s / 3600.0

        sh_power_col = "PostProcessing - ActualSpaceHeatingSupply [Any - W]"
        sh_energy_col = "PostProcessing - ActualSpaceHeatingEnergySupply [Any - Wh]"
        dhw_power_col = "PostProcessing - ActualDHWHeatingSupply [Any - W]"
        dhw_energy_col = "PostProcessing - ActualDHWHeatingEnergySupply [Any - Wh]"

        ppdt.results[sh_power_col] = sh_power_w
        ppdt.results[sh_energy_col] = sh_energy_wh
        ppdt.results[dhw_power_col] = dhw_power_w
        ppdt.results[dhw_energy_col] = dhw_energy_wh

        if ppdt.results_monthly is not None:
            ppdt.results_monthly[sh_energy_col] = sh_energy_wh.resample("M").sum()
            ppdt.results_monthly[dhw_energy_col] = dhw_energy_wh.resample("M").sum()

    def make_pkl_export(self, ppdt: PostProcessingDataTransfer) -> None:
        """Exports all data to Pickle."""
        log.information("Exporting to pkl.")
        self.export_results_to_pickle(ppdt)

    def make_monthly_bar_charts(
        self,
        ppdt: PostProcessingDataTransfer,
        report_image_entries: List[ReportImageEntry],
    ) -> None:
        """Make bar charts."""
        for index, output in enumerate(ppdt.all_outputs):
            my_bar = charts.BarChart(
                output=output.full_name,
                component_name=output.component_name,
                units=output.unit,
                directory_path=os.path.join(ppdt.simulation_parameters.result_directory),
                time_correction_factor=ppdt.time_correction_factor,
                output_description=output.output_description,
                figure_format=ppdt.simulation_parameters.figure_format,
            )
            my_entry = my_bar.plot(data=ppdt.results_monthly.iloc[:, index])
            report_image_entries.append(my_entry)

    def make_single_day_plots(
        self,
        days: Dict[str, int],
        ppdt: PostProcessingDataTransfer,
        report_image_entries: List[ReportImageEntry],
    ) -> None:
        """Makes plots for selected days."""
        for index, output in enumerate(ppdt.all_outputs):
            my_days = ChartSingleDay(
                output=output.full_name,
                component_name=output.component_name,
                units=output.unit,
                directory_path=ppdt.simulation_parameters.result_directory,
                time_correction_factor=ppdt.time_correction_factor,
                day=days["day"],
                month=days["month"],
                data=ppdt.results.iloc[:, index],
                output_description=output.output_description,
                figure_format=ppdt.simulation_parameters.figure_format,
            )
            my_entry = my_days.plot(close=True)
            report_image_entries.append(my_entry)

    def make_carpet_plots(
        self,
        ppdt: PostProcessingDataTransfer,
        report_image_entries: List[ReportImageEntry],
    ) -> None:
        """Make carpet plots."""
        for index, output in enumerate(ppdt.all_outputs):
            log.trace("Making carpet plots")
            my_carpet = charts.Carpet(
                output=output.full_name,
                component_name=output.component_name,
                units=output.unit,
                directory_path=ppdt.simulation_parameters.result_directory,
                time_correction_factor=ppdt.time_correction_factor,
                output_description=output.output_description,
                figure_format=ppdt.simulation_parameters.figure_format,
            )

            my_entry = my_carpet.plot(
                xdims=int((ppdt.simulation_parameters.end_date - ppdt.simulation_parameters.start_date).days),
                data=ppdt.results.iloc[:, index],
            )
            report_image_entries.append(my_entry)

    @utils.measure_memory_leak
    def make_line_plots(
        self,
        ppdt: PostProcessingDataTransfer,
        report_image_entries: List[ReportImageEntry],
    ) -> None:
        """Makes the line plots."""
        for index, output in enumerate(ppdt.all_outputs):
            if output.output_description is None:
                raise ValueError("Output description was missing for " + output.full_name)
            my_line = charts.Line(
                output=output.full_name,
                component_name=output.component_name,
                units=output.unit,
                directory_path=ppdt.simulation_parameters.result_directory,
                time_correction_factor=ppdt.time_correction_factor,
                output_description=output.output_description,
                figure_format=ppdt.simulation_parameters.figure_format,
            )
            my_entry = my_line.plot(data=ppdt.results.iloc[:, index])
            report_image_entries.append(my_entry)
            del my_line

    @utils.measure_execution_time
    def export_results_to_csv(self, ppdt: PostProcessingDataTransfer) -> None:
        """Exports the results to a CSV file."""

        if PostProcessingOptions.EXPORT_RESULTS_IN_ONE_FILE in ppdt.post_processing_options:
            csvfilename = os.path.join(
                ppdt.simulation_parameters.result_directory,
                "all_results.csv"
            )
            csvfilename = self.shorten_path(csvfilename)
            ppdt.results.to_csv(csvfilename, sep=",", decimal=".")
        else:
            for column in ppdt.results:
                csvfilename = os.path.join(
                    ppdt.simulation_parameters.result_directory,
                    f"{column.split(' ', 3)[0]}_{column.split(' ', 3)[2]}.csv",
                )
                csvfilename = self.shorten_path(csvfilename)
                ppdt.results[column].to_csv(csvfilename, sep=",", decimal=".")

            if PostProcessingOptions.EXPORT_MONTHLY_RESULTS in ppdt.post_processing_options:
                for column in ppdt.results_monthly:
                    csvfilename = os.path.join(
                        ppdt.simulation_parameters.result_directory,
                        f"{column.split(' ', 3)[0]}_{column.split(' ', 3)[2]}_monthly.csv",
                    )
                    header = [f"{column.split('[', 1)[0]} - monthly [" f"{column.split('[', 1)[1]}"]
                    csvfilename = self.shorten_path(csvfilename)
                    ppdt.results_monthly[column].to_csv(csvfilename, sep=",", decimal=".", header=header)

    @utils.measure_execution_time
    def export_results_to_pickle(self, ppdt: PostProcessingDataTransfer) -> None:
        """Exports the results to a Pickle file."""

        if PostProcessingOptions.EXPORT_RESULTS_IN_ONE_FILE in ppdt.post_processing_options:
            pickle_filename = os.path.join(
                ppdt.simulation_parameters.result_directory,
                "all_results.pkl"
            )
            pickle_filename = self.shorten_path(pickle_filename)
            with open(pickle_filename, "wb") as f:
                pickle.dump(ppdt.results, f)
        else:
            for column in ppdt.results:
                pickle_filename = os.path.join(
                    ppdt.simulation_parameters.result_directory,
                    f"{column.split(' ', 3)[0]}_{column.split(' ', 3)[2]}.pkl",
                )

                pickle_filename = self.shorten_path(pickle_filename)

                with open(pickle_filename, "wb") as f:
                    pickle.dump(ppdt.results[column], f)
            if PostProcessingOptions.EXPORT_MONTHLY_RESULTS in ppdt.post_processing_options:
                for column in ppdt.results_monthly:
                    pickle_filename_monthly = os.path.join(
                        ppdt.simulation_parameters.result_directory,
                        f"{column.split(' ', 3)[0]}_{column.split(' ', 3)[2]}_monthly.pkl",
                    )

                    pickle_filename_monthly = self.shorten_path(pickle_filename_monthly)

                    with open(pickle_filename_monthly, "wb") as f:
                        pickle.dump(ppdt.results_monthly[column], f)

    def shorten_path(self, path, max_length=250):
        """Shorten path if its longer than 250."""
        if len(path) <= max_length:
            return path

        dir_path, last_part = os.path.split(path)

        remove_length = len(path) - max_length

        remaining_length = len(last_part) - remove_length
        part_length = remaining_length // 2

        start = last_part[:part_length]
        end = last_part[-part_length:]

        shortened_last_part = f"{start}...{end}"

        shortend_path = os.path.join(dir_path, shortened_last_part)

        return shortend_path

    def write_simulation_parameters_to_report(
        self, ppdt: PostProcessingDataTransfer, report: reportgenerator.ReportGenerator
    ) -> None:
        """Write simulation parameters to report."""
        lines = ["The following information was used to configure the HiSim Building Simulation."]
        simulation_parameters_list = ppdt.simulation_parameters.get_unique_key_as_list()
        lines += simulation_parameters_list
        self.write_new_chapter_with_text_content_to_report(
            report=report,
            lines=lines,
            headline=". Simulation Parameters",
        )

    def write_components_to_report(
        self,
        ppdt: PostProcessingDataTransfer,
        report: reportgenerator.ReportGenerator,
        report_image_entries: List[ReportImageEntry],
    ) -> None:
        """Writes information about the components used in the simulation to the simulation report."""

        def write_image_entry_to_report_for_one_component(
            component: Any, report_image_entries_for_component: List[ReportImageEntry]
        ) -> None:
            """Write image entry to report for one component."""

            sorted_entries: List[ReportImageEntry] = sorted(
                report_image_entries_for_component, key=lambda x: x.output_type
            )
            output_explanations = []

            output_type_counter = 1
            report.add_spacer()
            report.write_heading_with_style_heading_one([str(self.chapter_counter) + ". " + component])
            if PostProcessingOptions.INCLUDE_CONFIGS_IN_PDF_REPORT in ppdt.post_processing_options:
                for wrapped_component in ppdt.wrapped_components:
                    if wrapped_component.my_component.component_name == component:
                        report.write_with_normal_alignment(
                            ["The following information was used to configure the component."]
                        )
                        component_content = wrapped_component.my_component.write_to_report()
                        report.write_with_normal_alignment(component_content)

            if PostProcessingOptions.INCLUDE_IMAGES_IN_PDF_REPORT in ppdt.post_processing_options:
                entry: ReportImageEntry
                for entry in sorted_entries:
                    # write output description only once for each output type
                    if entry.output_type not in output_explanations:
                        output_explanations.append(entry.output_type)
                        report.write_heading_with_style_heading_two(
                            [
                                str(self.chapter_counter)
                                + "."
                                + str(output_type_counter)
                                + " "
                                + entry.component_name
                                + " Output: "
                                + entry.output_type
                            ]
                        )
                        if entry.output_description is None:
                            raise ValueError("Component had no description: " + str(entry.component_name))
                        report.write_with_normal_alignment([entry.output_description])
                        output_type_counter = output_type_counter + 1
                    report.write_figures_to_report(entry.file_path)
                    report.write_with_center_alignment(
                        ["Fig." + str(self.figure_counter) + ": " + entry.component_name + " " + entry.output_type]
                    )
                    report.add_spacer()
                    self.figure_counter = self.figure_counter + 1
            report.page_break()
            self.chapter_counter = self.chapter_counter + 1

        report.open()
        # sort report image entries
        component_names = []
        for report_image_entry in report_image_entries:
            if report_image_entry.component_name not in component_names:
                component_names.append(report_image_entry.component_name)

        for component in component_names:
            output_types = []
            report_image_entries_for_component = []
            for report_image_entry in report_image_entries:
                if report_image_entry.component_name == component:
                    report_image_entries_for_component.append(report_image_entry)
                    if report_image_entry.output_type not in output_types:
                        output_types.append(report_image_entry.output_type)

            write_image_entry_to_report_for_one_component(component, report_image_entries_for_component)

        report.close()

    def write_all_outputs_to_report(
        self, ppdt: PostProcessingDataTransfer, report: reportgenerator.ReportGenerator
    ) -> None:
        """Write all outputs to report."""
        all_output_names: List[Optional[str]]
        all_output_names = []
        output: ComponentOutput
        for output in ppdt.all_outputs:
            all_output_names.append(output.full_name + " [" + output.unit + "]")
        self.write_new_chapter_with_text_content_to_report(
            report=report,
            lines=all_output_names,
            headline=". All Outputs",
        )

    def write_network_charts_to_report(
        self,
        ppdt: PostProcessingDataTransfer,
        report: reportgenerator.ReportGenerator,
        system_chart_entries: List[SystemChartEntry],
    ) -> None:
        """Write network charts to report."""
        report.open()
        report.write_heading_with_style_heading_one([str(self.chapter_counter) + ". System Network Charts"])
        for entry in system_chart_entries:
            report.write_figures_to_report_with_size_four_six(
                os.path.join(ppdt.simulation_parameters.result_directory, entry.path)
            )
            report.write_with_center_alignment(["Fig." + str(self.figure_counter) + ": " + entry.caption])
            self.figure_counter = self.figure_counter + 1
        self.chapter_counter = self.chapter_counter + 1
        report.page_break()
        report.close()

    def compute_kpis_and_write_to_report_and_to_ppdt(
        self,
        ppdt: PostProcessingDataTransfer,
        report: Optional[reportgenerator.ReportGenerator],
        building_objects_in_district_list: list,
    ) -> PostProcessingDataTransfer:
        """Computes KPI's and writes them to report and to ppdt kpi collection."""
        # initialize kpi data class and compute all kpi values
        kpi_data_class = KpiGenerator(
            post_processing_data_transfer=ppdt, building_objects_in_district_list=building_objects_in_district_list
        )
        # write kpi table to report if option is chosen
        if PostProcessingOptions.GENERATE_PDF_REPORT in ppdt.post_processing_options:
            kpi_table = kpi_data_class.return_table_for_report()
            if report is not None:
                self.write_new_chapter_with_table_to_report(
                    report=report,
                    table_as_list_of_list=kpi_table,
                    headline=". KPIs",
                    comment=["Here a comment on calculation of numbers will follow"],
                )
            else:
                raise ValueError("report is None but should be a ReportGenerator object.")

        # write kpi dict collection into ppdt
        ppdt.kpi_collection_dict = kpi_data_class.kpi_collection_dict_sorted
        return ppdt

    def compute_and_write_opex_costs_to_report(
        self,
        ppdt: PostProcessingDataTransfer,
        report: Optional[reportgenerator.ReportGenerator],
        building_objects_in_district_list: list,
    ) -> None:
        """Computes OPEX costs and operational CO2-emissions and writes them to report and csv."""
        opex_compute_return = opex_calculation(
            components=ppdt.wrapped_components,
            all_outputs=ppdt.all_outputs,
            postprocessing_results=ppdt.results,
            simulation_parameters=ppdt.simulation_parameters,
            building_objects_in_district_list=building_objects_in_district_list,
        )

        # write capex to report if option is chosen
        if PostProcessingOptions.GENERATE_PDF_REPORT in ppdt.post_processing_options:
            if report is not None:
                self.write_new_chapter_with_table_to_report(
                    report=report,
                    table_as_list_of_list=opex_compute_return,
                    headline=". Operational Costs and Emissions for simulated period",
                    comment=[
                        "\n",
                        "Comments:",
                        "Operational Costs are the sum of fuel costs and maintenance costs for the devices, calculated for the simulated period.",
                        "Emissions are fuel emissions emitted during simulad period.",
                        "Consumption for Diesel_Car in l, for EV in kWh.",
                    ],
                )
            else:
                raise ValueError("report is None but should be a ReportGenerator object.")

    def compute_and_write_capex_costs_to_report(
        self,
        ppdt: PostProcessingDataTransfer,
        report: Optional[reportgenerator.ReportGenerator],
        building_objects_in_district_list: list,
    ) -> None:
        """Computes CAPEX costs and CO2-emissions for production of devices and writes them to report and csv."""
        capex_compute_return = capex_calculation(
            components=ppdt.wrapped_components,
            simulation_parameters=ppdt.simulation_parameters,
            building_objects_in_district_list=building_objects_in_district_list,
        )
        # write capex to report if option is chosen
        if PostProcessingOptions.GENERATE_PDF_REPORT in ppdt.post_processing_options:
            if report is not None:
                self.write_new_chapter_with_table_to_report(
                    report=report,
                    table_as_list_of_list=capex_compute_return,
                    headline=". Investment Cost and CO2-Emissions of devices for simulated period",
                    comment=["Values for Battery are calculated with lifetime in cycles instead of lifetime in years"],
                )
            else:
                raise ValueError("report is None but should be a ReportGenerator object.")

    def write_new_chapter_with_text_content_to_report(
        self, report: reportgenerator.ReportGenerator, lines: List, headline: str
    ) -> None:
        """Write new chapter with headline and some general information e.g. KPIs to report."""
        report.open()
        report.write_heading_with_style_heading_one([str(self.chapter_counter) + headline])
        report.write_with_normal_alignment(lines)
        self.chapter_counter = self.chapter_counter + 1
        report.page_break()
        report.close()

    def write_new_chapter_with_table_to_report(
        self,
        report: reportgenerator.ReportGenerator,
        table_as_list_of_list: List,
        headline: str,
        comment: List,
    ) -> None:
        """Write new chapter with headline and a table to report."""
        report.open()
        report.write_heading_with_style_heading_one([str(self.chapter_counter) + headline])
        report.write_tables_to_report(table_as_list_of_list)
        report.write_with_normal_alignment(comment)
        self.chapter_counter = self.chapter_counter + 1
        report.page_break()
        report.close()

    def open_dir_in_file_explorer(self, ppdt: PostProcessingDataTransfer) -> None:
        """Opens files in given path.

        The keyword darwin is used for supporting macOS,
        xdg-open will be available on any unix client running X.
        """
        if sys.platform == "win32":
            os.startfile(os.path.realpath(ppdt.simulation_parameters.result_directory))  # noqa: B606
        else:
            log.information("Not on Windows. Can't open explorer.")

    def export_sankeys(self):
        """Exports Sankeys plots.

        ToDo: implement
        """
        pass  # noqa: unnecessary-pass

    @utils.measure_execution_time
    def prepare_results_for_scenario_evaluation(self, ppdt: PostProcessingDataTransfer) -> None:
        """Prepare the results for the scenario evaluation."""

        # create result data folder
        self.result_data_folder_for_scenario_evaluation = os.path.join(
            ppdt.simulation_parameters.result_directory, "result_data_for_scenario_evaluation"
        )
        if os.path.exists(self.result_data_folder_for_scenario_evaluation) is False:
            os.makedirs(self.result_data_folder_for_scenario_evaluation)
        else:
            log.information("This result data path exists already: " + self.result_data_folder_for_scenario_evaluation)

        # --------------------------------------------------------------------------------------------------------------------------------------------------------------
        # make dictionaries with pyam data structure yearly data

        simple_dict_cumulative_data: Dict = {
            "model": [],
            "scenario": [],
            "region": [],
            "variable": [],
            "unit": [],
            "year": [],
            "value": [],
        }

        # Set meta info
        self.model = f"HiSim_{ppdt.module_filename}"
        self.scenario = (
            SingletonSimRepository().get_entry(SingletonDictKeyEnum.RESULT_SCENARIO_NAME)
            if SingletonSimRepository().exist_entry(SingletonDictKeyEnum.RESULT_SCENARIO_NAME)
            else ""
        )
        self.region = (
            SingletonSimRepository().get_entry(SingletonDictKeyEnum.LOCATION)
            if SingletonSimRepository().exist_entry(SingletonDictKeyEnum.LOCATION)
            else ""
        )
        self.year = ppdt.simulation_parameters.year

        # Time series (wide format): first column is time, each variable its own column.
        # This replaces the previous long/stacked pyam-like format for the scenario-evaluation CSVs.
        time_configs = [
            ("hourly", ppdt.results_hourly),
            ("daily", ppdt.results_daily),
            ("monthly", ppdt.results_monthly),
        ]

        for time_res, df in time_configs:
            if df is None:
                continue
            wide_df = df.copy()
            wide_df.insert(0, "time", wide_df.index)
            filename = os.path.join(
                self.result_data_folder_for_scenario_evaluation,
                f"{time_res}_{ppdt.simulation_parameters.duration.days}_days.csv",
            )
            wide_df.to_csv(path_or_buf=filename, index=False)

        # got through all components and read output values, variables and units for simple_dict_cumulative_data
        for column in ppdt.results_cumulative:
            value = ppdt.results_cumulative[column].values[0]

            (
                variable_name,
                unit,
            ) = self.get_variable_name_and_unit_from_ppdt_results_column(column=str(column))

            simple_dict_cumulative_data["model"].append(self.model)
            simple_dict_cumulative_data["scenario"].append(self.scenario)
            simple_dict_cumulative_data["region"].append(self.region)
            simple_dict_cumulative_data["variable"].append(variable_name)
            simple_dict_cumulative_data["unit"].append(unit)
            simple_dict_cumulative_data["year"].append(self.year)
            simple_dict_cumulative_data["value"].append(value)

        # add kpis to yearly dict
        if PostProcessingOptions.COMPUTE_KPIS in ppdt.post_processing_options:
            simple_dict_cumulative_data = self.write_kpis_in_dict(
                ppdt=ppdt, simple_dict_cumulative_data=simple_dict_cumulative_data
            )

        # Yearly / cumulative (wide format): one row with columns per output.
        if ppdt.results_cumulative is not None:
            yearly_wide_df = ppdt.results_cumulative.copy()
            yearly_wide_df.insert(0, "year", self.year)
            filename = os.path.join(
                self.result_data_folder_for_scenario_evaluation,
                f"yearly_{ppdt.simulation_parameters.duration.days}_days.csv",
            )
            yearly_wide_df.to_csv(path_or_buf=filename, index=False)

        self.write_config_data_for_scenario_evaluation(ppdt)

    def write_config_data_for_scenario_evaluation(self, ppdt: PostProcessingDataTransfer) -> None:
        """Prepare the results for the scenario evaluation."""
        # create dictionary with all import data information
        if PostProcessingOptions.PREPARE_OUTPUTS_FOR_SCENARIO_EVALUATION in ppdt.post_processing_options:
            result_data_folder_for_scenario_evaluation = os.path.join(
                ppdt.simulation_parameters.result_directory, "result_data_for_scenario_evaluation"
            )
            if os.path.exists(result_data_folder_for_scenario_evaluation) is False:
                os.makedirs(result_data_folder_for_scenario_evaluation)
        else:
            result_data_folder_for_scenario_evaluation = ppdt.simulation_parameters.result_directory

        self.model = "".join(["HiSim_", ppdt.module_filename])

        # set pyam scenario name
        if SingletonSimRepository().exist_entry(key=SingletonDictKeyEnum.RESULT_SCENARIO_NAME):
            self.scenario = SingletonSimRepository().get_entry(key=SingletonDictKeyEnum.RESULT_SCENARIO_NAME)
        else:
            self.scenario = ""

        # set region
        if SingletonSimRepository().exist_entry(key=SingletonDictKeyEnum.LOCATION):
            self.region = SingletonSimRepository().get_entry(key=SingletonDictKeyEnum.LOCATION)
        else:
            self.region = ""

        # set year or timeseries
        self.year = ppdt.simulation_parameters.year

        data_information_dict = {
            "model": self.model,
            "scenario": self.scenario,
            "region": self.region,
            "year": self.year,
            "duration in days": ppdt.simulation_parameters.duration.days,
        }

        # write json config with all component configs, module config, pyam information dict and simulation parameters
        json_generator_config = JsonConfigurationGenerator(name=f"{self.scenario}")
        json_generator_config.set_simulation_parameters(my_simulation_parameters=ppdt.simulation_parameters)
        if ppdt.my_module_config is not None:
            json_generator_config.set_module_config(my_module_config=ppdt.my_module_config)
        json_generator_config.set_scenario_data_information_dict(scenario_data_information_dict=data_information_dict)
        for component in ppdt.wrapped_components:
            json_generator_config.add_component(config=component.my_component.config)

        # save the json config
        json_generator_config.save_to_json(
            filename=os.path.join(result_data_folder_for_scenario_evaluation, "data_for_scenario_evaluation.json")
        )

    def write_component_configurations_to_json(self, ppdt: PostProcessingDataTransfer) -> None:
        """Collect all component configurations and write into JSON file in result directory."""
        json_generator_config = JsonConfigurationGenerator(name=f"{self.scenario}")
        for component in ppdt.wrapped_components:
            json_generator_config.add_component(config=component.my_component.config)
        json_generator_config.save_to_json(
            filename=os.path.join(
                ppdt.simulation_parameters.result_directory,
                "component_configurations.json",
            )
        )

    def write_kpis_in_dict(
        self,
        ppdt: PostProcessingDataTransfer,
        simple_dict_cumulative_data: Dict[str, Any],
    ) -> Dict:
        """Write kpis in dictionary."""
        # get kpis from ppdt
        try:
            kpi_collection_dict = ppdt.kpi_collection_dict["BUI1"]
        except Exception as exc:
            raise KeyError(f"Key Error BUI1. Dict is {ppdt.kpi_collection_dict}.") from exc

        for kpi_entries in kpi_collection_dict.values():
            for kpi_name, kpi_entry in kpi_entries.items():
                variable_name = kpi_name
                variable_value = kpi_entry["value"]
                variable_unit = kpi_entry["unit"]

                simple_dict_cumulative_data["model"].append(self.model)
                simple_dict_cumulative_data["scenario"].append(self.scenario)
                simple_dict_cumulative_data["region"].append(self.region)
                simple_dict_cumulative_data["variable"].append(variable_name)
                simple_dict_cumulative_data["unit"].append(variable_unit)
                try:
                    simple_dict_cumulative_data["year"].append(self.year)
                except Exception as exc:
                    # simple_dict_cumulative_data["time"].append(self.year)
                    raise KeyError(
                        "KPI values should be written only to yearly or cumulative data, not to timeseries data."
                    ) from exc
                simple_dict_cumulative_data["value"].append(variable_value)
        return simple_dict_cumulative_data

    def get_variable_name_and_unit_from_ppdt_results_column(self, column: str) -> Tuple[str, str]:
        """Get variable name and unit for pyam dictionary."""

        column_splitted = str(
            "".join([x for x in column if x in string.ascii_letters + "'- " + string.digits + "_" + "°" + "/"])
        ).split(sep=" ")

        variable_name = "".join([column_splitted[0], "|", column_splitted[3], "|", column_splitted[2]])

        unit = column_splitted[5]

        return variable_name, unit

    def iterate_over_results_and_add_values_to_dict(self, results_df: pd.DataFrame, timeseries: Any) -> pd.DataFrame:
        """Iterate over results and add values to dict, write to dataframe and save as csv."""

        column_meta = {col: self.get_variable_name_and_unit_from_ppdt_results_column(col) for col in results_df.columns}
        frames = []

        for col in results_df.columns:
            values = results_df[col].values
            variable_name, unit = column_meta[col]
            frames.append(
                pd.DataFrame(
                    {
                        "model": self.model,
                        "scenario": self.scenario,
                        "region": self.region,
                        "variable": variable_name,
                        "unit": unit,
                        "time": timeseries,
                        "value": values,
                    }
                )
            )

        return pd.concat(frames, ignore_index=True)

    def write_filename_and_save_to_csv(
        self,
        dataframe: pd.DataFrame,
        folder: str,
        time_resolution_of_data: str,
        simulation_duration: int,
    ) -> None:
        """Write file to csv."""

        filename = os.path.join(
            folder,
            f"{time_resolution_of_data}_{simulation_duration}_days.csv",
        )

        dataframe.to_csv(path_or_buf=filename, index=None)  # type: ignore

    def write_operation_data_for_webtool(self, ppdt: PostProcessingDataTransfer) -> None:
        """Collect daily operation results and write into json for webtool."""

        # Get bools that tells if the output should be displayed in webtool
        component_display_in_webtool: list[str] = []
        for output in ppdt.all_outputs:
            if output.postprocessing_flag:
                if OutputPostprocessingRules.DISPLAY_IN_WEBTOOL in output.postprocessing_flag:
                    component_display_in_webtool.append(output.get_pretty_name())

        results_daily = ppdt.results_daily[component_display_in_webtool]
        data = results_daily.to_json(date_format="iso")

        # Write to file
        with open(
            os.path.join(ppdt.simulation_parameters.result_directory, "results_daily_operation_for_webtool.json"),
            "w",
            encoding="utf-8",
        ) as file:
            file.write(data)

    def write_results_for_webtool_to_json_file(
        self, ppdt: PostProcessingDataTransfer, building_objects_in_district_list: list
    ) -> None:
        """Collect results and write into json for webtool."""

        # Check if important options were set
        if all(
            option in ppdt.post_processing_options
            for option in [
                PostProcessingOptions.COMPUTE_KPIS,
                PostProcessingOptions.COMPUTE_CAPEX,
                PostProcessingOptions.COMPUTE_OPEX,
            ]
        ):
            # Get KPIs from ppdt
            print("KPIs and costs are collected!")
            kpi_collection_dict = ppdt.kpi_collection_dict

            # Calculate capex
            capex_compute_return = capex_calculation(
                components=ppdt.wrapped_components,
                simulation_parameters=ppdt.simulation_parameters,
                building_objects_in_district_list=building_objects_in_district_list,
            )

            # Calculate opex
            opex_compute_return = opex_calculation(
                components=ppdt.wrapped_components,
                all_outputs=ppdt.all_outputs,
                postprocessing_results=ppdt.results,
                simulation_parameters=ppdt.simulation_parameters,
                building_objects_in_district_list=building_objects_in_district_list,
            )

            # Consolidate results into structured dataclass for webtool
            webtool_results_dataclass = WebtoolDict(  # type: ignore
                kpis=kpi_collection_dict,
                post_processing_data_transfer=ppdt,
                computed_opex=opex_compute_return,
                computed_capex=capex_compute_return,
            )

            # Save dataclass as json file in results folder
            json_file = webtool_results_dataclass.to_json(indent=4)
            with open(
                os.path.join(ppdt.simulation_parameters.result_directory, "results_for_webtool.json"),
                "w",
                encoding="utf-8",
            ) as file:
                file.write(json_file)

        else:
            raise ValueError(
                "Some PostProcessingOptions are not set. Please check if "
                f"{PostProcessingOptions.COMPUTE_KPIS}, {PostProcessingOptions.COMPUTE_CAPEX} and "
                f"{PostProcessingOptions.COMPUTE_OPEX} are set in your system setup."
            )

    def write_kpis_to_json_file(self, ppdt: PostProcessingDataTransfer) -> None:
        """Write all KPIs o json file."""

        # Check if important options were set
        if PostProcessingOptions.COMPUTE_KPIS in ppdt.post_processing_options:
            # Get KPIs from ppdt
            kpi_collection_dict = ppdt.kpi_collection_dict

            pathname = os.path.join(ppdt.simulation_parameters.result_directory, "all_kpis.json")
            with open(pathname, "w", encoding="utf-8") as outfile:
                json.dump(kpi_collection_dict, outfile, indent=5)

        else:
            raise ValueError(
                "Some PostProcessingOptions are not set. Please check if "
                f"{PostProcessingOptions.COMPUTE_KPIS} is set in your system setup."
            )

    def write_kpis_to_json_for_building_sizer(
        self, ppdt: PostProcessingDataTransfer, building_objects_in_district_list: list
    ) -> None:
        """Write KPIs to json file for building sizer."""

        def get_kpi_entries_for_building_sizer(data, target_key):
            """Get kpi entries for building sizer."""
            result = None
            for key1, value1 in data.items():
                if key1 == target_key:
                    result = value1["value"]
                if isinstance(value1, dict):
                    for key2, value2 in value1.items():
                        if key2 == target_key:
                            result = value2["value"]
            return result

        def fallback_conditioned_floor_area_from_building(building_object: str) -> Optional[float]:
            """Try to read conditioned floor area directly from the Building component."""
            try:
                for wrapper in ppdt.wrapped_components:
                    comp = wrapper.my_component
                    if isinstance(comp, building.Building):
                        # In multi-building runs, component_name is prefixed (e.g., "BUI1_Building")
                        if ppdt.simulation_parameters.multiple_buildings:
                            if not comp.component_name.startswith(building_object):
                                continue
                        return float(getattr(comp, "my_building_information").scaled_conditioned_floor_area_in_m2)
            except Exception:
                return None
            return None

        def get_kpi_value_or_default(data: dict, target_key: str, default: float = 0.0) -> float:
            """Get KPI value for target_key or return default (as float)."""
            value = get_kpi_entries_for_building_sizer(data=data, target_key=target_key)
            if value is None:
                return float(default)
            try:
                return float(value)
            except Exception:
                return float(default)

        kpi_dict = {}

        # Check if important options were set
        if PostProcessingOptions.COMPUTE_KPIS in ppdt.post_processing_options:
            for building_object in building_objects_in_district_list:
                # Get KPIs from ppdt

                kpi_collection_dict = ppdt.kpi_collection_dict[building_object]
                # conditioned floor area
                conditioned_floor_area_in_m2 = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Conditioned floor area"
                )
                if conditioned_floor_area_in_m2 is None:
                    conditioned_floor_area_in_m2 = fallback_conditioned_floor_area_from_building(building_object)
                if conditioned_floor_area_in_m2 is None:
                    raise KeyError(
                        "Could not determine 'Conditioned floor area' for building sizer export. "
                        "KPI not present and Building fallback failed."
                    )
                # Total costs
                annualized_total_costs_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Total costs for simulated period"
                )
                # Investment costs
                annualized_investment_costs_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Investment costs for equipment per simulated period"
                )
                annualized_net_investment_costs_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict,
                    target_key="Investment costs for equipment per simulated period minus subsidies",
                )
                # Total upfront net investment costs
                total_upfront_net_investment_costs_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Investment costs upfront for equipment period minus subsidies"
                )
                # Energy costs
                total_annualized_energy_costs_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Energy grid costs for simulated period"
                )
                annualzed_energy_costs_electricity_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Costs of grid electricity for simulated period"
                )
                annualized_energy_costs_gas_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Costs of grid gas for simulated period"
                )
                annualized_energy_costs_heating_fuels_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Costs of other heating fuels for simulated period"
                )
                # Maintenance costs
                annualized_maintenance_costs_in_euro = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Maintenance costs for simulated period"
                )
                # CO2 emissions
                annualized_total_co2_emissions_in_kg = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Total CO2 emissions for simulated period"
                )
                # CO2 emissions fromd evices
                annualized_co2_emissions_from_devices_in_kg = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="CO2 footprint for equipment per simulated period"
                )
                # CO2 emissions from energy consumption
                annualized_electricity_co2_emissions_in_kg = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="CO2 footprint of grid electricity for simulated period"
                )
                annualized_gas_co2_emissions_in_kg = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="CO2 footprint of grid gas for simulated period"
                )
                annualized_heating_fuels_co2_emissions_in_kg = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="CO2 footprint of other heating fuels for simulated period"
                )
                annualized_energy_co2_emissions_in_kg = (
                    annualized_electricity_co2_emissions_in_kg
                    + annualized_gas_co2_emissions_in_kg
                    + annualized_heating_fuels_co2_emissions_in_kg
                )

                # Other
                self_sufficiency_rate_electricity_in_percent = get_kpi_value_or_default(
                    data=kpi_collection_dict, target_key="Self-sufficiency rate according to solar htw berlin", default=0.0
                )
                self_sufficiency_rate_all_energy_in_percent = get_kpi_value_or_default(
                    data=kpi_collection_dict, target_key="Total energy self-suffiency rate", default=0.0
                )
                annualized_purchased_energy_consumption_in_kwh = get_kpi_value_or_default(
                    data=kpi_collection_dict, target_key="Purchased energy consumption for simulated period", default=0.0
                )
                annualized_electricity_to_grid_in_kwh = get_kpi_value_or_default(
                    data=kpi_collection_dict, target_key="Total energy to grid", default=0.0
                )
                annualized_electricity_from_grid_in_kwh = get_kpi_value_or_default(
                    data=kpi_collection_dict, target_key="Total energy from grid", default=0.0
                )
                minimum_indoor_temperature_in_celsius = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Minimum building indoor air temperature reached"
                )
                maximum_indoor_temperature_in_celsius = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict, target_key="Maximum building indoor air temperature reached"
                )
                deviation_from_minimum_indoor_temperature_in_celsius_hour = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict,
                    target_key="Temperature deviation of building indoor air temperature being below set temperature 20.0 Celsius",
                )
                deviation_from_maximum_indoor_temperature_in_celsius_hour = get_kpi_entries_for_building_sizer(
                    data=kpi_collection_dict,
                    target_key="Temperature deviation of building indoor air temperature being above set temperature 25.0 Celsius",
                )

                # initialize json interface to pass kpi's to building_sizer
                kpi_config = KPIConfig(
                    self_sufficiency_rate_electricity_in_percent=self_sufficiency_rate_electricity_in_percent,
                    self_sufficiency_rate_all_energy_in_percent=self_sufficiency_rate_all_energy_in_percent,
                    annualized_total_costs_in_euro_per_m2=annualized_total_costs_in_euro / conditioned_floor_area_in_m2,
                    total_upfront_net_investment_costs_in_euro=total_upfront_net_investment_costs_in_euro,
                    annualized_total_co2_emissions_in_kg_per_m2=annualized_total_co2_emissions_in_kg
                    / conditioned_floor_area_in_m2,
                    annualized_co2_emissions_for_devices_in_kg_per_m2=annualized_co2_emissions_from_devices_in_kg
                    / conditioned_floor_area_in_m2,
                    annualized_energy_co2_emissions_in_kg_per_m2=annualized_energy_co2_emissions_in_kg
                    / conditioned_floor_area_in_m2,
                    annualized_electricity_co2_emissions_in_kg_per_m2=annualized_electricity_co2_emissions_in_kg
                    / conditioned_floor_area_in_m2,
                    annualized_gas_co2_emissions_in_kg_per_m2=annualized_gas_co2_emissions_in_kg
                    / conditioned_floor_area_in_m2,
                    annualized_heat_co2_emissions_in_kg_per_m2=annualized_heating_fuels_co2_emissions_in_kg
                    / conditioned_floor_area_in_m2,
                    annualized_energy_costs_in_euro_per_m2=total_annualized_energy_costs_in_euro
                    / conditioned_floor_area_in_m2,
                    annualized_electricity_costs_in_euro_per_m2=annualzed_energy_costs_electricity_in_euro
                    / conditioned_floor_area_in_m2,
                    annualized_gas_costs_in_euro_per_m2=annualized_energy_costs_gas_in_euro
                    / conditioned_floor_area_in_m2,
                    annualized_heat_costs_in_euro_per_m2=annualized_energy_costs_heating_fuels_in_euro
                    / conditioned_floor_area_in_m2,
                    annualized_maintenance_costs_in_euro_per_m2=annualized_maintenance_costs_in_euro
                    / conditioned_floor_area_in_m2,
                    annualized_investment_costs_in_euro_per_m2=annualized_investment_costs_in_euro
                    / conditioned_floor_area_in_m2,
                    annualized_net_investment_costs_in_euro_per_m2=annualized_net_investment_costs_in_euro
                    / conditioned_floor_area_in_m2,
                    annualized_purchased_energy_consumption_in_kwh_per_m2=annualized_purchased_energy_consumption_in_kwh
                    / conditioned_floor_area_in_m2,
                    annualized_electricity_to_grid_in_kwh_per_m2=annualized_electricity_to_grid_in_kwh
                    / conditioned_floor_area_in_m2,
                    annualized_electricity_from_grid_in_kwh_per_m2=annualized_electricity_from_grid_in_kwh
                    / conditioned_floor_area_in_m2,
                    minimum_indoor_temperature_in_celsius=minimum_indoor_temperature_in_celsius,
                    maximum_indoor_temperature_in_celsius=maximum_indoor_temperature_in_celsius,
                    deviation_from_max_indoor_temperature_in_celsius_hour=deviation_from_maximum_indoor_temperature_in_celsius_hour,
                    deviation_from_min_indoor_temperature_in_celsius_hour=deviation_from_minimum_indoor_temperature_in_celsius_hour,
                )

                kpi_dict = kpi_config.to_dict()  # type: ignore

                pathname = os.path.join(
                    ppdt.simulation_parameters.result_directory, f"{building_object}_kpi_config_for_building_sizer.json"
                )
                config_file_written = json.dumps(kpi_dict, ensure_ascii=False, indent=4)
                with open(pathname, "w", encoding="utf-8") as outfile:
                    outfile.write(config_file_written)

        else:
            raise ValueError(
                "Some PostProcessingOptions are not set. Please check if "
                f"{PostProcessingOptions.COMPUTE_KPIS} is set in your system setup."
            )

    def get_dict_from_opex_capex_lists(self, value_list: List[str]) -> Dict[str, Any]:
        """Get dict with values for webtool from opex capex lists."""

        dict_with_cost_values = {}
        dict_with_emission_values = {}
        dict_with_lifetime_values = {}

        total_dict = {}

        name_one = value_list[0]

        for value_unit in value_list:
            if "---" not in value_unit:
                variable_name = "".join(x for x in value_unit[0] if x != ":")
                variable_value_investment = value_unit[1]
                variable_value_emissions = value_unit[2]
                variable_value_lifetime = value_unit[3]

                dict_with_cost_values.update({f"{variable_name} [{name_one[1]}] ": variable_value_investment})
                dict_with_emission_values.update({f"{variable_name} [{name_one[2]}] ": variable_value_emissions})
                dict_with_lifetime_values.update({f"{variable_name} [{name_one[3]}] ": variable_value_lifetime})

                total_dict.update(
                    {
                        "column 1": dict_with_cost_values,
                        "column 2": dict_with_emission_values,
                        "column 3": dict_with_lifetime_values,
                    }
                )

        return total_dict

    def get_building_object_in_district(self, ppdt: PostProcessingDataTransfer) -> list[str]:
        """Get building names in district."""

        building_objects_in_district = set()

        for wrapped_component in ppdt.wrapped_components:
            building_objects_in_district.add(wrapped_component.my_component.config.building_name)

        building_objects_in_district_list = list(building_objects_in_district)

        return building_objects_in_district_list
