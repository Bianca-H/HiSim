"""Convert MeteoSwiss SMN historical hourly CSVs to HiSim custom weather CSV format.

Reads semicolon-separated OGD SMN historical exports and writes one comma-separated file
per calendar year, using the same column layout as the location reference custom CSVs
(e.g. Zurich/ZUESTA_2023_DRY.csv).

HiSim maps custom CSV radiation columns as follows (weather.py: read_custom_weather_csv):
  - rad.global -> GHI  (global horizontal irradiance)
  - rad.direct -> DNI  (direct NORMAL irradiance; can exceed GHI)
  - rad.diffus -> DHI  (diffuse horizontal irradiance)

Because rad.direct is DNI, it is not a simple split of GHI. The relationship is:
  GHI = DNI * cos(zenith) + DHI

When only gre000h0 (GHI) is available, DNI and DHI are estimated with the Erbs decomposition
model (pvlib), using the station coordinates and hourly timestamps. When ods000h0 (diffuse) is
present it is used as DHI and DNI is back-calculated from GHI and solar zenith.

Usage (from repo root):
    python hisim/inputs/weather/custom_csv/historical/convert_smn_historical_to_custom_csv.py
    python hisim/inputs/weather/custom_csv/historical/convert_smn_historical_to_custom_csv.py --years 2009 2010 2012
    python hisim/inputs/weather/custom_csv/historical/convert_smn_historical_to_custom_csv.py --validate-only
    python hisim/inputs/weather/custom_csv/historical/convert_smn_historical_to_custom_csv.py --skip-validate

Validation runs for all configured locations (SMA, BAS, KLO, RUE) before conversion by default.
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from pvlib import irradiance, solarposition

SCRIPT_DIR = Path(__file__).resolve().parent
CUSTOM_CSV_ROOT = SCRIPT_DIR.parent

DEFAULT_YEARS = (2009, 2010, 2012)
TIMEZONE = "Europe/Zurich"
ZENITH_CAP_DEG = 87.0

CUSTOM_CSV_COLUMNS = [
    "station",
    "time.yy",
    "time.mm",
    "time.dd",
    "time.hh",
    "temp",
    "relhum",
    "vappres",
    "dewpt",
    "mixratio",
    "wetbulb",
    "enthalpy",
    "precip",
    "airpres",
    "winddir",
    "windmean",
    "windmax",
    "rad.global",
    "rad.direct",
    "rad.diffus",
    "rad.vert.N",
    "rad.vert.E",
    "rad.vert.S",
    "rad.vert.W",
    "ir.horiz",
    "cloudcov",
    "albedo",
    "emissivity",
]

SIMULATION_COLUMNS = (
    "station",
    "time.yy",
    "time.mm",
    "time.dd",
    "time.hh",
    "temp",
    "airpres",
    "windmean",
    "rad.global",
    "rad.direct",
    "rad.diffus",
)

STATION_CONFIG: dict[str, dict[str, Any]] = {
    "SMA": {
        "hisim_station": "ZUESTA",
        "output_subdir": "Zurich",
        "reference_csv": "Zurich/ZUESTA_2023_DRY.csv",
        "input_glob": "ogd-smn_sma_h_historical_*.csv",
        "latitude": 47.37759,
        "longitude": 8.530352,
    },
    "BAS": {
        "hisim_station": "BASSTA",
        "output_subdir": "Basel",
        "reference_csv": "Basel/BASSTA_2023_DRY.csv",
        "input_glob": "ogd-smn_bas_h_historical_*.csv",
        "latitude": 47.558262,
        "longitude": 7.583405,
    },
    "KLO": {
        "hisim_station": "KLO",
        "output_subdir": "Kloten",
        "reference_csv": "Kloten/KLO_2023_DRY.csv",
        "input_glob": "ogd-smn_klo_h_historical_*.csv",
        "latitude": 47.479611,
        "longitude": 8.535961,
    },
    "RUE": {
        "hisim_station": "RUE",
        "output_subdir": "Ruenenberg",
        "reference_csv": "Ruenenberg/RUE_2023_DRY.csv",
        "input_glob": "ogd-smn_rue_h_historical_*.csv",
        "latitude": 47.434572,
        "longitude": 7.879414,
    },
}

SMN_SOURCE_MAP: dict[str, str] = {
    "tre200h0": "temp",
    "prestah0": "airpres",
    "fkl010h0": "windmean",
    "gre000h0": "rad.global",
}


@dataclass
class ConversionSummary:
    """Per-file conversion statistics."""

    year: int
    total_rows: int = 0
    missing_by_column: dict[str, int] = field(default_factory=dict)


def _to_numeric(series: pd.Series) -> pd.Series:
    cleaned = series.replace("", pd.NA).astype("string").str.strip()
    return pd.to_numeric(cleaned, errors="coerce")


def _reference_airpres_hpa(reference_csv: Path) -> float:
    ref = pd.read_csv(reference_csv, usecols=["airpres"])
    return float(ref["airpres"].astype(float).median())


def _format_value(value: object) -> str:
    if value is None or value is pd.NA:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    if isinstance(value, float):
        rounded = round(value, 1)
        if rounded == int(rounded):
            return str(int(rounded))
        return f"{rounded:.1f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    return str(value)


def _build_timestamps(out: pd.DataFrame) -> pd.DatetimeIndex:
    naive = pd.to_datetime(
        {
            "year": out["time.yy"].astype(int),
            "month": out["time.mm"].astype(int),
            "day": out["time.dd"].astype(int),
            "hour": out["time.hh"].astype(int),
        },
        errors="coerce",
    )
    localized = naive.dt.tz_localize(TIMEZONE, ambiguous=False, nonexistent="shift_forward")
    return pd.DatetimeIndex(localized)


def _series_on_timestamps(values: pd.Series, timestamps: pd.DatetimeIndex) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return pd.Series(numeric.to_numpy(dtype="float64", na_value=float("nan")), index=timestamps, dtype="float64")


def _extract_erbs_components(erbs: Any, timestamps: pd.DatetimeIndex) -> tuple[pd.Series, pd.Series]:
    dni_raw = erbs["dni"]
    dhi_raw = erbs["dhi"]
    if isinstance(dni_raw, pd.Series):
        return dni_raw.astype(float).copy(), dhi_raw.astype(float).copy()
    return (
        pd.Series(dni_raw, index=timestamps, dtype="float64"),
        pd.Series(dhi_raw, index=timestamps, dtype="float64"),
    )


def _decompose_global_radiation(
    timestamps: pd.DatetimeIndex,
    ghi: pd.Series,
    dhi_measured: pd.Series | None,
    *,
    latitude: float,
    longitude: float,
) -> tuple[pd.Series, pd.Series]:
    """Derive DNI (rad.direct) and DHI (rad.diffus) from global horizontal irradiance.

    Uses pvlib Erbs decomposition when diffuse (ods000h0) is unavailable. When measured
    diffuse is available, it is used as DHI and DNI is computed from:
        DNI = max(GHI - DHI, 0) / cos(zenith)
    matching the approach in weather.py:calculate_direct_normal_radiation.
    """
    ghi_numeric = _series_on_timestamps(ghi, timestamps)
    dhi_measured_aligned = (
        _series_on_timestamps(dhi_measured, timestamps) if dhi_measured is not None else None
    )

    solpos = solarposition.get_solarposition(timestamps, latitude, longitude)
    zenith = solpos["apparent_zenith"].clip(upper=ZENITH_CAP_DEG)
    cos_zenith = zenith.apply(math.radians).apply(math.cos).clip(lower=1e-6)

    ghi_fill = ghi_numeric.fillna(0.0)

    # Day-of-year avoids pvlib/pandas issues with tz-aware Series in newer pandas.
    erbs = irradiance.erbs(ghi_fill, zenith, timestamps.dayofyear)
    dni, dhi = _extract_erbs_components(erbs, timestamps)

    if dhi_measured_aligned is not None:
        has_measured = dhi_measured_aligned.notna()
        if has_measured.any():
            dhi.loc[has_measured] = dhi_measured_aligned.loc[has_measured]
            direct_horizontal = (ghi_fill - dhi).clip(lower=0.0)
            dni.loc[has_measured] = direct_horizontal.loc[has_measured] / cos_zenith.loc[has_measured]

    night = (ghi_fill <= 0.0) | (zenith >= ZENITH_CAP_DEG)
    dni.loc[night] = 0.0
    dhi.loc[night] = 0.0

    dni = dni.where(ghi_numeric.notna(), pd.NA)
    dhi = dhi.where(ghi_numeric.notna(), pd.NA)
    return dni.reset_index(drop=True), dhi.reset_index(drop=True)


@dataclass
class ValidationMetrics:
    """Erbs vs reference DRY CSV comparison for one radiation component."""

    label: str
    n_hours: int
    mae: float
    rmse: float
    bias: float


def _radiation_errors(reference: pd.Series, estimated: pd.Series) -> tuple[float, float, float]:
    diff = estimated.astype(float) - reference.astype(float)
    mae = float(diff.abs().mean())
    rmse = float((diff**2).mean()) ** 0.5
    bias = float(diff.mean())
    return mae, rmse, bias


def validate_erbs_against_reference(
    station_abbr: str,
    *,
    sample_hours: int = 12,
) -> list[ValidationMetrics]:
    """Compare Erbs GHI decomposition against the location DRY reference CSV.

    Uses the reference file's GHI (rad.global) as input, decomposes with the same Erbs
    model as the historical conversion, and compares to reference DNI/DHI (rad.direct,
    rad.diffus). Reports full-year metrics for daylight hours (GHI > 0) and a small
    sample of individual hours across the year.
    """
    cfg = STATION_CONFIG[station_abbr]
    reference_csv = CUSTOM_CSV_ROOT / cfg["reference_csv"]
    if not reference_csv.is_file():
        raise FileNotFoundError(f"Reference custom CSV not found: {reference_csv}")

    ref = pd.read_csv(reference_csv)
    required = ("time.yy", "time.mm", "time.dd", "time.hh", "rad.global", "rad.direct", "rad.diffus")
    missing = [col for col in required if col not in ref.columns]
    if missing:
        raise ValueError(f"{reference_csv} is missing columns required for validation: {missing}")

    ts_index = _build_timestamps(ref)
    dni_est, dhi_est = _decompose_global_radiation(
        ts_index,
        ref["rad.global"],
        dhi_measured=None,
        latitude=float(cfg["latitude"]),
        longitude=float(cfg["longitude"]),
    )

    daylight = ref["rad.global"].astype(float) > 0
    n_daylight = int(daylight.sum())
    if n_daylight == 0:
        raise ValueError(f"{reference_csv} contains no daylight hours (rad.global > 0)")

    dni_ref = ref.loc[daylight, "rad.direct"].astype(float)
    dhi_ref = ref.loc[daylight, "rad.diffus"].astype(float)
    dni_cmp = dni_est.loc[daylight].astype(float)
    dhi_cmp = dhi_est.loc[daylight].astype(float)

    dni_metrics = ValidationMetrics("DNI (rad.direct)", n_daylight, *_radiation_errors(dni_ref, dni_cmp))
    dhi_metrics = ValidationMetrics("DHI (rad.diffus)", n_daylight, *_radiation_errors(dhi_ref, dhi_cmp))

    print(f"Erbs validation against {reference_csv.name}")
    print(f"Station {station_abbr} -> {cfg['hisim_station']} @ {cfg['latitude']}, {cfg['longitude']}")
    print(f"Daylight hours evaluated (GHI > 0): {n_daylight}")
    print()
    print(f"{'Component':<20} {'MAE':>8} {'RMSE':>8} {'Bias':>8}")
    print("-" * 46)
    for metrics in (dni_metrics, dhi_metrics):
        print(
            f"{metrics.label:<20} {metrics.mae:8.1f} {metrics.rmse:8.1f} {metrics.bias:8.1f}"
        )
    print()
    print("Bias = mean(estimated - reference); units W/m²")
    print()

    # Sample hours: pick the hour with maximum GHI in each month for readable spot checks.
    sample_rows: list[pd.Series] = []
    ref_day = ref.loc[daylight].copy()
    ref_day = ref_day.assign(
        _dni_est=dni_est.loc[daylight].values,
        _dhi_est=dhi_est.loc[daylight].values,
    )
    for _, month_df in ref_day.groupby("time.mm", sort=True):
        sample_rows.append(month_df.loc[month_df["rad.global"].astype(float).idxmax()])

    sample = pd.DataFrame(sample_rows).head(sample_hours)
    print(f"Sample hours (max GHI per month, up to {sample_hours}):")
    print(
        f"{'Date':<12} {'Hr':>3} {'GHI':>6} "
        f"{'DNI_ref':>8} {'DNI_est':>8} {'DHI_ref':>8} {'DHI_est':>8}"
    )
    print("-" * 62)
    for _, row in sample.iterrows():
        date_label = f"{int(row['time.mm']):02d}-{int(row['time.dd']):02d}"
        print(
            f"{date_label:<12} {int(row['time.hh']):3d} "
            f"{float(row['rad.global']):6.0f} "
            f"{float(row['rad.direct']):8.0f} {float(row['_dni_est']):8.0f} "
            f"{float(row['rad.diffus']):8.0f} {float(row['_dhi_est']):8.0f}"
        )
    print()

    return [dni_metrics, dhi_metrics]


def validate_all_locations(*, sample_hours: int = 12) -> dict[str, list[ValidationMetrics]]:
    """Run Erbs validation for every station in STATION_CONFIG."""
    results: dict[str, list[ValidationMetrics]] = {}
    station_abbrs = sorted(STATION_CONFIG)
    for index, station_abbr in enumerate(station_abbrs):
        if index > 0:
            print("=" * 62)
            print()
        reference_csv = CUSTOM_CSV_ROOT / STATION_CONFIG[station_abbr]["reference_csv"]
        if not reference_csv.is_file():
            print(f"Skipping {station_abbr}: reference CSV not found ({reference_csv.name})")
            print()
            continue
        try:
            results[station_abbr] = validate_erbs_against_reference(
                station_abbr,
                sample_hours=sample_hours,
            )
            _print_validation_note(results[station_abbr])
        except (FileNotFoundError, ValueError) as exc:
            print(f"Skipping {station_abbr}: {exc}")
            print()
    return results


def _print_validation_note(metrics: list[ValidationMetrics]) -> None:
    dni, dhi = metrics
    if dni.mae > 100 or dhi.mae > 50:
        print("Note: Erbs is a statistical model; moderate deviations from the DRY")
        print("reference are expected, especially for DNI under mixed cloud conditions.")
        print()


def convert_year(
    source: pd.DataFrame,
    *,
    year: int,
    hisim_station: str,
    latitude: float,
    longitude: float,
    reference_airpres_hpa: float,
) -> tuple[pd.DataFrame, ConversionSummary]:
    """Map one calendar year from an SMN historical dataframe to the custom CSV schema."""
    timestamps = pd.to_datetime(
        source["reference_timestamp"],
        format="%d.%m.%Y %H:%M",
        errors="coerce",
    )
    year_mask = timestamps.dt.year == year
    subset = source.loc[year_mask].copy()
    summary = ConversionSummary(year=year, total_rows=len(subset))
    if subset.empty:
        summary.missing_by_column = {col: 0 for col in SIMULATION_COLUMNS}
        return pd.DataFrame(columns=CUSTOM_CSV_COLUMNS), summary

    out = pd.DataFrame(index=subset.index)
    out["station"] = hisim_station
    out["time.yy"] = timestamps.loc[year_mask].dt.year
    out["time.mm"] = timestamps.loc[year_mask].dt.month
    out["time.dd"] = timestamps.loc[year_mask].dt.day
    out["time.hh"] = timestamps.loc[year_mask].dt.hour

    for smn_col, custom_col in SMN_SOURCE_MAP.items():
        if smn_col in subset.columns:
            out[custom_col] = _to_numeric(subset[smn_col])
        else:
            out[custom_col] = pd.NA

    if "ods000h0" in subset.columns:
        out["_dhi_measured"] = _to_numeric(subset["ods000h0"])

    out["airpres"] = out["airpres"].fillna(reference_airpres_hpa)

    out = out.sort_values(["time.mm", "time.dd", "time.hh"]).reset_index(drop=True)
    dhi_measured = out.pop("_dhi_measured") if "_dhi_measured" in out.columns else None
    ts_index = _build_timestamps(out)
    out["rad.direct"], out["rad.diffus"] = _decompose_global_radiation(
        ts_index,
        out["rad.global"],
        dhi_measured,
        latitude=latitude,
        longitude=longitude,
    )

    for col in ("rad.vert.N", "rad.vert.E", "rad.vert.S", "rad.vert.W"):
        out[col] = 0

    for col in CUSTOM_CSV_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA

    out = out[CUSTOM_CSV_COLUMNS]
    for col in SIMULATION_COLUMNS:
        summary.missing_by_column[col] = int(out[col].isna().sum()) if col in out.columns else summary.total_rows
    return out, summary


def write_custom_csv(df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CUSTOM_CSV_COLUMNS)
        for row in df.itertuples(index=False, name=None):
            writer.writerow([_format_value(value) for value in row])


def _output_name(hisim_station: str, year: int) -> str:
    return f"{hisim_station}_{year}_CS.csv"


def _print_summary(output_path: Path, summary: ConversionSummary) -> None:
    if summary.total_rows == 0:
        print(f"  {summary.year}: no rows found (skipped {output_path.name})")
        return
    print(f"  {summary.year}: wrote {output_path.name} ({summary.total_rows} rows)")
    missing = {col: count for col, count in summary.missing_by_column.items() if count > 0}
    if missing:
        for col, count in sorted(missing.items(), key=lambda item: (-item[1], item[0])):
            pct = 100.0 * count / summary.total_rows
            print(f"    missing {col}: {count} ({pct:.1f}%)")


def convert_station_years(
    *,
    station_abbr: str,
    years: list[int],
    input_dir: Path,
    output_dir: Path,
) -> int:
    cfg = STATION_CONFIG[station_abbr]
    reference_csv = CUSTOM_CSV_ROOT / cfg["reference_csv"]
    if not reference_csv.is_file():
        raise FileNotFoundError(f"Reference custom CSV not found: {reference_csv}")

    input_files = sorted(input_dir.glob(cfg["input_glob"]))
    if not input_files:
        raise FileNotFoundError(
            f"No input files matching {cfg['input_glob']!r} in {input_dir}"
        )

    source = pd.concat(
        [pd.read_csv(path, sep=";", dtype="string", keep_default_na=False) for path in input_files],
        ignore_index=True,
    )
    if "reference_timestamp" not in source.columns:
        raise ValueError("Historical CSV is missing required column reference_timestamp")

    reference_airpres = _reference_airpres_hpa(reference_csv)
    hisim_station = cfg["hisim_station"]

    print(f"Station {station_abbr} -> {hisim_station}")
    print(f"Coordinates: {cfg['latitude']}, {cfg['longitude']}")
    print(f"Reference air pressure fallback: {reference_airpres:.1f} hPa ({reference_csv.name})")
    print(f"Radiation: Erbs decomposition of GHI -> DNI + DHI (ods000h0 used when present)")
    print(f"Years: {', '.join(str(year) for year in years)}")
    print()

    for year in years:
        converted, summary = convert_year(
            source,
            year=year,
            hisim_station=hisim_station,
            latitude=float(cfg["latitude"]),
            longitude=float(cfg["longitude"]),
            reference_airpres_hpa=reference_airpres,
        )
        if summary.total_rows == 0:
            _print_summary(output_dir / _output_name(hisim_station, year), summary)
            continue
        output_path = output_dir / _output_name(hisim_station, year)
        write_custom_csv(converted, output_path)
        _print_summary(output_path, summary)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert MeteoSwiss SMN historical CSVs to yearly HiSim custom weather CSVs."
    )
    parser.add_argument(
        "--station",
        default="SMA",
        choices=sorted(STATION_CONFIG),
        help="SMN station abbreviation (SMA=ZUESTA, BAS=BASSTA, KLO=KLO, RUE=RUE).",
    )
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=list(DEFAULT_YEARS),
        help=f"Calendar years to export (default: {' '.join(str(y) for y in DEFAULT_YEARS)}).",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=SCRIPT_DIR,
        help="Directory containing ogd-smn_* historical CSV files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for converted CSVs (default: custom_csv/<location subdir>).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Run Erbs validation for all locations against their DRY reference CSVs and exit.",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip Erbs validation before conversion (all locations validated by default).",
    )
    args = parser.parse_args()

    if not args.skip_validate or args.validate_only:
        validate_all_locations()

    if args.validate_only:
        return 0

    cfg = STATION_CONFIG[args.station]
    output_dir = args.output_dir or (CUSTOM_CSV_ROOT / cfg["output_subdir"])

    convert_station_years(
        station_abbr=args.station,
        years=sorted(set(args.years)),
        input_dir=args.input_dir,
        output_dir=output_dir,
    )

    print()
    print("Columns left blank (not used by HiSim):")
    print("  relhum, vappres, dewpt, mixratio, wetbulb, enthalpy, precip")
    print("  winddir, windmax, ir.horiz, cloudcov, albedo, emissivity")
    print("rad.vert.N/E/S/W set to 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
