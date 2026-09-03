"""Generate simulation documentation for Swiss CH archetypes and energy systems.

Reads a JSON config listing archetypes, energy systems, weather locations, and optional
CLI overrides (same keys as ``hisim_main.py``, e.g. TIME_HORIZON, HEATGEN_SIZING).

For each combination, runs the setup's ``setup_function`` without time-step simulation
and records resolved envelope, internal loads, plant sizing, and related parameters.

Usage (from repo root):

    python scripts/generate_ch_simulation_documentation.py
    python scripts/generate_ch_simulation_documentation.py path/to/my_config.json
    python scripts/generate_ch_simulation_documentation.py -o reports/my_doc.xlsx --arch 01_CH --setup hp01 bo01

See ``scripts/ch_simulation_documentation.example.json`` for a full example.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hisim.ch_simulation_documentation import (  # noqa: E402
    DEFAULT_ARCHETYPES,
    DEFAULT_ENERGY_SYSTEMS,
    collect_all_cases,
    write_excel_document,
    write_markdown_summary,
)

DEFAULT_CONFIG_PATH = Path(__file__).with_name("ch_simulation_documentation.example.json")


def _load_json_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError("Config file must be a JSON object.")
    return data


def _resolve_lists(config: Dict[str, Any], args: argparse.Namespace) -> tuple[List[str], List[str], List[str]]:
    archetypes = args.arch or config.get("archetypes") or list(DEFAULT_ARCHETYPES)
    energy_systems = args.setup or config.get("energy_systems") or list(DEFAULT_ENERGY_SYSTEMS)
    weather = args.weather or config.get("weather") or ["ZUESTA"]
    return [str(x) for x in archetypes], [str(x) for x in energy_systems], [str(x) for x in weather]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Swiss CH simulation documentation (Excel + Markdown).")
    parser.add_argument(
        "config",
        nargs="?",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"JSON config path (default: {DEFAULT_CONFIG_PATH.name})",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output Excel path (default: from JSON or reports/ch_simulation_documentation.xlsx)",
    )
    parser.add_argument("--arch", nargs="+", help="Override archetype list (e.g. 01_CH 02_CH)")
    parser.add_argument("--setup", nargs="+", help="Override energy system list (e.g. hp01 bo01)")
    parser.add_argument("--weather", nargs="+", help="Override weather location list (e.g. ZUESTA BASSTA)")
    parser.add_argument(
        "--no-markdown",
        action="store_true",
        help="Skip companion .md summary next to the Excel file",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_file():
        raise SystemExit(f"Config not found: {config_path}")

    config = _load_json_config(config_path)
    archetypes, energy_systems, weather_locations = _resolve_lists(config, args)
    cli_overrides_config = config.get("cli_overrides") or {}
    if not isinstance(cli_overrides_config, dict):
        raise SystemExit("'cli_overrides' in config must be an object.")

    output_excel = Path(
        args.output or config.get("output") or REPO_ROOT / "reports" / "ch_simulation_documentation.xlsx"
    )
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(
        f"Collecting documentation for {len(energy_systems)} setups × "
        f"{len(archetypes)} archetypes × {len(weather_locations)} weather = "
        f"{len(energy_systems) * len(archetypes) * len(weather_locations)} cases …"
    )
    parameter_rows, error_rows = collect_all_cases(
        archetypes=archetypes,
        energy_systems=energy_systems,
        weather_locations=weather_locations,
        cli_overrides_config={str(k): str(v) for k, v in cli_overrides_config.items()},
    )

    metadata: Dict[str, Any] = {
        "generated_at": generated_at,
        "config_file": str(config_path.resolve()),
        "archetypes": ", ".join(archetypes),
        "energy_systems": ", ".join(energy_systems),
        "weather_locations": ", ".join(weather_locations),
        "cli_overrides": json.dumps(cli_overrides_config, ensure_ascii=False),
        "successful_cases": len({r["case_id"] for r in parameter_rows}),
        "failed_cases": len(error_rows),
    }

    write_excel_document(output_excel, parameter_rows, error_rows, metadata)
    print(f"Wrote Excel: {output_excel}")

    if not args.no_markdown:
        md_path = output_excel.with_suffix(".md")
        write_markdown_summary(md_path, parameter_rows, metadata)
        print(f"Wrote Markdown summary: {md_path}")

    if error_rows:
        print(f"WARNING: {len(error_rows)} case(s) failed — see 'errors' sheet in Excel.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
