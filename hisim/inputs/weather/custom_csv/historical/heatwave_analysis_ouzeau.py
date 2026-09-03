"""Cold-spell analysis using the Ouzeau heatwave methodology on the cold tail.

The detection rules mirror ``detect_heatwaves`` but use the coldest percentiles
(5 / 2.5 / 0.5 instead of 95 / 97.5 / 99.5) and inverted temperature comparisons.

For each Swiss location, the following weather files are compared:
  - Present:      *_2023_DRY.csv
  - Future heat:  *_2060_RCP85_HEAT.csv  (1-in-10-year heat scenario)
  - Historical:   *_{2009,2010,2012}_CS.csv

Each dataset is analysed with its own thresholds. Future and historical datasets
are additionally analysed using present-day (2023 DRY) thresholds for comparison.

Usage (from repo root):
    python hisim/inputs/weather/custom_csv/historical/heatwave_analysis_ouzeau.py
    python hisim/inputs/weather/custom_csv/historical/heatwave_analysis_ouzeau.py --location ZUESTA
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
CUSTOM_CSV_ROOT = SCRIPT_DIR.parent

HISTORICAL_YEARS = (2009, 2010, 2012)

TEMP_COLUMN_ALIASES = ("temp", "tre200h0")

LOCATION_CONFIG: dict[str, dict[str, str | list[str]]] = {
    "ZUESTA": {
        "label": "ZUESTA",
        "subdir": "Zurich",
        "present": "ZUESTA_2023_DRY.csv",
        "future_heat": "NABZUE_2060_RCP85_HEAT.csv",
    },
    "BASSTA": {
        "label": "BASSTA",
        "subdir": "Basel",
        "present": "BASSTA_2023_DRY.csv",
        "future_heat": "BKLI_2060_RCP85_HEAT.csv",
    },
    "KLO": {
        "label": "KLO",
        "subdir": "Kloten",
        "present": "KLO_2023_DRY.csv",
        "future_heat": "KLO_2060_RCP85_HEAT.csv",
    },
    "RUE": {
        "label": "RUE",
        "subdir": "Ruenenberg",
        "present": "RUE_2023_DRY.csv",
        "future_heat": "RUE_2060_RCP85_HEAT.csv",
    },
}


def _resolve_temp_column(columns: pd.Index) -> str:
    for name in TEMP_COLUMN_ALIASES:
        if name in columns:
            return name
    raise ValueError(f"No temperature column found (expected one of {TEMP_COLUMN_ALIASES})")


def load_hourly_temperature(csv_path: Path) -> pd.Series:
    df = pd.read_csv(csv_path)
    temp_col = _resolve_temp_column(df.columns)
    values = pd.to_numeric(df[temp_col], errors="coerce")
    if values.isna().any():
        raise ValueError(f"{csv_path.name} contains non-numeric temperature values")
    n_hours = len(values)
    if n_hours % 24 != 0:
        raise ValueError(f"{csv_path.name}: expected hourly data (length multiple of 24), got {n_hours}")
    return values.reset_index(drop=True)


def hourly_to_daily(series: pd.Series) -> np.ndarray:
    values = series.to_numpy(dtype=float)
    return values.reshape(len(values) // 24, 24).mean(axis=1)


def calculate_cold_thresholds(daily: np.ndarray) -> dict[str, float]:
    return {
        "Sint": float(np.percentile(daily, 5)),
        "Sdeb": float(np.percentile(daily, 2.5)),
        "Spic": float(np.percentile(daily, 0.5)),
    }


def detect_coldspells(daily: np.ndarray, thr: dict[str, float]) -> pd.DataFrame:
    """Detect cold spells using the mirrored Ouzeau event rules."""
    sint, sdeb, spic = thr["Sint"], thr["Sdeb"], thr["Spic"]
    intensity_denominator = sdeb - spic
    if intensity_denominator <= 0:
        raise ValueError("Invalid cold thresholds: Sdeb must be greater than Spic.")

    events: list[dict[str, float | int]] = []
    n = len(daily)
    i = 0

    while i < n:
        if daily[i] > spic:
            i += 1
            continue

        start = i
        while start > 0 and daily[start - 1] <= sdeb:
            start -= 1

        end = i
        above_sdeb = 0
        j = i + 1

        while j < n:
            temp = daily[j]

            if temp > sint:
                break

            if temp > sdeb:
                above_sdeb += 1
                if above_sdeb >= 3:
                    end = j - 3
                    break
            else:
                above_sdeb = 0
                end = j

            j += 1
        else:
            if above_sdeb >= 3:
                end = n - 4
            else:
                end = n - 1

        if end < start:
            end = i

        temps = daily[start : end + 1]
        intensity = ((sdeb - temps).clip(min=0).sum()) / intensity_denominator
        events.append(
            {
                "Start day": start + 1,
                "End day": end + 1,
                "Duration (days)": end - start + 1,
                "Trough temperature": float(temps.min()),
                "Global intensity": float(intensity),
                "Sint": sint,
                "Sdeb": sdeb,
                "Spic": spic,
            }
        )
        i = max(j, end + 1)

    return pd.DataFrame(events)


def hourly_intensity_profile(daily: np.ndarray, thr: dict[str, float]) -> np.ndarray:
    """Hourly cold-spell intensity profile (8760+ length depending on input year)."""
    sint, sdeb, spic = thr["Sint"], thr["Sdeb"], thr["Spic"]
    intensity_denominator = sdeb - spic
    n_hours = len(daily) * 24
    hourly = np.zeros(n_hours)

    n = len(daily)
    i = 0

    while i < n:
        if daily[i] > spic:
            i += 1
            continue

        start = i
        while start > 0 and daily[start - 1] <= sdeb:
            start -= 1

        end = i
        above_sdeb = 0
        j = i + 1

        while j < n:
            temp = daily[j]

            if temp > sint:
                break

            if temp > sdeb:
                above_sdeb += 1
                if above_sdeb >= 3:
                    end = j - 3
                    break
            else:
                above_sdeb = 0
                end = j

            j += 1
        else:
            if above_sdeb >= 3:
                end = n - 4
            else:
                end = n - 1

        if end < start:
            end = i

        for day in range(start, end + 1):
            contribution = max(sdeb - daily[day], 0.0) / intensity_denominator
            first_hour = day * 24
            hourly[first_hour : first_hour + 24] = contribution

        i = max(j, end + 1)

    return hourly


def _scenario_paths(location_key: str) -> dict[str, Path]:
    cfg = LOCATION_CONFIG[location_key]
    subdir = CUSTOM_CSV_ROOT / str(cfg["subdir"])
    scenarios = {
        "Present_2023_DRY": subdir / str(cfg["present"]),
        "Future_2060_RCP85_HEAT": subdir / str(cfg["future_heat"]),
    }
    for year in HISTORICAL_YEARS:
        scenarios[f"Historical_{year}"] = subdir / f"{cfg['label']}_{year}_CS.csv"
    return scenarios


def analyse_location(
    location_key: str,
    *,
    output_dir: Path | None = None,
) -> dict[str, pd.DataFrame]:
    cfg = LOCATION_CONFIG[location_key]
    location_label = str(cfg["label"])
    scenario_paths = _scenario_paths(location_key)

    missing = [name for name, path in scenario_paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing weather files for {location_label}: "
            + ", ".join(f"{name} ({scenario_paths[name].name})" for name in missing)
        )

    daily_by_scenario = {
        name: hourly_to_daily(load_hourly_temperature(path))
        for name, path in scenario_paths.items()
    }
    thresholds_by_scenario = {
        name: calculate_cold_thresholds(daily) for name, daily in daily_by_scenario.items()
    }
    present_thresholds = thresholds_by_scenario["Present_2023_DRY"]

    analysis_plan: list[tuple[str, str, dict[str, float]]] = []
    for scenario_name, daily in daily_by_scenario.items():
        analysis_plan.append((scenario_name, scenario_name, thresholds_by_scenario[scenario_name]))
        if scenario_name != "Present_2023_DRY":
            analysis_plan.append(
                (
                    f"{scenario_name}_using_PresentThresholds",
                    "Present_2023_DRY",
                    present_thresholds,
                )
            )

    results: dict[str, pd.DataFrame] = {}
    hourly_profiles: dict[str, np.ndarray] = {}

    for sheet_name, threshold_source, thresholds in analysis_plan:
        scenario_data_name = sheet_name.replace("_using_PresentThresholds", "")
        daily = daily_by_scenario[scenario_data_name]
        events = detect_coldspells(daily, thresholds)
        events.insert(0, "Location", location_label)
        events.insert(1, "Scenario", scenario_data_name)
        events.insert(2, "Threshold source", threshold_source)
        results[sheet_name] = events
        hourly_profiles[sheet_name] = hourly_intensity_profile(daily, thresholds)

    max_hours = max(len(profile) for profile in hourly_profiles.values())
    hourly_df = pd.DataFrame({"Hour": np.arange(1, max_hours + 1)})
    for sheet_name, profile in hourly_profiles.items():
        hourly_df[sheet_name] = pd.Series(profile).reindex(range(max_hours))

    out_dir = output_dir or SCRIPT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    excel_out = out_dir / f"{location_label}_coldspell_analysis.xlsx"

    with pd.ExcelWriter(excel_out, engine="openpyxl") as writer:
        for sheet_name, frame in results.items():
            frame.to_excel(writer, sheet_name=sheet_name[:31], index=False)
        hourly_df.to_excel(writer, sheet_name="HourlyIntensity", index=False)

    print(f"Wrote {excel_out}")
    return results


def print_summary(location_key: str, results: dict[str, pd.DataFrame]) -> None:
    print(f"\n{location_key} cold-spell summary (own thresholds):")
    for scenario in ("Present_2023_DRY", "Future_2060_RCP85_HEAT", *(f"Historical_{y}" for y in HISTORICAL_YEARS)):
        frame = results.get(scenario)
        if frame is None or frame.empty:
            print(f"  {scenario}: no cold spells detected")
            continue
        total_intensity = frame["Global intensity"].sum()
        max_duration = frame["Duration (days)"].max()
        coldest = frame["Trough temperature"].min()
        print(
            f"  {scenario}: {len(frame)} event(s), "
            f"sum intensity={total_intensity:.2f}, "
            f"max duration={int(max_duration)} d, "
            f"coldest={coldest:.1f} C"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cold-spell analysis (Ouzeau method, cold tail) for Swiss custom weather CSVs."
    )
    parser.add_argument(
        "--location",
        choices=sorted(LOCATION_CONFIG),
        nargs="*",
        default=None,
        help="Location(s) to analyse (default: all four Swiss locations).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_DIR,
        help="Directory for Excel output files.",
    )
    args = parser.parse_args()

    locations = args.location or sorted(LOCATION_CONFIG)
    for location_key in locations:
        results = analyse_location(location_key, output_dir=args.output_dir)
        print_summary(location_key, results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
