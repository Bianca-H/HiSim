"""Collect and render Swiss CH building archetype documentation (PDF).

Resolves archetype geometry, TABULA-derived properties, SIA 2024 internal loads,
and Swiss CH batch building assumptions (ventilation / infiltration) for PDF export.
"""

from __future__ import annotations

import json
import tempfile
import xml.sax.saxutils
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from hisim import cli_overrides
from hisim.components import building, sia2024_occupancy

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHETYPES: Tuple[str, ...] = tuple(f"{i:02d}_CH" for i in range(1, 10))

WEATHER_DESIGN_OUTDOOR_TEMP_C: Dict[str, float] = {
    "ZUESTA": -8.0,
    "BASSTA": -7.0,
    "KLO": -9.0,
    "RUE": -10.0,
}

# LaTeX-like typography (Computer Modern approximated with Times-Roman in ReportLab).
FONT_BODY = "Times-Roman"
FONT_BODY_BOLD = "Times-Bold"
FONT_BODY_ITALIC = "Times-Italic"
FONT_SIZE_BODY = 11.0
FONT_SIZE_SECTION = 17.3
FONT_SIZE_SUBSECTION = 14.4


@dataclass
class PdfTypography:
    """Paragraph styles for archetype PDF export."""

    section: ParagraphStyle
    subsection: ParagraphStyle
    body: ParagraphStyle
    table_cell: ParagraphStyle
    table_header: ParagraphStyle
    caption: ParagraphStyle


def _escape_pdf_text(text: str) -> str:
    return xml.sax.saxutils.escape(str(text))


def build_pdf_typography() -> PdfTypography:
    """Build styles similar to 11pt Computer Modern / LaTeX article."""
    base = getSampleStyleSheet()
    return PdfTypography(
        section=ParagraphStyle(
            "ArchSection",
            parent=base["Normal"],
            fontName=FONT_BODY_BOLD,
            fontSize=FONT_SIZE_SECTION,
            leading=FONT_SIZE_SECTION * 1.15,
            spaceBefore=14,
            spaceAfter=8,
            alignment=TA_LEFT,
        ),
        subsection=ParagraphStyle(
            "ArchSubsection",
            parent=base["Normal"],
            fontName=FONT_BODY_BOLD,
            fontSize=FONT_SIZE_SUBSECTION,
            leading=FONT_SIZE_SUBSECTION * 1.2,
            spaceBefore=10,
            spaceAfter=6,
            alignment=TA_LEFT,
        ),
        body=ParagraphStyle(
            "ArchBody",
            parent=base["Normal"],
            fontName=FONT_BODY,
            fontSize=FONT_SIZE_BODY,
            leading=FONT_SIZE_BODY * 1.2,
            spaceAfter=6,
            alignment=TA_LEFT,
        ),
        table_cell=ParagraphStyle(
            "ArchTableCell",
            parent=base["Normal"],
            fontName=FONT_BODY,
            fontSize=FONT_SIZE_BODY,
            leading=FONT_SIZE_BODY * 1.25,
            alignment=TA_LEFT,
        ),
        table_header=ParagraphStyle(
            "ArchTableHeader",
            parent=base["Normal"],
            fontName=FONT_BODY_BOLD,
            fontSize=FONT_SIZE_BODY,
            leading=FONT_SIZE_BODY * 1.25,
            alignment=TA_LEFT,
        ),
        caption=ParagraphStyle(
            "ArchCaption",
            parent=base["Normal"],
            fontName=FONT_BODY,
            fontSize=FONT_SIZE_BODY,
            leading=FONT_SIZE_BODY * 1.2,
            spaceBefore=4,
            spaceAfter=4,
            alignment=TA_LEFT,
        ),
    )


def _table_cell_paragraph(text: str, typo: PdfTypography, *, header: bool = False) -> Paragraph:
    style = typo.table_header if header else typo.table_cell
    return Paragraph(_escape_pdf_text(text), style)


def _latex_table(
    data: List[List[str]],
    col_widths: List[float],
    typo: PdfTypography,
) -> Table:
    """Booktabs-like table with wrapped cells."""
    if not data:
        raise ValueError("empty table")
    table_data: List[List[Any]] = []
    for row_index, row in enumerate(data):
        table_data.append(
            [_table_cell_paragraph(cell, typo, header=(row_index == 0)) for cell in row]
        )
    table = Table(table_data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    rule = colors.black
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), FONT_BODY),
                ("FONTSIZE", (0, 0), (-1, -1), FONT_SIZE_BODY),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEABOVE", (0, 0), (-1, 0), 0.75, rule),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, rule),
                ("LINEBELOW", (0, -1), (-1, -1), 0.75, rule),
            ]
        )
    )
    return table


