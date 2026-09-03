"""Generate PDF simulation documentation for Swiss CH building archetypes.

Usage (from repo root):

    python scripts/generate_ch_archetype_pdfs.py
    python scripts/generate_ch_archetype_pdfs.py scripts/ch_archetype_documentation.example.json
    python scripts/generate_ch_archetype_pdfs.py --arch 01_CH 05_CH -o reports/my_archetypes.pdf
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hisim.ch_archetype_documentation import (  # noqa: E402
    DEFAULT_ARCHETYPES,
    DocumentationContext,
    load_archetypes_from_config,
    load_config,
    load_context_from_dict,
    write_archetype_documentation_pdf,
)

DEFAULT_CONFIG = Path(__file__).with_name("ch_archetype_documentation.example.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Swiss CH archetype documentation PDF.")
    parser.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG), help="JSON config path")
    parser.add_argument("-o", "--output", help="Output PDF path")
    parser.add_argument("--arch", nargs="+", help="Archetype list override (e.g. 01_CH)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_file():
        raise SystemExit(f"Config not found: {config_path}")

    data = load_config(config_path)
    archetypes = [str(a) for a in args.arch] if args.arch else load_archetypes_from_config(data)
    ctx = load_context_from_dict(data)
    output = Path(args.output or data.get("output") or REPO_ROOT / "reports" / "ch_archetype_documentation.pdf")

    print(f"Writing PDF for {len(archetypes)} archetype(s) -> {output}")
    write_archetype_documentation_pdf(output, archetypes, ctx)
    print("Done.")


if __name__ == "__main__":
    main()
