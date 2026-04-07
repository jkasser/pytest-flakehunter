"""
pytest-flakehunter: Historical run persistence.

Writes per-test CSVs to .flakehunter/history/ — one file per test nodeid,
append-only so history accumulates across runs. Also captures hardware
context and test arguments for AI correlation analysis.
"""

from __future__ import annotations

import csv
import hashlib
import os
import platform
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pytest_flakehunter.plugin import TestFlakeRecord, AttemptResult

from pytest_flakehunter.plugin import extract_frames, extract_error, failure_fingerprint, short_path

# ──────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────

FIELDS = [
    # Run identity
    "run_id",
    "timestamp_utc",
    "flakehunter_version",

    # Environment
    "hostname",
    "os_name",
    "os_version",
    "python_version",
    "pytest_version",
    "cpu_count",
    "cpu_freq_mhz",
    "ram_total_gb",
    "ram_available_gb",

    # Git context
    "git_branch",
    "git_commit",
    "git_dirty",

    # Test identity
    "nodeid",
    "test_file",
    "test_function",
    "parametrize_id",       # e.g. "user_id=42-role=admin"
    "fixture_names",        # comma-separated fixture names used
    "fixture_values",       # JSON-encoded {name: repr(value)} — best-effort

    # Run config
    "total_runs_requested",

    # Attempt data
    "attempt",
    "outcome",
    "total_duration_s",
    "setup_duration_s",
    "call_duration_s",
    "teardown_duration_s",

    # Failure detail
    "error_type",
    "error_msg",
    "failure_function",
    "failure_file",
    "failure_lineno",
    "traceback_fingerprint",
    "failure_depth",         # How many frames deep the failure was
]


# ──────────────────────────────────────────────
# Hardware snapshot (captured once per session)
# ──────────────────────────────────────────────

_hw_cache: Optional[dict] = None


def capture_hardware() -> dict:
    global _hw_cache
    if _hw_cache is not None:
        return _hw_cache

    hw = {
        "hostname": socket.gethostname(),
        "os_name": platform.system(),
        "os_version": platform.release(),
        "python_version": sys.version.split()[0],
        "pytest_version": _safe_import_version("pytest"),
        "cpu_count": os.cpu_count() or "",
        "cpu_freq_mhz": "",
        "ram_total_gb": "",
        "ram_available_gb": "",
    }

    # CPU frequency
    try:
        import psutil
        freq = psutil.cpu_freq()
        hw["cpu_freq_mhz"] = f"{freq.current:.0f}" if freq else ""
        mem = psutil.virtual_memory()
        hw["ram_total_gb"] = f"{mem.total / 1e9:.2f}"
        hw["ram_available_gb"] = f"{mem.available / 1e9:.2f}"
    except ImportError:
        # psutil is optional — fall back gracefully
        try:
            if platform.system() == "Linux":
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal"):
                            kb = int(line.split()[1])
                            hw["ram_total_gb"] = f"{kb / 1e6:.2f}"
                        if line.startswith("MemAvailable"):
                            kb = int(line.split()[1])
                            hw["ram_available_gb"] = f"{kb / 1e6:.2f}"
        except Exception:
            pass

    _hw_cache = hw
    return hw