def _widths_from_ratios(total_width: float, ratios: Sequence[float]) -> List[float]:
    ratio_sum = float(sum(ratios))
    return [total_width * (r / ratio_sum) for r in ratios]


@dataclass
class DocumentationContext:
    """Reference assumptions printed in the PDF header."""

    weather: str = "ZUESTA"
    time_horizon: str = "present"
    scenario: str = "none"
    occupancy_mode: str = "SIA2024"
    sia_use_type: str = "residential"
    # When True, mirror hp01-style building config (20.5 °C heat, cooling demand disabled at 99 °C).
    ch_batch_building_overrides: bool = True


def _fmt(value: Any, *, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _schedule_people_description(cfg: sia2024_occupancy.SIA2024OccupancyConfig) -> str:
    return (
        "Weekly hourly profile (168 h, residential); "
        f"sensible heat {cfg.sensible_heat_gain_per_person_in_watt} W per person"
    )


def _schedule_appliances_description(cfg: sia2024_occupancy.SIA2024OccupancyConfig) -> str:
    return "Weekly hourly profile (168 h, residential)"


def _schedule_lighting_description(cfg: sia2024_occupancy.SIA2024OccupancyConfig) -> str:
    if cfg.lighting_schedule_weekly_hourly is not None:
        return "SIA 2024 residential Nutzungsstunden (168 h weekly shape)"
    return "Follows occupancy (people present)"


def _seasonal_people_description(cfg: sia2024_occupancy.SIA2024OccupancyConfig) -> str:
    return (
        f"Constant monthly multiplier on weekly profile: "
        f"{cfg.person_yearly_utilization_per_month} (SIA presence utilisation)"
    )


def _seasonal_appliances_description(cfg: sia2024_occupancy.SIA2024OccupancyConfig) -> str:
    util = float(cfg.appliances_yearly_utilization_per_month)
    if abs(util - 1.0) < 1e-9:
        return "None (uniform year-round)"
    return (
        f"Uniform annual multiplier on weekly profile: {util} "
        "(SIA t_A,Ps vs daily equivalent)"
    )


def _seasonal_lighting_description(cfg: sia2024_occupancy.SIA2024OccupancyConfig) -> str:
    if cfg.lighting_schedule_weekly_hourly is None:
        return "—"
    flh = cfg.lighting_annual_full_load_hours
    amp = cfg.lighting_seasonal_variation_amplitude
    parts = []
    if flh is not None:
        parts.append(f"Annual full-load target t_L = {flh} h")
    if amp and float(amp) > 0:
        parts.append(f"Monthly scale 1 + {amp}·cos(2π·m/12) (winter higher)")
    return "; ".join(parts) if parts else "None (raw weekly fractions only)"


def resolve_archetype_models(
    archetype: str,
    ctx: DocumentationContext,
) -> Tuple[building.BuildingConfig, building.BuildingInformation, sia2024_occupancy.SIA2024OccupancyConfig]:
    """Build resolved building + occupancy configs for one archetype."""
    cli_overrides.set_overrides(
        {
            "ARCH": archetype,
            "WEATHER": ctx.weather,
            "TIME_HORIZON": ctx.time_horizon,
            "SCENARIO": ctx.scenario if ctx.scenario.lower() != "none" else "",
            "OCC": ctx.occupancy_mode,
            "SIA_USE": ctx.sia_use_type,
        }
    )

    bcfg = cli_overrides.apply_building_archetype_override(building_module=building, arch_value=archetype)
    if ctx.weather in WEATHER_DESIGN_OUTDOOR_TEMP_C:
        bcfg.heating_reference_temperature_in_celsius = WEATHER_DESIGN_OUTDOOR_TEMP_C[ctx.weather]

    if ctx.ch_batch_building_overrides:
        bcfg.set_heating_temperature_in_celsius = cli_overrides.DEFAULT_HEATING_SETPOINT_IN_CELSIUS
        bcfg.set_cooling_temperature_in_celsius = 99.0
    cli_overrides.apply_scenario_building_settings(bcfg)
    cli_overrides.apply_swiss_sia_natural_ventilation_settings(bcfg, arch_value=archetype)

    binfo = building.BuildingInformation(config=bcfg)
    floor_area = float(bcfg.absolute_conditioned_floor_area_in_m2 or 0.0)
    occ_cfg = sia2024_occupancy.SIA2024OccupancyConfig.get_default_for_use_type(
        conditioned_floor_area_in_m2=floor_area,
        use_type=ctx.sia_use_type,
        building_name="BUI1",
        name="SIA2024Occupancy",
    )
    return bcfg, binfo, occ_cfg


def geometry_table_rows(bcfg: building.BuildingConfig, binfo: building.BuildingInformation) -> List[List[str]]:
    g_gl = float(binfo.total_solar_energy_transmittance_for_perpedicular_radiation)
    rows: List[List[str]] = [
        ["Component", "Area [m²]", "U-value [W/(m²·K)]", "g-value [-]"],
    ]
    components = [
        ("Floor", bcfg.floor_area_in_m2, bcfg.floor_u_value_in_watt_per_m2_per_kelvin, None),
        ("Facade (opaque)", bcfg.facade_area_in_m2, bcfg.facade_u_value_in_watt_per_m2_per_kelvin, None),
        ("Roof", bcfg.roof_area_in_m2, bcfg.roof_u_value_in_watt_per_m2_per_kelvin, None),
        ("Windows", bcfg.window_area_in_m2, bcfg.window_u_value_in_watt_per_m2_per_kelvin, g_gl),
        ("Doors", bcfg.door_area_in_m2, bcfg.door_u_value_in_watt_per_m2_per_kelvin, None),
    ]
    for name, area, u_val, g_val in components:
        if area is None and u_val is None:
            continue
        rows.append([name, _fmt(area), _fmt(u_val), _fmt(g_val) if g_val is not None else "—"])
    return rows


def thermal_mass_rows(bcfg: building.BuildingConfig, binfo: building.BuildingInformation) -> List[List[str]]:
    return [
        ["Quantity", "Value"],
        ["Heat capacity class (TABULA / ISO 13790)", bcfg.building_heat_capacity_class],
        [
            "Thermal mass capacitance [kWh/K]",
            _fmt(binfo.thermal_capacity_of_building_thermal_mass_in_joule_per_kelvin / 3.6e6, digits=4),
        ],
        [
            "Thermal mass per floor area [Wh/(m²·K)]",
            _fmt(binfo.thermal_capacity_of_building_thermal_mass_in_watthour_per_m2_per_kelvin, digits=4),
        ],
        ["Conditioned floor area [m²]", _fmt(bcfg.absolute_conditioned_floor_area_in_m2)],
        ["TABULA building code", bcfg.building_code],
        ["TABULA period label", bcfg.building_name],
    ]


def internal_loads_rows(occ: sia2024_occupancy.SIA2024OccupancyConfig) -> List[List[str]]:
    people_w_per_m2 = float(occ.people_per_m2) * float(occ.sensible_heat_gain_per_person_in_watt)
    return [
        ["Load", "Intensity", "Schedule", "Seasonal variation"],
        [
            "People (sensible internal gains)",
            f"{_fmt(people_w_per_m2)} W/m² "
            f"({occ.people_per_m2} pers/m² × {occ.sensible_heat_gain_per_person_in_watt} W/pers)",
            _schedule_people_description(occ),
            _seasonal_people_description(occ),
        ],
        [
            "Appliances (electricity → internal gains)",
            f"{_fmt(occ.appliances_load_w_per_m2)} W/m²",
            _schedule_appliances_description(occ),
            _seasonal_appliances_description(occ),
        ],
        [
            "Lighting (electricity → internal gains)",
            f"{_fmt(occ.lighting_load_w_per_m2)} W/m²",
            _schedule_lighting_description(occ),
            _seasonal_lighting_description(occ),
        ],
    ]


def ventilation_rows(bcfg: building.BuildingConfig) -> List[List[str]]:
    return [
        ["Mechanism", "Parameter", "Value / schedule"],
        [
            "Infiltration (Swiss, replaces TABULA n_air_infiltration)",
            "Air change rate",
            f"{_fmt(bcfg.swiss_infiltration_rate_per_h)} 1/h",
        ],
        [
            "Hygienic ventilation (SIA floor area)",
            "Flow per conditioned area",
            f"{_fmt(bcfg.sia_natural_ventilation_m3_per_h_per_m2)} m³/(h·m²); "
            f"enabled: {_fmt(bcfg.enable_sia_floor_area_natural_ventilation)}",
        ],
        [
            "Occupancy-driven ventilation",
            "Flow per person (when present)",
            f"{_fmt(bcfg.natural_ventilation_m3_per_h_per_person)} m³/(h·person); "
            f"enabled: {_fmt(bcfg.enable_occupancy_driven_natural_ventilation)}; "
            "schedule follows SIA 2024 people profile",
        ],
        [
            "Summer window ventilation",
            "Target ACH when active",
            f"{_fmt(bcfg.summer_window_ventilation_target_ach_per_h)} 1/h; "
            f"enabled: {_fmt(bcfg.enable_summer_window_ventilation_ach)}",
        ],
        [
            "Summer window ventilation",
            "Running-mean outdoor enable threshold [°C]",
            _fmt(bcfg.summer_window_ventilation_enable_running_mean_outdoor_temperature_threshold_in_celsius),
        ],
        [
            "Summer window ventilation",
            "Open when outdoor cooler than operative",
            _fmt(bcfg.summer_window_ventilation_open_when_outdoor_cooler_than_operative),
        ],
    ]


def _plot_weekly_schedule(values: Sequence[float], title: str, ylabel: str, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.size": FONT_SIZE_BODY,
            "axes.titlesize": FONT_SIZE_BODY,
            "axes.labelsize": FONT_SIZE_BODY,
        }
    )
    hours = list(range(len(values)))
    fig, ax = plt.subplots(figsize=(7.5, 2.2))
    ax.plot(hours, values, color="#2563eb", linewidth=0.8)
    ax.set_title(title, fontsize=9)
    ax.set_xlabel("Hour index (168 h week, Mon 00:00 = 0)")
    ax.set_ylabel(ylabel)
    ax.set_xlim(0, max(len(values) - 1, 1))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _schedule_figure_paths(occ: sia2024_occupancy.SIA2024OccupancyConfig, tmp: Path) -> List[Tuple[str, Path]]:
    tmp.mkdir(parents=True, exist_ok=True)
    figures: List[Tuple[str, Path]] = []
    people_path = tmp / "people.png"
    _plot_weekly_schedule(occ.people_schedule_weekly_hourly, "People schedule (fraction)", "fraction", people_path)
    figures.append(("People schedule (weekly)", people_path))

    appl_path = tmp / "appliances.png"
    _plot_weekly_schedule(
        occ.appliances_schedule_weekly_hourly, "Appliances schedule (fraction)", "fraction", appl_path
    )
    figures.append(("Appliances schedule (weekly)", appl_path))

    if occ.lighting_schedule_weekly_hourly is not None:
        light_path = tmp / "lighting.png"
        _plot_weekly_schedule(
            occ.lighting_schedule_weekly_hourly, "Lighting schedule (fraction)", "fraction", light_path
        )
        figures.append(("Lighting schedule (weekly)", light_path))
    return figures


