"""Compare Swiss CH custom weather CSV profiles for batch documentation.

Compares present-day DRY (2023), future DRY (2060 RCP85), and future HEAT (2060 RCP85)
for all four Swiss batch locations (ZUESTA, BASSTA, KLO, RUE).

Writes a wide summary CSV with absolute metrics and deltas vs present and heat vs dry.

Usage (from repo root):
    python scripts/compare_ch_weather_profiles.py
    python scripts/compare_ch_weather_profiles.py -o reports/my_comparison.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
WEATHER_ROOT = REPO_ROOT / "hisim" / "inputs" / "weather" / "custom_csv"

LOCATIONS: dict[str, dict[str, Path]] = {
    "ZUESTA": {
        "present": WEATHER_ROOT / "Zurich" / "ZUESTA_2023_DRY.csv",
        "future_dry": WEATHER_ROOT / "Zurich" / "NABZUE_2060_RCP85_DRY.csv",
        "future_heat": WEATHER_ROOT / "Zurich" / "NABZUE_2060_RCP85_HEAT.csv",
    },
    "BASSTA": {
        "present": WEATHER_ROOT / "Basel" / "BASSTA_2023_DRY.csv",
        "future_dry": WEATHER_ROOT / "Basel" / "BKLI_2060_RCP85_DRY.csv",
        "future_heat": WEATHER_ROOT / "Basel" / "BKLI_2060_RCP85_HEAT.csv",
    },
    "KLO": {
        "present": WEATHER_ROOT / "Kloten" / "KLO_2023_DRY.csv",
        "future_dry": WEATHER_ROOT / "Kloten" / "KLO_2060_RCP85_DRY.csv",
        "future_heat": WEATHER_ROOT / "Kloten" / "KLO_2060_RCP85_HEAT.csv",
    },
    "RUE": {
        "present": WEATHER_ROOT / "Ruenenberg" / "RUE_2023_DRY.csv",
        "future_dry": WEATHER_ROOT / "Ruenenberg" / "RUE_2060_RCP85_DRY.csv",
        "future_heat": WEATHER_ROOT / "Ruenenberg" / "RUE_2060_RCP85_HEAT.csv",
    },
}

COLUMN_MAPS: dict[str, dict[str, str]] = {
    "present": {
        "temp": "temp",
        "wind": "windmean",
        "ghi": "rad.global",
        "dni": "rad.direct",
        "dhi": "rad.diffus",
    },
    "future": {
        "temp": "tre200h0",
        "wind": "fkl010h0",
        "ghi": "gls",
        "dni": "str.direkt",
        "dhi": "str.diffus",
    },
}

PROFILE_LABELS = {
    "present": "present_2023_DRY",
    "future_dry": "future_2060_RCP85_DRY",
    "future_heat": "future_2060_RCP85_HEAT",
}

METRICS = [
    "n_hours",
    "temp_mean_c",
    "temp_summer_mean_c",
    "temp_summer_night_mean_c",
    "ghi_kwh_per_m2",
    "dni_kwh_per_m2",
    "dhi_kwh_per_m2",
    "summer_midday_ghi_mean_w_per_m2",
    "hdd18_c_hours",
    "cdd24_c_hours",
    "hours_above_26c",
    "hours_below_0c",
    "wind_mean_m_per_s",
]


def _load_weather_csv(path: Path, schema: str) -> pd.DataFrame:
    """Load a Swiss custom weather CSV into a normalized hourly dataframe."""
    raw = pd.read_csv(path, encoding="utf-8")
    column_map = COLUMN_MAPS[schema]
    out = pd.DataFrame({key: pd.to_numeric(raw[col], errors="coerce") for key, col in column_map.items()})
    out["month"] = pd.to_numeric(raw["time.mm"], errors="coerce").astype("Int64")
    out["hour"] = pd.to_numeric(raw["time.hh"], errors="coerce").astype("Int64")
    return out.dropna(subset=["temp", "ghi", "dni", "dhi"]).reset_index(drop=True)


def _summarize(df: pd.DataFrame) -> dict[str, float]:
    """Compute annual / seasonal indicators used for SQ25 vs SQ50 interpretation."""
    summer = df[df["month"].isin([6, 7, 8])]
    summer_day = summer[summer["hour"].between(10, 16)]
    summer_night = summer[summer["hour"].isin([0, 1, 2, 3, 4, 5])]

    return {
        "n_hours": float(len(df)),
        "temp_mean_c": float(df["temp"].mean()),
        "temp_summer_mean_c": float(summer["temp"].mean()),
        "temp_summer_night_mean_c": float(summer_night["temp"].mean()),
        "ghi_kwh_per_m2": float(df["ghi"].sum() / 1000.0),
        "dni_kwh_per_m2": float(df["dni"].sum() / 1000.0),
        "dhi_kwh_per_m2": float(df["dhi"].sum() / 1000.0),
        "summer_midday_ghi_mean_w_per_m2": float(summer_day["ghi"].mean()),
        "hdd18_c_hours": float((18.0 - df["temp"]).clip(lower=0).sum()),
        "cdd24_c_hours": float((df["temp"] - 24.0).clip(lower=0).sum()),
        "hours_above_26c": float((df["temp"] > 26).sum()),
        "hours_below_0c": float((df["temp"] < 0).sum()),
        "wind_mean_m_per_s": float(df["wind"].mean()),
    }


def _compare_profiles(location: str, paths: dict[str, Path]) -> list[dict[str, object]]:
    """Build one wide comparison row per location."""
    summaries: dict[str, dict[str, float]] = {}
    for profile_key, csv_path in paths.items():
        schema = "present" if profile_key == "present" else "future"
        summaries[profile_key] = _summarize(_load_weather_csv(csv_path, schema))

    row: dict[str, object] = {"location": location}
    for profile_key, label in PROFILE_LABELS.items():
        for metric in METRICS:
            row[f"{label}__{metric}"] = summaries[profile_key][metric]

    present = summaries["present"]
    future_dry = summaries["future_dry"]
    future_heat = summaries["future_heat"]

    for metric in METRICS:
        row[f"delta_future_dry_vs_present__{metric}"] = future_dry[metric] - present[metric]
        row[f"delta_future_heat_vs_present__{metric}"] = future_heat[metric] - present[metric]
        row[f"delta_future_heat_vs_future_dry__{metric}"] = future_heat[metric] - future_dry[metric]
        if present[metric] != 0:
            row[f"pct_future_dry_vs_present__{metric}"] = 100.0 * (future_dry[metric] - present[metric]) / present[metric]
            row[f"pct_future_heat_vs_present__{metric}"] = 100.0 * (future_heat[metric] - present[metric]) / present[metric]
        else:
            row[f"pct_future_dry_vs_present__{metric}"] = float("nan")
            row[f"pct_future_heat_vs_present__{metric}"] = float("nan")

    return [row]


def build_comparison_dataframe() -> pd.DataFrame:
    """Return wide comparison table for all Swiss batch locations."""
    rows = [_compare_profiles(location, paths) for location, paths in LOCATIONS.items()]
    flat_rows = [item for sublist in rows for item in sublist]
    return pd.DataFrame(flat_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Swiss CH weather profiles for batch documentation.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=REPO_ROOT / "reports" / "ch_weather_comparison.csv",
        help="Output CSV path (default: reports/ch_weather_comparison.csv)",
    )
    args = parser.parse_args()

    df = build_comparison_dataframe()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False, encoding="utf-8")

    print(f"Wrote {len(df)} location comparison(s) to {args.output}")
    for location in LOCATIONS:
        present_ghi = df.loc[df["location"] == location, "present_2023_DRY__ghi_kwh_per_m2"].iloc[0]
        dry_ghi = df.loc[df["location"] == location, "future_2060_RCP85_DRY__ghi_kwh_per_m2"].iloc[0]
        heat_ghi = df.loc[df["location"] == location, "future_2060_RCP85_HEAT__ghi_kwh_per_m2"].iloc[0]
        print(
            f"  {location}: GHI present={present_ghi:.1f}, future_dry={dry_ghi:.1f}, future_heat={heat_ghi:.1f} kWh/m2"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