def capture_git() -> dict:
    """Best-effort git context — silently returns empty strings on failure."""
    result = {"git_branch": "", "git_commit": "", "git_dirty": ""}
    try:
        result["git_branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
        result["git_commit"] = _git("rev-parse", "--short", "HEAD")
        dirty = _git("status", "--porcelain")
        result["git_dirty"] = "true" if dirty else "false"
    except Exception:
        pass
    return result


def _git(*args) -> str:
    out = subprocess.check_output(
        ["git"] + list(args),
        stderr=subprocess.DEVNULL,
        timeout=3,
    )
    return out.decode().strip()


def _safe_import_version(pkg: str) -> str:
    try:
        import importlib.metadata
        return importlib.metadata.version(pkg)
    except Exception:
        return ""


# ──────────────────────────────────────────────
# Argument extraction
# ──────────────────────────────────────────────

def extract_test_args(item) -> dict:
    """
    Pull parametrize ID and fixture values from a pytest item.
    Returns a dict with parametrize_id, fixture_names, fixture_values.
    """
    import json

    parametrize_id = ""
    fixture_names = ""
    fixture_values = ""

    try:
        # Parametrize ID comes from callspec
        if hasattr(item, "callspec"):
            cs = item.callspec
            # Build a readable ID from param names+values
            parts = []
            for k, v in cs.params.items():
                parts.append(f"{k}={_safe_repr(v)}")
            parametrize_id = " | ".join(parts)

        # Fixture names from the item's fixturenames
        if hasattr(item, "fixturenames"):
            # Exclude internal pytest fixtures
            SKIP = {"request", "tmp_path", "tmp_path_factory", "capsys",
                    "capfd", "monkeypatch", "pytestconfig", "record_xml_attribute"}
            names = [n for n in item.fixturenames if n not in SKIP]
            fixture_names = ",".join(names)

            # Try to get actual fixture values (only works post-setup)
            vals = {}
            for name in names[:8]:  # Cap at 8 to keep rows manageable
                try:
                    val = item.funcargs.get(name)
                    if val is not None:
                        vals[name] = _safe_repr(val)
                except Exception:
                    pass
            if vals:
                fixture_values = json.dumps(vals, ensure_ascii=False)

    except Exception:
        pass

    return {
        "parametrize_id": parametrize_id,
        "fixture_names": fixture_names,
        "fixture_values": fixture_values,
    }


def _safe_repr(val, max_len: int = 80) -> str:
    """repr() a value safely, truncating if needed."""
    try:
        r = repr(val)
        return r[:max_len] + "…" if len(r) > max_len else r
    except Exception:
        return "<unrepresentable>"


# ──────────────────────────────────────────────
# CSV writer
# ──────────────────────────────────────────────

class HistoryWriter:
    def __init__(self, history_dir: str = ".flakehunter/history"):
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = str(uuid.uuid4())[:8]
        self.timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.hw = capture_hardware()
        self.git = capture_git()
        self._version = _safe_import_version("pytest-flakehunter") or "dev"

    def write_record(
        self,
        record: "TestFlakeRecord",
        item,
        total_runs: int,
    ) -> None:
        """Append all attempts for a single test to its history CSV."""
        csv_path = self.history_dir / (_nodeid_to_filename(record.nodeid) + ".csv")
        is_new = not csv_path.exists()

        test_args = extract_test_args(item)

        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if is_new:
                writer.writeheader()

            for attempt in record.attempts:
                row = self._build_row(record, attempt, test_args, total_runs)
                writer.writerow(row)

    def _build_row(
        self,
        record: "TestFlakeRecord",
        attempt: "AttemptResult",
        test_args: dict,
        total_runs: int,
    ) -> dict:
        phases = {r.when: r for r in attempt.reports}
        fr = attempt.failed_report
        frames = extract_frames(fr.longrepr) if fr else []
        error_type, error_msg = extract_error(fr.longrepr) if fr else ("", "")
        last_frame = frames[-1] if frames else None  # (filename, lineno, func, ctx)

        return {
            # Identity
            "run_id": self.run_id,
            "timestamp_utc": self.timestamp,
            "flakehunter_version": self._version,

            # Environment
            **self.hw,
            **self.git,

            # Test identity
            "nodeid": record.nodeid,
            "test_file": record.file,
            "test_function": record.name,
            **test_args,

            # Config
            "total_runs_requested": total_runs,

            # Attempt
            "attempt": attempt.attempt,
            "outcome": attempt.outcome,
            "total_duration_s": f"{attempt.total_duration:.4f}",
            "setup_duration_s": f"{phases['setup'].duration:.4f}" if "setup" in phases else "",
            "call_duration_s": f"{phases['call'].duration:.4f}" if "call" in phases else "",
            "teardown_duration_s": f"{phases['teardown'].duration:.4f}" if "teardown" in phases else "",

            # Failure
            "error_type": error_type,
            "error_msg": error_msg[:200],
            "failure_function": last_frame[2] if last_frame else "",
            "failure_file": short_path(last_frame[0]) if last_frame else "",
            "failure_lineno": last_frame[1] if last_frame else "",
            "traceback_fingerprint": failure_fingerprint(fr.longrepr) or "" if fr else "",
            "failure_depth": len(frames),
        }


def _nodeid_to_filename(nodeid: str) -> str:
    """Convert a pytest nodeid to a safe filename."""
    safe = nodeid.replace("::", "__").replace("/", "_").replace("\\", "_")
    # Hash long names to keep filesystem happy
    if len(safe) > 180:
        safe = safe[:120] + "__" + hashlib.md5(nodeid.encode()).hexdigest()[:8]
    return safe


# ──────────────────────────────────────────────
# CSV reader (for report + AI analysis)
# ──────────────────────────────────────────────

def load_history(nodeid: str, history_dir: str = ".flakehunter/history") -> list[dict]:
    """Load all historical rows for a given test nodeid."""
    path = Path(history_dir) / (_nodeid_to_filename(nodeid) + ".csv")
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def summarize_history(rows: list[dict]) -> dict:
    """
    Compute aggregate stats from historical rows — used by the report
    and AI analysis to surface trends.
    """
    if not rows:
        return {}

    total = len(rows)
    failures = [r for r in rows if r.get("outcome") == "failed"]
    passes   = [r for r in rows if r.get("outcome") == "passed"]

    def safe_float(v):
        try: return float(v)
        except: return None

    durations = [d for r in rows if (d := safe_float(r.get("total_duration_s"))) is not None]
    fail_durations = [d for r in failures if (d := safe_float(r.get("total_duration_s"))) is not None]

    # Flake rate per run_id (so we can see trend over time)
    runs: dict[str, dict] = {}
    for r in rows:
        rid = r.get("run_id", "")
        if rid not in runs:
            runs[rid] = {"ts": r.get("timestamp_utc", ""), "total": 0, "failed": 0}
        runs[rid]["total"] += 1
        if r.get("outcome") == "failed":
            runs[rid]["failed"] += 1

    run_flake_rates = [
        {"run_id": rid, "timestamp": v["ts"],
         "flake_rate": v["failed"] / v["total"] if v["total"] else 0}
        for rid, v in sorted(runs.items(), key=lambda x: x[1]["ts"])
    ]

    # Environment breakdown
    env_flake: dict[str, dict] = {}
    for r in rows:
        host = r.get("hostname", "unknown")
        env_flake.setdefault(host, {"total": 0, "failed": 0})
        env_flake[host]["total"] += 1
        if r.get("outcome") == "failed":
            env_flake[host]["failed"] += 1

    # Arg correlation: which fixture values appear most in failures?
    arg_fail_counts: dict[str, int] = {}
    arg_total_counts: dict[str, int] = {}
    for r in rows:
        pid = r.get("parametrize_id", "")
        if pid:
            arg_total_counts[pid] = arg_total_counts.get(pid, 0) + 1
            if r.get("outcome") == "failed":
                arg_fail_counts[pid] = arg_fail_counts.get(pid, 0) + 1

    # Most common failure locations
    loc_counts: dict[str, int] = {}
    for r in failures:
        loc = f"{r.get('failure_function','?')}() in {r.get('failure_file','?')}:{r.get('failure_lineno','?')}"
        loc_counts[loc] = loc_counts.get(loc, 0) + 1

    return {
        "total_attempts": total,
        "total_failures": len(failures),
        "overall_flake_rate": len(failures) / total if total else 0,
        "avg_duration_s": sum(durations) / len(durations) if durations else 0,
        "p95_duration_s": sorted(durations)[int(len(durations) * 0.95)] if len(durations) > 1 else 0,
        "avg_fail_duration_s": sum(fail_durations) / len(fail_durations) if fail_durations else 0,
        "run_flake_rates": run_flake_rates,       # List of {run_id, timestamp, flake_rate}
        "env_breakdown": env_flake,               # {hostname: {total, failed}}
        "arg_correlation": {                      # {param_combo: {total, failed, rate}}
            k: {"total": arg_total_counts[k], "failed": arg_fail_counts.get(k, 0),
                "rate": arg_fail_counts.get(k, 0) / arg_total_counts[k]}
            for k in arg_total_counts
        },
        "top_failure_locations": sorted(loc_counts.items(), key=lambda x: -x[1])[:5],
        "unique_error_types": list({r.get("error_type","") for r in failures if r.get("error_type")}),
        "unique_hosts": list({r.get("hostname","") for r in rows}),
        "git_branches_seen": list({r.get("git_branch","") for r in rows if r.get("git_branch")}),
        "date_range": (
            min(r.get("timestamp_utc","") for r in rows if r.get("timestamp_utc")),
            max(r.get("timestamp_utc","") for r in rows if r.get("timestamp_utc")),
        ),
    }