def build_archetype_pdf_story(
    archetype: str,
    ctx: DocumentationContext,
    typo: PdfTypography,
    tmp_dir: Path,
    *,
    section_number: int,
    content_width: float,
) -> List[Any]:
    """Return reportlab flowables for one archetype chapter."""
    bcfg, binfo, occ = resolve_archetype_models(archetype, ctx)
    story: List[Any] = []
    sub = 0

    def subsection(title: str) -> str:
        nonlocal sub
        sub += 1
        return f"{section_number}.{sub} {title}"

    arch_title = f"Archetype {archetype}"
    if bcfg.building_name:
        arch_title += f" ({bcfg.building_name})"
    story.append(Paragraph(f"{section_number} {_escape_pdf_text(arch_title)}", typo.section))
    story.append(
        Paragraph(
            _escape_pdf_text(
                f"Swiss CH building assumptions; reference weather {ctx.weather}, "
                f"TIME_HORIZON={ctx.time_horizon}, SCENARIO={ctx.scenario}, "
                f"occupancy {ctx.occupancy_mode} ({ctx.sia_use_type})."
            ),
            typo.body,
        )
    )

    story.append(Paragraph(_escape_pdf_text(subsection("Geometry inputs")), typo.subsection))
    story.append(
        _latex_table(
            geometry_table_rows(bcfg, binfo),
            _widths_from_ratios(content_width, (1.1, 0.9, 1.35, 0.85)),
            typo,
        )
    )
    story.append(Spacer(1, 0.2 * cm))

    story.append(Paragraph(_escape_pdf_text(subsection("Thermal mass")), typo.subsection))
    story.append(
        _latex_table(
            thermal_mass_rows(bcfg, binfo),
            _widths_from_ratios(content_width, (1.15, 0.85)),
            typo,
        )
    )
    story.append(Spacer(1, 0.25 * cm))

    story.append(Paragraph(_escape_pdf_text(subsection("Internal loads")), typo.subsection))
    story.append(
        _latex_table(
            internal_loads_rows(occ),
            _widths_from_ratios(content_width, (0.95, 1.0, 1.05, 1.2)),
            typo,
        )
    )
    story.append(Spacer(1, 0.15 * cm))

    story.append(Paragraph(_escape_pdf_text(subsection("Weekly schedule profiles")), typo.subsection))
    for caption, img_path in _schedule_figure_paths(occ, tmp_dir / archetype):
        story.append(Paragraph(_escape_pdf_text(caption), typo.caption))
        img_width = min(content_width, 15 * cm)
        story.append(Image(str(img_path), width=img_width, height=img_width * 0.28))
        story.append(Spacer(1, 0.12 * cm))
    story.append(Spacer(1, 0.15 * cm))

    story.append(Paragraph(_escape_pdf_text(subsection("Ventilation")), typo.subsection))
    story.append(
        _latex_table(
            ventilation_rows(bcfg),
            _widths_from_ratios(content_width, (1.0, 0.85, 1.35)),
            typo,
        )
    )
    story.append(PageBreak())
    return story


