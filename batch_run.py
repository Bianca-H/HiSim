"""Batch runner for HiSim simulations.

Supports two ways of defining runs:
1) Matrix mode: one system setup × multiple ARCH × multiple WEATHER (cartesian product)
2) Commands mode: explicit commands to execute

Runs simulations sequentially, continues on failures, and reports failures at the end.
Creates per-run log files under `batch_runner_logs/<timestamp>/`.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional


@dataclass
class RunResult:
    index: int
    name: str
    command: List[str]
    returncode: Optional[int]
    log_path: Path
    error: Optional[str] = None


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _default_hisim_main() -> Path:
    return _repo_root() / "hisim" / "hisim_main.py"


def _default_cwd() -> Path:
    # Most users run `hisim_main.py basic_household` from within `system_setups/`.
    return _repo_root() / "system_setups"


def _format_override_value(value: Any) -> str:
    """Serialize an override value for CLI KEY=VALUE args.

    Lists/tuples become comma-separated (e.g. SCENARIO=[a,b] → SCENARIO=a,b).
    """
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ",".join(parts)
    return str(value).strip()


def _iter_matrix_runs(
    hisim_main: Path,
    setup: str,
    arch_values: Iterable[str],
    weather_values: Iterable[str],
    extra_overrides: Optional[dict[str, Any]] = None,
    start_index: int = 0,
) -> List[tuple[str, List[str]]]:
    runs: List[tuple[str, List[str]]] = []
    extra_overrides = extra_overrides or {}
    base_override_args: List[str] = []
    for k, v in extra_overrides.items():
        key = str(k).strip().upper()
        if not key:
            continue
        base_override_args.append(f"{key}={_format_override_value(v)}")
    run_counter = int(start_index)
    for arch in arch_values:
        for weather in weather_values:
            run_counter += 1
            explorer_override = f"BATCH_OPEN_EXPLORER={'1' if run_counter == 1 else '0'}"
            override_args = [*base_override_args, explorer_override]
            name = f"{setup} ARCH={arch} WEATHER={weather}"
            if override_args:
                name += " " + " ".join(override_args)
            cmd = [
                sys.executable,
                str(hisim_main),
                setup,
                f"ARCH={arch}",
                f"WEATHER={weather}",
                *override_args,
            ]
            runs.append((name, cmd))
    return runs


def _iter_command_runs(commands: Iterable[Any]) -> List[tuple[str, List[str]]]:
    runs: List[tuple[str, List[str]]] = []
    for i, entry in enumerate(commands, start=1):
        if isinstance(entry, dict):
            name = str(entry.get("name") or f"cmd_{i}")
            cmd_raw = entry.get("cmd")
        else:
            name = f"cmd_{i}"
            cmd_raw = entry

        if cmd_raw is None:
            raise ValueError(f"Command entry #{i} is missing `cmd`.")

        if isinstance(cmd_raw, list):
            cmd = [str(x) for x in cmd_raw]
        elif isinstance(cmd_raw, str):
            # On Windows, shlex.split(..., posix=False) behaves more like cmd parsing.
            cmd = shlex.split(cmd_raw, posix=(sys.platform != "win32"))
        else:
            raise TypeError(f"Command entry #{i} must be a string or list, got {type(cmd_raw)}.")

        runs.append((name, cmd))
    return runs


def _write_log_header(fp, name: str, cmd: List[str], cwd: Path) -> None:
    fp.write(f"NAME: {name}\n")
    fp.write(f"CWD:  {cwd}\n")
    fp.write("CMD:  " + " ".join(cmd) + "\n")
    fp.write("=" * 120 + "\n\n")


def _run_one(index: int, name: str, cmd: List[str], cwd: Path, log_dir: Path) -> RunResult:
    log_path = log_dir / f"{index:04d}_{name.replace(' ', '_').replace('=', '-')}.log"
    try:
        with log_path.open("w", encoding="utf-8") as fp:
            _write_log_header(fp, name=name, cmd=cmd, cwd=cwd)
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.stdout:
                fp.write("STDOUT\n------\n")
                fp.write(proc.stdout)
                fp.write("\n\n")
            if proc.stderr:
                fp.write("STDERR\n------\n")
                fp.write(proc.stderr)
                fp.write("\n\n")

        return RunResult(
            index=index,
            name=name,
            command=cmd,
            returncode=proc.returncode,
            log_path=log_path,
            error=None if proc.returncode == 0 else "nonzero_exit",
        )
    except Exception as exc:
        # Best effort to record the failure.
        try:
            with log_path.open("a", encoding="utf-8") as fp:
                fp.write("\nEXCEPTION\n---------\n")
                fp.write(repr(exc) + "\n")
        except Exception:
            pass
        return RunResult(
            index=index,
            name=name,
            command=cmd,
            returncode=None,
            log_path=log_path,
            error=repr(exc),
        )


def _load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def _runs_from_config(cfg: dict, hisim_main: Path) -> List[tuple[str, List[str]]]:
    mode = str(cfg.get("mode") or "").strip().lower()
    if mode == "matrix":
        setup_raw = cfg["setup"]
        arch_values = cfg.get("arch") or []
        weather_values = cfg.get("weather") or []
        extra_overrides = cfg.get("overrides") or {}
        if not arch_values or not weather_values:
            raise ValueError("Matrix mode needs non-empty `arch` and `weather` lists.")
        if extra_overrides is not None and not isinstance(extra_overrides, dict):
            raise TypeError("Matrix mode `overrides` must be a JSON object (dict).")
        # `setup` can be a single string or a list of strings.
        if isinstance(setup_raw, list):
            setups = [str(x).strip() for x in setup_raw if str(x).strip()]
        else:
            setups = [str(setup_raw).strip()]
        if not setups:
            raise ValueError("Matrix mode needs non-empty `setup` (string or list of strings).")

        runs: List[tuple[str, List[str]]] = []
        # Ensure BATCH_OPEN_EXPLORER=1 only for the very first run across all setups.
        run_index = 0
        for setup in setups:
            setup_runs = _iter_matrix_runs(
                hisim_main=hisim_main,
                setup=setup,
                arch_values=arch_values,
                weather_values=weather_values,
                extra_overrides=extra_overrides,
                start_index=run_index,
            )
            run_index += len(setup_runs)
            runs.extend(setup_runs)
        return runs
    if mode == "commands":
        commands = cfg.get("commands") or []
        if not commands:
            raise ValueError("Commands mode needs a non-empty `commands` list.")
        return _iter_command_runs(commands)
    raise ValueError("Config `mode` must be either 'matrix' or 'commands'.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch runner for HiSim simulations.")
    parser.add_argument("--config", type=str, default=None, help="Path to JSON config file.")
    parser.add_argument("--mode", type=str, choices=["matrix", "commands"], default=None, help="Run mode (if not using --config).")
    parser.add_argument("--setup", type=str, default=None, help="System setup module name (e.g. basic_household) for matrix mode.")
    parser.add_argument("--arch", type=str, nargs="*", default=None, help="List of ARCH values for matrix mode (e.g. 01_CH 05_CH).")
    parser.add_argument("--weather", type=str, nargs="*", default=None, help="List of WEATHER values for matrix mode (e.g. ZUESTA BASSTA).")
    parser.add_argument("--command", type=str, action="append", default=None, help="Explicit command (repeatable) for commands mode.")
    parser.add_argument("--cwd", type=str, default=None, help="Working directory for all runs (default: system_setups).")
    parser.add_argument("--hisim-main", type=str, default=None, help="Path to hisim_main.py (default: <repo>/hisim/hisim_main.py).")
    args = parser.parse_args()

    hisim_main = Path(args.hisim_main).resolve() if args.hisim_main else _default_hisim_main()
    if not hisim_main.is_file():
        raise FileNotFoundError(f"hisim_main.py not found at {hisim_main}")

    cwd = Path(args.cwd).resolve() if args.cwd else _default_cwd()
    if not cwd.is_dir():
        raise NotADirectoryError(f"Working directory not found: {cwd}")

    if args.config:
        cfg = _load_config(Path(args.config).resolve())
        runs = _runs_from_config(cfg, hisim_main=hisim_main)
    else:
        if not args.mode:
            raise ValueError("Provide either --config or --mode.")
        if args.mode == "matrix":
            if not args.setup:
                raise ValueError("Matrix mode needs --setup.")
            arch_values = args.arch or []
            weather_values = args.weather or []
            if not arch_values or not weather_values:
                raise ValueError("Matrix mode needs non-empty --arch and --weather lists.")
            runs = _iter_matrix_runs(hisim_main=hisim_main, setup=args.setup, arch_values=arch_values, weather_values=weather_values)
        else:
            commands = args.command or []
            if not commands:
                raise ValueError("Commands mode needs at least one --command.")
            runs = _iter_command_runs(commands)

    log_dir = _repo_root() / "batch_runner_logs" / _timestamp()
    log_dir.mkdir(parents=True, exist_ok=True)

    results: List[RunResult] = []
    for idx, (name, cmd) in enumerate(runs, start=1):
        print(f"[{idx}/{len(runs)}] Running {name}")
        results.append(_run_one(index=idx, name=name, cmd=cmd, cwd=cwd, log_dir=log_dir))

    failures = [r for r in results if r.returncode not in (0,) or r.error is not None]
    if failures:
        print("\nFailures:")
        for r in failures:
            rc = "EXC" if r.returncode is None else str(r.returncode)
            print(f"- #{r.index:04d} rc={rc} {r.name}")
            print(f"  log: {r.log_path}")
        print(f"\n{len(failures)}/{len(results)} runs failed. Logs in {log_dir}")
        return 1

    print(f"\nAll {len(results)} runs succeeded. Logs in {log_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

