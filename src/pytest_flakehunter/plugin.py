"""
pytest-flakehunter: Re-run tests N times and collect telemetry for flakiness analysis.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from _pytest.runner import runtestprotocol


# ──────────────────────────────────────────────
# Helpers — operate on pytest's TestReport.longrepr
# ──────────────────────────────────────────────

def extract_frames(longrepr) -> list[tuple[str, int, str, str]]:
	"""Return list of (filename, lineno, function, code_ctx) from a longrepr."""
	if not longrepr:
		return []
	chain = getattr(longrepr, "chain", None)
	if not chain:
		return []
	reprtb, _, _ = chain[-1]
	frames = []
	for entry in (reprtb.reprentries if reprtb else []):
		loc = getattr(entry, "reprfileloc", None)
		if loc:
			func = (loc.message or "").removeprefix("in ")
			ctx = (getattr(entry, "lines", None) or [""])[0].strip()
			frames.append((str(loc.path), loc.lineno, func, ctx))
	return frames


def extract_error(longrepr) -> tuple[str, str]:
	"""Return (error_type, error_msg) from a longrepr."""
	if not longrepr:
		return "", ""
	chain = getattr(longrepr, "chain", None)
	reprcrash = chain[-1][1] if chain else getattr(longrepr, "reprcrash", None)
	if not reprcrash:
		return "", ""
	msg = reprcrash.message
	if ": " in msg:
		t, m = msg.split(": ", 1)
		return t, m
	return msg, msg


def failure_fingerprint(longrepr) -> Optional[str]:
	"""MD5 of last 3 frame locations — used to cluster similar failures."""
	frames = extract_frames(longrepr)
	if not frames:
		return None
	key = "|".join(f"{f[0]}:{f[1]}" for f in frames[-3:])
	return hashlib.md5(key.encode()).hexdigest()[:8]


def short_path(filename: str) -> str:
	parts = filename.replace("\\", "/").split("/")
	return "/".join(parts[-2:]) if len(parts) > 2 else filename


# ──────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────

@dataclass
class AttemptResult:
	attempt: int
	reports: list = field(default_factory=list)  # list[TestReport]

	@property
	def outcome(self) -> str:
		return "failed" if any(r.outcome == "failed" for r in self.reports) else "passed"

	@property
	def total_duration(self) -> float:
		return sum(r.duration for r in self.reports)

	@property
	def failed_report(self):  # -> Optional[TestReport]
		return next((r for r in self.reports if r.outcome == "failed"), None)


@dataclass
class TestFlakeRecord:
	nodeid: str
	attempts: list[AttemptResult] = field(default_factory=list)

	@property
	def name(self) -> str:
		return self.nodeid.split("::")[-1]

	@property
	def file(self) -> str:
		return self.nodeid.split("::")[0]

	@property
	def flake_rate(self) -> float:
		if not self.attempts:
			return 0.0
		return sum(1 for a in self.attempts if a.outcome == "failed") / len(self.attempts)

	@property
	def pass_count(self) -> int:
		return sum(1 for a in self.attempts if a.outcome == "passed")

	@property
	def fail_count(self) -> int:
		return sum(1 for a in self.attempts if a.outcome == "failed")

	def failure_clusters(self) -> dict[str, list[AttemptResult]]:
		"""Group failed attempts by stack trace fingerprint."""
		clusters: dict[str, list[AttemptResult]] = {}
		for attempt in self.attempts:
			fr = attempt.failed_report
			if fr:
				fp = failure_fingerprint(fr.longrepr) or "unknown"
				clusters.setdefault(fp, []).append(attempt)
		return clusters


# ──────────────────────────────────────────────
# Internal: preserve session-scoped fixtures between runs
# ──────────────────────────────────────────────

class _IntermediateNextItem:
	"""
	Fake 'nextitem' passed to runtestprotocol for all but the last attempt.

	pytest's _setupstate.teardown_exact(nextitem) compares listchain() of the
	current item and nextitem to decide which fixture scopes to tear down.
	By returning the parent chain without the Function node we tell pytest:
	  - tear down function-scoped fixtures (page, browser_context, login…)
	  - keep session/module/class fixtures  (event_loop, playwright, browser…)
	"""
	def __init__(self, item):
		self._chain = item.listchain()[:-1]
		self.nodeid = item.nodeid
		self.config = item.config
		self.session = item.session

	def listchain(self):
		return self._chain


# ──────────────────────────────────────────────
# pytest hooks
# ──────────────────────────────────────────────

def pytest_addoption(parser):
	group = parser.getgroup("fh", "Flake Hunter - flakiness analysis")
	group.addoption("--fh", action="store_true", default=False,
					help="Enable flake hunter mode: re-run each test multiple times")
	group.addoption("--fh-runs", type=int, default=10, metavar="N",
					help="Number of times to run each test (default: 10)")
	group.addoption("--fh-report", type=str, default="flakehunter_report.html", metavar="PATH",
					help="Output path for HTML report (default: flakehunter_report.html)")
	group.addoption("--fh-ai", action="store_true", default=False,
					help="Use Claude AI to generate root cause hypotheses (requires ANTHROPIC_API_KEY)")
	group.addoption("--fh-history-dir", type=str, default=".flakehunter/history", metavar="PATH",
					help="Directory for persistent CSV history (default: .flakehunter/history)")
	group.addoption("--fh-no-history", action="store_true", default=False,
					help="Disable history recording for this run")
	group.addoption("--fh-isolate", action="store_true", default=False,
					help="Full fixture teardown between every attempt (slower but catches session-state flakes)")


def pytest_configure(config):
	if config.getoption("--fh", default=False):
		plugin = FlakeHunterPlugin(
			runs=config.getoption("--fh-runs"),
			report_path=config.getoption("--fh-report"),
			use_ai=config.getoption("--fh-ai"),
			history_dir=config.getoption("--fh-history-dir"),
			no_history=config.getoption("--fh-no-history"),
			isolate=config.getoption("--fh-isolate"),
		)
		config.pluginmanager.register(plugin, "flakehunter_plugin")
		if config.pluginmanager.hasplugin("xdist"):
			config.pluginmanager.register(
				_FlakeHunterXdistPlugin(plugin), "flakehunter_xdist"
			)


class FlakeHunterPlugin:
	def __init__(self, runs, report_path, use_ai,
				 history_dir=".flakehunter/history", no_history=False, isolate=False):
		self.runs = runs
		self.report_path = report_path
		self.use_ai = use_ai
		self.isolate = isolate
		self.records: dict[str, TestFlakeRecord] = {}
		self.history = None
		if not no_history:
			from pytest_flakehunter.history import HistoryWriter
			self.history = HistoryWriter(history_dir=history_dir)

	def pytest_runtest_protocol(self, item, nextitem):
		record = TestFlakeRecord(nodeid=item.nodeid)
		self.records[item.nodeid] = record
		intermediate = _IntermediateNextItem(item)

		for n in range(1, self.runs + 1):
			if self.isolate:
				# Full teardown after every attempt — catches session-state flakes
				# but is slower since all fixtures are rebuilt each time.
				run_next = nextitem if n == self.runs else None
			else:
				# Default: preserve session/module fixtures across attempts for speed.
				run_next = nextitem if n == self.runs else intermediate
			reports = runtestprotocol(item, log=True, nextitem=run_next)
			record.attempts.append(AttemptResult(attempt=n, reports=reports))

		if self.history:
			try:
				self.history.write_record(record, item, total_runs=self.runs)
			except Exception as exc:
				print(f"\n[flakehunter] could not write history for {record.nodeid}: {exc}")

		return True

	def pytest_sessionfinish(self, session, exitstatus):
		is_worker = hasattr(session.config, "workeroutput")

		if is_worker:
			# Ship our slice of records to the controller — don't touch the report
			session.config.workeroutput["flakehunter_records"] = _serialize_records(
				self.records, session.config
			)
			return

		# Controller (or plain non-xdist run) — we now have all records
		from pytest_flakehunter.reporter import generate_report
		history_dir = str(self.history.history_dir) if self.history else None
		generate_report(
			records=list(self.records.values()),
			runs=self.runs,
			report_path=self.report_path,
			use_ai=self.use_ai,
			history_dir=history_dir,
		)
		if self.history:
			print(f"\n[flakehunter] history: {self.history.history_dir}/")
		print(f"[flakehunter] report:  {self.report_path}")


class _FlakeHunterXdistPlugin:
	"""Registers xdist-specific hooks only when pytest-xdist is installed."""
	def __init__(self, main_plugin: "FlakeHunterPlugin"):
		self._plugin = main_plugin

	def pytest_testnodedown(self, node, error):
		"""Controller-side: merge records shipped in from a finished worker."""
		worker_data = node.workeroutput.get("flakehunter_records")
		if worker_data:
			merged = _deserialize_records(worker_data, node.config)
			self._plugin.records.update(merged)


def _serialize_records(records: dict, config) -> list:
	"""Pack records into JSON-safe dicts for workeroutput transport."""
	out = []
	for nodeid, record in records.items():
		attempts = []
		for attempt in record.attempts:
			serialized_reports = []
			for report in attempt.reports:
				try:
					data = config.hook.pytest_report_to_serializable(
						config=config, report=report
					)
					serialized_reports.append(data)
				except Exception:
					pass
			attempts.append({
				"attempt": attempt.attempt,
				"reports": serialized_reports,
			})
		out.append({"nodeid": nodeid, "attempts": attempts})
	return out


def _deserialize_records(data: list, config) -> dict:
	"""Unpack records received from a worker."""
	records = {}
	for rec_data in data:
		nodeid = rec_data["nodeid"]
		record = TestFlakeRecord(nodeid=nodeid)
		for att_data in rec_data["attempts"]:
			reports = []
			for report_data in att_data["reports"]:
				try:
					report = config.hook.pytest_report_from_serializable(
						config=config, data=report_data
					)
					if report:
						reports.append(report)
				except Exception:
					pass
			record.attempts.append(AttemptResult(
				attempt=att_data["attempt"],
				reports=reports,
			))
		records[nodeid] = record
	return records