def write_archetype_documentation_pdf(
    output_path: Path,
    archetypes: Sequence[str],
    ctx: DocumentationContext,
) -> None:
    """Write one PDF containing all requested archetypes."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    typo = build_pdf_typography()
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=2.0 * cm,
        rightMargin=2.0 * cm,
        topMargin=2.0 * cm,
        bottomMargin=2.0 * cm,
    )
    story: List[Any] = []

    with tempfile.TemporaryDirectory(prefix="hisim_arch_doc_") as tmp:
        tmp_dir = Path(tmp)
        for index, arch in enumerate(archetypes):
            story.extend(
                build_archetype_pdf_story(
                    arch,
                    ctx,
                    typo,
                    tmp_dir,
                    section_number=index + 1,
                    content_width=doc.width,
                )
            )
            if index == len(archetypes) - 1 and story and isinstance(story[-1], PageBreak):
                story.pop()
        doc.build(story)


def load_context_from_dict(data: Dict[str, Any]) -> DocumentationContext:
    ctx = DocumentationContext()
    if "weather" in data:
        ctx.weather = str(data["weather"])
    if "time_horizon" in data:
        ctx.time_horizon = str(data["time_horizon"])
    if "scenario" in data:
        ctx.scenario = str(data["scenario"])
    if "occupancy_mode" in data:
        ctx.occupancy_mode = str(data["occupancy_mode"])
    if "sia_use_type" in data:
        ctx.sia_use_type = str(data["sia_use_type"])
    if "ch_batch_building_overrides" in data:
        ctx.ch_batch_building_overrides = bool(data["ch_batch_building_overrides"])
    overrides = data.get("cli_overrides") or {}
    if isinstance(overrides, dict):
        ctx.weather = str(overrides.get("WEATHER", ctx.weather))
        ctx.time_horizon = str(overrides.get("TIME_HORIZON", ctx.time_horizon))
        ctx.scenario = str(overrides.get("SCENARIO", ctx.scenario) or "none")
        ctx.occupancy_mode = str(overrides.get("OCC", ctx.occupancy_mode))
        ctx.sia_use_type = str(overrides.get("SIA_USE", ctx.sia_use_type))
    return ctx


def load_archetypes_from_config(data: Dict[str, Any]) -> List[str]:
    archs = data.get("archetypes")
    if not archs:
        return list(DEFAULT_ARCHETYPES)
    return [str(a) for a in archs]


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)
