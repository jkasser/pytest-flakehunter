"""
Generates a self-contained HTML report with:
  - Summary dashboard
  - Per-test attempt timeline bars
  - Duration scatter plots (attempt × duration, colored by outcome)
  - Failure heatmap (code location × attempt)
  - Stack trace clusters
  - AI root cause hypotheses
"""

from __future__ import annotations
import json
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest_flakehunter.plugin import TestFlakeRecord

from pytest_flakehunter.plugin import extract_frames, extract_error, short_path


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def generate_report(
    records: list["TestFlakeRecord"],
    runs: int,
    report_path: str,
    use_ai: bool = False,
    history_dir: str = None,
) -> None:
    # Load historical summaries and raw rows
    history_summaries: dict[str, dict] = {}
    history_rows_map: dict[str, list] = {}
    if history_dir:
        try:
            from pytest_flakehunter.history import load_history, summarize_history
            for r in records:
                rows = load_history(r.nodeid, history_dir=history_dir)
                if rows:
                    history_summaries[r.nodeid] = summarize_history(rows)
                    history_rows_map[r.nodeid] = rows
        except Exception as exc:
            print(f"\n⚠ FlakeHunter: could not load history: {exc}")

    ai_analyses: dict[str, str] = {}
    if use_ai:
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("\n[flakehunter] ⚠ --flakehunter-ai was set but ANTHROPIC_API_KEY is not in the environment. AI analysis skipped.")
            use_ai = False
        else:
            from pytest_flakehunter.ai_analysis import analyze_flaky_test
            for r in records:
                if r.flake_rate > 0 or history_summaries.get(r.nodeid):
                    ai_analyses[r.nodeid] = analyze_flaky_test(
                        r,
                        history_summary=history_summaries.get(r.nodeid),
                    )

    html = _build_html(records, runs, ai_analyses, history_summaries, use_ai=use_ai, history_rows_map=history_rows_map)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)


# ──────────────────────────────────────────────
# SVG builders
# ──────────────────────────────────────────────

def _scatter_svg(record: "TestFlakeRecord") -> str:
    """Duration scatter: x=attempt, y=duration, color=outcome."""
    W, H = 340, 130
    PAD = dict(top=14, right=14, bottom=28, left=40)
    inner_w = W - PAD["left"] - PAD["right"]
    inner_h = H - PAD["top"] - PAD["bottom"]

    attempts = record.attempts
    if not attempts:
        return ""

    durations = [a.total_duration for a in attempts]
    max_d = max(durations) * 1.15 or 1.0
    n = len(attempts)

    def cx(i): return PAD["left"] + (i / max(n - 1, 1)) * inner_w
    def cy(d): return PAD["top"] + inner_h - (d / max_d) * inner_h

    lines = [
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:{H}px;display:block">',
        # grid lines
        *[f'<line x1="{PAD["left"]}" y1="{cy(v):.1f}" x2="{W - PAD["right"]}" '
          f'y2="{cy(v):.1f}" stroke="#2a2a3a" stroke-width="1"/>'
          for v in _nice_ticks(0, max_d, 4)],
        # y-axis labels
        *[f'<text x="{PAD["left"] - 5}" y="{cy(v):.1f}" text-anchor="end" '
          f'dominant-baseline="middle" fill="#8888aa" font-size="9" font-family="\'JetBrains Mono\',monospace">'
          f'{v:.2f}s</text>'
          for v in _nice_ticks(0, max_d, 4)],
        # connect dots with faint line
        '<polyline points="' +
        " ".join(f"{cx(i):.1f},{cy(d):.1f}" for i, d in enumerate(durations)) +
        f'" fill="none" stroke="#2a2a3a" stroke-width="1.5"/>',
    ]

    # dots
    for i, attempt in enumerate(attempts):
        color = "#ff4d6d" if attempt.outcome == "failed" else "#00e5a0"
        glow = f'filter="url(#glow_{record.name})"' if attempt.outcome == "failed" else ""
        lines.append(
            f'<circle cx="{cx(i):.1f}" cy="{cy(durations[i]):.1f}" r="4.5" '
            f'fill="{color}" {glow} opacity="0.92">'
            f'<title>Run {i+1}: {attempt.outcome} in {durations[i]:.3f}s</title>'
            f'</circle>'
        )

    # x-axis attempt labels (every other)
    for i in range(n):
        if i % max(1, n // 5) == 0 or i == n - 1:
            lines.append(
                f'<text x="{cx(i):.1f}" y="{H - 6}" text-anchor="middle" '
                f'fill="#7777aa" font-size="9" font-family="\'JetBrains Mono\',monospace">#{i+1}</text>'
            )

    # glow filter
    lines.insert(1,
        f'<defs><filter id="glow_{record.name}"><feGaussianBlur stdDeviation="2.5" result="blur"/>'
        f'<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>'
    )

    lines.append('</svg>')
    return "\n".join(lines)


def _heatmap_html(record: "TestFlakeRecord", history_rows: list[dict] = None) -> str:
    """
    Multi-dimensional failure commonality heatmap.
    Current run section: cols=failed attempts, rows=(duration bucket, traceback frames).
    Historical section (if rows provided): cols=runs, rows=(environment, branch, date).
    Cell intensity reflects how common that value is across failures.
    """
    failed_attempts = [a for a in record.attempts if a.outcome == "failed"]
    n_fail = len(failed_attempts)

    if not failed_attempts:
        return '<p style="color:#444;font-size:12px;margin:0;padding:12px">No failures recorded.</p>'

    out = []

    # ── inline helpers ────────────────────────────────────────────────────

    def _cell(color, title=""):
        tip = f' title="{_escape(title)}"' if title else ""
        return (
            f'<td style="padding:2px 3px;text-align:center">'
            f'<span{tip} style="display:inline-block;width:14px;height:14px;'
            f'border-radius:3px;background:{color}"></span></td>'
        )

    def _dim_td(name, first):
        return (
            f'<td style="padding:4px 8px;font-size:11px;color:#8888b8;'
            f'white-space:nowrap;font-style:italic;min-width:72px">{name if first else ""}</td>'
        )

    def _val_td(text, color="#c0c0e0", full_text=""):
        tip = f' title="{_escape(full_text)}"' if full_text and full_text != text else ""
        return (
            f'<td{tip} style="padding:4px 10px;font-size:11px;font-family:var(--mono);'
            f'color:{color};white-space:nowrap;cursor:{"help" if tip else "default"}">'
            f'{_escape(text)}</td>'
        )

    def _section_hdr(label):
        return (
            f'<div style="font-size:11px;font-family:var(--mono);color:#9090c0;'
            f'font-weight:600;letter-spacing:1.5px;margin-bottom:8px;margin-top:4px;'
            f'padding-bottom:5px;border-bottom:1px solid #2a2a3e">{label}</div>'
        )

    def _table(col_headers_html, tbody_rows):
        return (
            f'<table style="border-collapse:collapse;width:100%;font-family:var(--mono)">'
            f'<thead><tr>'
            f'<th style="padding:4px 8px;font-size:11px;color:#7070a8;text-align:left;min-width:72px">Dim</th>'
            f'<th style="padding:4px 10px;font-size:11px;color:#7070a8;text-align:left">Value</th>'
            f'{col_headers_html}'
            f'</tr></thead>'
            f'<tbody>{"".join(tbody_rows)}</tbody>'
            f'</table>'
        )

    MISS = "#151520"
    MISS_HIST = "#0a0a12"

    # ── Section 1: Current run ────────────────────────────────────────────

    all_durations = [a.total_duration for a in record.attempts]
    sd = sorted(all_durations)
    median_d = sd[len(sd) // 2]
    p75_d = sd[min(int(len(sd) * 0.75), len(sd) - 1)]

    def _dur_bucket(d):
        if d >= p75_d:
            return "slow"
        if d >= median_d:
            return "med"
        return "fast"

    feat_dur = []
    feat_frames: list[list[str]] = []
    frame_order: list[str] = []
    seen_frames: set[str] = set()

    for a in failed_attempts:
        feat_dur.append(_dur_bucket(a.total_duration))
        fr = a.failed_report
        frames = extract_frames(fr.longrepr) if fr else []
        frame_keys = [f"{short_path(f[0])}:{f[2]}()" for f in frames]
        feat_frames.append(frame_keys)
        for fk in frame_keys:
            if fk not in seen_frames:
                frame_order.append(fk)
                seen_frames.add(fk)

    col_headers = "".join(
        f'<th style="padding:4px 5px;font-size:11px;color:#8888b8;text-align:center">#{a.attempt}</th>'
        for a in failed_attempts
    )

    tbody: list[str] = []

    # Duration rows
    DUR_COLOR = {"slow": "#ff4d6d", "med": "#ffb347", "fast": "#00e5a0"}
    first_dur = True
    for bucket in ("slow", "med", "fast"):
        count = sum(1 for d in feat_dur if d == bucket)
        if count == 0:
            continue
        pct = count / n_fail
        cells = "".join(
            _cell(DUR_COLOR[bucket], f"#{a.attempt}: {a.total_duration:.3f}s")
            if feat_dur[i] == bucket else _cell(MISS)
            for i, a in enumerate(failed_attempts)
        )
        tbody.append(
            f'<tr>{_dim_td("duration", first_dur)}'
            f'{_val_td(bucket, DUR_COLOR[bucket])}'
            f'{cells}</tr>'
        )
        first_dur = False

    # Traceback frame rows
    frame_counts = {fk: sum(1 for fs in feat_frames if fk in fs) for fk in frame_order}
    first_tb = True
    for fk in frame_order:
        count = frame_counts[fk]
        pct = count / n_fail
        fc = "#ff4d6d" if pct >= 0.8 else "#cc3355" if pct >= 0.5 else "#882233" if pct >= 0.25 else "#441122"
        cells = "".join(
            _cell(fc, f"#{a.attempt}: {fk}")
            if fk in feat_frames[i] else _cell(MISS)
            for i, a in enumerate(failed_attempts)
        )
        tbody.append(
            f'<tr>{_dim_td("traceback", first_tb)}'
            f'{_val_td(_truncate(fk, 55), full_text=fk if len(fk) > 55 else "")}'
            f'{cells}</tr>'
        )
        first_tb = False

    out.append(_section_hdr(f"Current Run \u2014 {n_fail} failure{'s' if n_fail != 1 else ''}"))
    out.append(_table(col_headers, tbody))

    # ── Section 2: Historical ─────────────────────────────────────────────
    if history_rows:
        runs_data: dict[str, dict] = {}
        for row in history_rows:
            rid = row.get("run_id", "")
            if not rid:
                continue
            if rid not in runs_data:
                runs_data[rid] = {
                    "ts": row.get("timestamp_utc", ""),
                    "hostname": row.get("hostname", ""),
                    "branch": row.get("git_branch", ""),
                    "total": 0,
                    "failed": 0,
                }
            runs_data[rid]["total"] += 1
            if row.get("outcome") == "failed":
                runs_data[rid]["failed"] += 1

        sorted_runs = sorted(runs_data.items(), key=lambda x: x[1]["ts"])
        n_runs = len(sorted_runs)

        if n_runs >= 2:
            hist_col_headers = "".join(
                f'<th style="padding:4px 5px;font-size:11px;color:#8888b8;text-align:center">'
                f'{v["ts"][5:10]}</th>'
                for _, v in sorted_runs
            )

            def _week_of(ts):
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    return dt.strftime("%Y-W%W")
                except Exception:
                    return ts[:7] if ts else "?"

            def _hist_dim_rows(dim_name, values, val_fn, color_fn):
                rows = []
                first = True
                for val in values:
                    cells = "".join(
                        _cell(color_fn(val_fn(v), v["failed"] / v["total"] if v["total"] else 0),
                              f'{v["ts"][:10]}: {v["failed"]}/{v["total"]} fail')
                        if val_fn(v) == val else _cell(MISS_HIST)
                        for _, v in sorted_runs
                    )
                    rows.append(
                        f'<tr>{_dim_td(dim_name, first)}'
                        f'{_val_td(_truncate(val, 40), full_text=val if len(val) > 40 else "")}'
                        f'{cells}</tr>'
                    )
                    first = False
                return rows

            hist_tbody: list[str] = []

            # Environment
            unique_hosts = sorted({v["hostname"] for _, v in sorted_runs if v["hostname"]})[:5]
            if unique_hosts:
                hist_tbody.extend(_hist_dim_rows(
                    "env", unique_hosts,
                    lambda v: v["hostname"],
                    lambda val, rate: f"rgba(255,77,109,{max(0.2, rate):.2f})" if rate > 0 else MISS_HIST,
                ))

            # Branch
            unique_branches = sorted({v["branch"] for _, v in sorted_runs if v["branch"]})[:4]
            if unique_branches:
                hist_tbody.extend(_hist_dim_rows(
                    "branch", unique_branches,
                    lambda v: v["branch"],
                    lambda val, rate: f"rgba(124,106,247,{max(0.2, rate):.2f})" if rate > 0 else MISS_HIST,
                ))

            # Date (weekly)
            unique_weeks = sorted({_week_of(v["ts"]) for _, v in sorted_runs if v["ts"]})
            if len(unique_weeks) > 1:
                hist_tbody.extend(_hist_dim_rows(
                    "date", unique_weeks[:8],
                    lambda v: _week_of(v["ts"]),
                    lambda val, rate: f"rgba(255,179,71,{max(0.2, rate):.2f})" if rate > 0 else MISS_HIST,
                ))

            if hist_tbody:
                out.append(_section_hdr(f"Historical \u2014 {n_runs} runs"))
                out.append(_table(hist_col_headers, hist_tbody))

    return "\n".join(out)


# ──────────────────────────────────────────────
# HTML assembly
# ──────────────────────────────────────────────

def _build_html(records: list["TestFlakeRecord"], runs: int, ai_analyses: dict, history_summaries: dict = None, use_ai: bool = False, history_rows_map: dict = None) -> str:
    if history_summaries is None:
        history_summaries = {}
    if history_rows_map is None:
        history_rows_map = {}
    total = len(records)
    flaky = sum(1 for r in records if r.flake_rate > 0)
    overall_rate = (sum(r.flake_rate for r in records) / total * 100) if total else 0
    worst = max(records, key=lambda r: r.flake_rate) if records else None
    cards = "\n".join(_test_card(r, runs, ai_analyses.get(r.nodeid, ""), history_summaries.get(r.nodeid, {}), use_ai=use_ai, history_rows=history_rows_map.get(r.nodeid)) for r in records)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlakeHunter Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&family=Space+Grotesk:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --bg:        #0d0d14;
  --surface:   #13131e;
  --surface2:  #1a1a28;
  --border:    #252535;
  --accent:    #7c6af7;
  --accent2:   #00e5a0;
  --danger:    #ff4d6d;
  --warning:   #ffb347;
  --text:      #d8d8f0;
  --muted:     #8888aa;
  --mono:      'JetBrains Mono', monospace;
  --sans:      'Space Grotesk', sans-serif;
}}

body {{
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 15px;
  min-height: 100vh;
  padding: 0;
}}

/* ── Header ── */
.header {{
  background: linear-gradient(135deg, #0d0d14 0%, #161626 60%, #1a1030 100%);
  border-bottom: 1px solid var(--border);
  padding: 40px 48px 32px;
  position: relative;
  overflow: hidden;
}}
.header::before {{
  content: '';
  position: absolute;
  top: -60px; right: -80px;
  width: 400px; height: 400px;
  background: radial-gradient(circle, rgba(124,106,247,0.12) 0%, transparent 70%);
  pointer-events: none;
}}
.header-top {{ display: flex; align-items: baseline; gap: 16px; }}
.logo {{
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  letter-spacing: 4px;
  text-transform: uppercase;
  opacity: 0.7;
}}
.title {{
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.5px;
  color: #fff;
}}
.title span {{ color: var(--accent); }}
.subtitle {{
  margin-top: 6px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
}}

/* ── Stats bar ── */
.stats-bar {{
  display: flex;
  gap: 2px;
  margin-top: 32px;
}}
.stat {{
  flex: 1;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px 20px;
  position: relative;
  overflow: hidden;
}}
.stat::after {{
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
}}
.stat.blue::after   {{ background: var(--accent); }}
.stat.green::after  {{ background: var(--accent2); }}
.stat.red::after    {{ background: var(--danger); }}
.stat.orange::after {{ background: var(--warning); }}
.stat-val {{
  font-family: var(--mono);
  font-size: 28px;
  font-weight: 700;
  line-height: 1;
  color: #fff;
}}
.stat-label {{
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-top: 6px;
}}
.stat-sub {{
  font-family: var(--mono);
  font-size: 11px;
  color: var(--muted);
  margin-top: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}

/* ── Main layout ── */
.main {{ padding: 32px 48px; max-width: 1600px; margin: 0 auto; width: 100%; }}

/* ── Section header ── */
.section-label {{
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 3px;
  text-transform: uppercase;
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}}

/* ── Test cards ── */
.cards {{ display: flex; flex-direction: column; gap: 16px; }}

.card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  transition: border-color 0.2s;
}}
.card:hover {{ border-color: #333348; }}
.card.is-stable {{ opacity: 0.7; }}

.card-header {{
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 18px 24px;
  cursor: pointer;
  user-select: none;
}}
.card-header:hover {{ background: rgba(255,255,255,0.015); }}

.flake-badge {{
  font-family: var(--mono);
  font-size: 15px;
  font-weight: 700;
  padding: 6px 14px;
  border-radius: 6px;
  min-width: 68px;
  text-align: center;
  flex-shrink: 0;
}}
.flake-badge.high    {{ background: rgba(255,77,109,0.2); color: #ff4d6d; border: 1px solid rgba(255,77,109,0.3); }}
.flake-badge.medium  {{ background: rgba(255,179,71,0.15); color: #ffb347; border: 1px solid rgba(255,179,71,0.25); }}
.flake-badge.low     {{ background: rgba(255,220,100,0.1); color: #ffd966; border: 1px solid rgba(255,220,100,0.2); }}
.flake-badge.none    {{ background: rgba(0,229,160,0.1); color: var(--accent2); border: 1px solid rgba(0,229,160,0.2); }}

.card-name {{
  flex: 1;
  font-family: var(--mono);
  font-size: 16px;
  font-weight: 600;
  color: #e0e0f5;
}}
.card-file {{
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
  margin-top: 4px;
}}

/* Run timeline dots */
.run-dots {{
  display: flex;
  gap: 3px;
  flex-shrink: 0;
}}
.dot {{
  width: 10px; height: 10px;
  border-radius: 50%;
}}
.dot.pass {{ background: var(--accent2); opacity: 0.8; }}
.dot.fail {{ background: var(--danger); }}
.dot.skip {{ background: var(--border); }}

.chevron {{
  color: var(--muted);
  font-size: 12px;
  transition: transform 0.2s;
  flex-shrink: 0;
}}
.card.open .chevron {{ transform: rotate(90deg); }}

/* Card body */
.card-body {{
  display: none;
  padding: 0 24px 24px;
  border-top: 1px solid var(--border);
}}
.card.open .card-body {{ display: block; }}

.card-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-top: 24px;
}}

.panel-title {{
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: 12px;
}}

/* Clusters */
.cluster {{
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px 14px;
  margin-bottom: 10px;
  border-left: 3px solid var(--danger);
}}
.cluster-id {{
  font-family: var(--mono);
  font-size: 10px;
  color: var(--muted);
}}
.cluster-error {{
  font-family: var(--mono);
  font-size: 13px;
  color: var(--danger);
  margin: 8px 0;
  word-break: break-all;
}}
.cluster-frames {{
  font-family: var(--mono);
  font-size: 12px;
  color: #8888aa;
  line-height: 1.7;
}}
.cluster-frames .hit {{
  color: #c0c0e0;
}}

/* AI analysis */
.ai-panel {{
  background: linear-gradient(135deg, rgba(124,106,247,0.06), rgba(0,229,160,0.04));
  border: 1px solid rgba(124,106,247,0.2);
  border-radius: 6px;
  padding: 14px 16px;
  margin-top: 16px;
}}
.ai-label {{
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: 10px;
}}
.ai-text {{
  font-size: 14px;
  color: #c0c0e0;
  line-height: 1.7;
}}

/* Heatmap scroll wrapper */
.heatmap-wrap {{
  overflow-x: auto;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
}}

/* Attempt table */
.attempt-table {{
  width: 100%;
  border-collapse: collapse;
  font-family: var(--mono);
  font-size: 12px;
  margin-top: 4px;
}}
.attempt-table th {{
  text-align: left;
  color: var(--muted);
  font-weight: 600;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}}
.attempt-table td {{
  padding: 6px 12px;
  border-bottom: 1px solid rgba(255,255,255,0.03);
  color: #c0c0e0;
  vertical-align: top;
}}
.attempt-table tr.fail-row td {{ color: #e0c0c8; }}
.attempt-table tr:last-child td {{ border-bottom: none; }}
.outcome-pass {{ color: var(--accent2) !important; font-weight: 600; }}
.outcome-fail {{ color: var(--danger) !important; font-weight: 600; }}

/* Full span panels */
.full-span {{ grid-column: 1 / -1; }}
</style>
</head>
<body>

<div class="header">
  <div class="header-top">
    <span class="logo">pytest</span>
    <h1 class="title">Flake<span>Hunter</span></h1>
  </div>
  <p class="subtitle">// {runs} runs × {total} tests &nbsp;·&nbsp; generated at runtime</p>

  <div class="stats-bar">
    <div class="stat blue">
      <div class="stat-val">{total}</div>
      <div class="stat-label">Tests Analyzed</div>
      <div class="stat-sub">{runs} runs each</div>
    </div>
    <div class="stat red">
      <div class="stat-val">{flaky}</div>
      <div class="stat-label">Flaky Tests</div>
      <div class="stat-sub">{total - flaky} fully stable</div>
    </div>
    <div class="stat orange">
      <div class="stat-val">{overall_rate:.1f}%</div>
      <div class="stat-label">Avg Flake Rate</div>
      <div class="stat-sub">across all tests</div>
    </div>
    <div class="stat green">
      <div class="stat-val">{worst.name[:18] + "…" if worst and len(worst.name) > 18 else (worst.name if worst else "—")}</div>
      <div class="stat-label">Worst Offender</div>
      <div class="stat-sub">{f"{worst.flake_rate:.0%} flake rate" if worst else ""}</div>
    </div>
  </div>
</div>

<div class="main">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid var(--border)">
    <span class="section-label" style="margin:0;border:none;padding:0">Test Results — click to expand</span>
    <button id="toggle-stable" onclick="toggleStable()"
      style="font-family:var(--mono);font-size:12px;font-weight:600;
             background:rgba(255,255,255,0.04);border:1px solid var(--border);
             color:var(--muted);border-radius:6px;padding:6px 14px;cursor:pointer;
             transition:all 0.15s">
      Hide passing tests
    </button>
  </div>
  <div class="cards">
    {cards}
  </div>
</div>

<script>
document.querySelectorAll('.card-header').forEach(h => {{
  h.addEventListener('click', () => {{
    const card = h.closest('.card');
    card.classList.toggle('open');
  }});
}});

// Auto-open flaky tests
document.querySelectorAll('.card:not(.is-stable)').forEach(c => c.classList.add('open'));

var stableHidden = false;
function toggleStable() {{
  stableHidden = !stableHidden;
  document.querySelectorAll('.card.is-stable').forEach(c => {{
    c.style.display = stableHidden ? 'none' : '';
  }});
  var btn = document.getElementById('toggle-stable');
  btn.textContent = stableHidden ? 'Show passing tests' : 'Hide passing tests';
  btn.style.color = stableHidden ? 'var(--accent2)' : 'var(--muted)';
  btn.style.borderColor = stableHidden ? 'rgba(0,229,160,0.3)' : 'var(--border)';
}}
</script>
</body>
</html>"""


def _test_card(record: "TestFlakeRecord", runs: int, ai_text: str, history_summary: dict = None, use_ai: bool = False, history_rows: list = None) -> str:
    if history_summary is None:
        history_summary = {}
    rate = record.flake_rate
    badge_class = "high" if rate >= 0.4 else "medium" if rate >= 0.2 else "low" if rate > 0 else "none"
    stable_class = " is-stable" if rate == 0 else ""

    dots = "".join(
        f'<div class="dot {"fail" if a.outcome == "failed" else "pass"}" '
        f'title="Run #{a.attempt}: {a.outcome} ({a.total_duration:.2f}s)"></div>'
        for a in record.attempts
    )

    durations = [a.total_duration for a in record.attempts]
    avg_d = sum(durations) / len(durations) if durations else 0

    scatter = _scatter_svg(record)
    heatmap = _heatmap_html(record, history_rows=history_rows)
    clusters_html = _clusters_html(record)
    attempts_html = _attempts_table(record)
    if ai_text:
        ai_html = (
            f'<div class="ai-panel full-span">'
            f'<div class="ai-label">&#x26A1; AI Root Cause Hypothesis</div>'
            f'<div class="ai-text">{_escape(ai_text)}</div>'
            f'</div>'
        )
    elif use_ai:
        ai_html = (
            f'<div class="ai-panel full-span" style="opacity:0.5">'
            f'<div class="ai-label">&#x26A1; AI Root Cause Hypothesis</div>'
            f'<div class="ai-text" style="color:var(--muted);font-style:italic">'
            f'No AI analysis for this test — it had no failures this run and no prior history.'
            f'</div>'
            f'</div>'
        )
    else:
        ai_html = (
            f'<div class="ai-panel full-span" style="opacity:0.45">'
            f'<div class="ai-label">&#x26A1; AI Root Cause Hypothesis</div>'
            f'<div class="ai-text" style="color:var(--muted);font-style:italic">'
            f'Not enabled. Re-run with <code style="font-family:var(--mono);font-size:13px;'
            f'background:rgba(255,255,255,0.05);padding:1px 6px;border-radius:4px">'
            f'--fh-ai</code> and set <code style="font-family:var(--mono);font-size:13px;'
            f'background:rgba(255,255,255,0.05);padding:1px 6px;border-radius:4px">'
            f'ANTHROPIC_API_KEY</code> to get AI-powered root cause analysis.'
            f'</div>'
            f'</div>'
        )

    history_html = _history_panel_html(history_summary, history_rows=history_rows) if history_summary else ""

    return f"""
<div class="card{stable_class}">
  <div class="card-header">
    <span class="flake-badge {badge_class}">{rate:.0%}</span>
    <div>
      <div class="card-name">{_escape(record.name)}</div>
      <div class="card-file">{_escape(record.file)}</div>
    </div>
    <div style="flex:1"></div>
    <div style="font-family:var(--mono);font-size:13px;color:var(--muted);margin-right:16px;text-align:right">
      {record.pass_count}✓ &nbsp; {record.fail_count}✗ &nbsp; avg {avg_d:.2f}s
    </div>
    <div class="run-dots">{dots}</div>
    <span class="chevron">▶</span>
  </div>
  <div class="card-body">
    <div class="card-grid">
      <div>
        <div class="panel-title">Duration per attempt</div>
        {scatter}
      </div>
      <div>
        <div class="panel-title">Attempt log</div>
        {attempts_html}
      </div>
      <div class="full-span">
        <div class="panel-title">Failure clusters</div>
        {clusters_html}
      </div>
      <div class="full-span">
        <div class="panel-title">Failure commonality heatmap</div>
        <div class="heatmap-wrap">{heatmap}</div>
      </div>
      {history_html}
      {ai_html}
    </div>
  </div>
</div>"""


def _clusters_html(record: "TestFlakeRecord") -> str:
    clusters = record.failure_clusters()
    if not clusters:
        return '<p style="color:var(--accent2);font-size:12px;font-family:var(--mono)">✓ No failures — test is stable</p>'

    out = []
    for fp, attempts in sorted(clusters.items(), key=lambda x: -len(x[1])):
        fr = attempts[0].failed_report
        if not fr:
            continue
        error_type, error_msg = extract_error(fr.longrepr)
        frames = extract_frames(fr.longrepr)[-5:]
        frames_html = ""
        for i, (filename, lineno, func, ctx) in enumerate(frames):
            is_failure = i == len(frames) - 1
            loc_style = 'color:var(--danger);font-weight:600' if is_failure else ''
            marker = ' <span style="color:var(--danger);font-size:10px">← raised here</span>' if is_failure else ''
            ctx_style = 'color:#cc6677;padding-left:16px;font-weight:600' if is_failure else 'color:#8888aa;padding-left:16px'
            frames_html += (
                f'<div class="hit" style="{loc_style}">'
                f'{_escape(short_path(filename))}:{lineno} '
                f'<span style="color:{"var(--danger)" if is_failure else "var(--accent)"}">in {_escape(func)}()</span>'
                f'{marker}'
                f'</div>'
                f'<div style="{ctx_style}">{_escape(ctx)}</div>'
            )
        out.append(f"""
<div class="cluster">
  <div class="cluster-id">fingerprint #{fp} — {len(attempts)} occurrence{'s' if len(attempts)>1 else ''}</div>
  <div class="cluster-error">{_escape(error_type)}: {_escape(error_msg[:120])}</div>
  <div class="cluster-frames">{frames_html}</div>
</div>""")

    return "\n".join(out)


def _attempts_table(record: "TestFlakeRecord") -> str:
    rows = []
    for a in record.attempts:
        fr = a.failed_report
        outcome_class = "outcome-fail" if a.outcome == "failed" else "outcome-pass"
        row_class = " class=\"fail-row\"" if a.outcome == "failed" else ""
        where = "—"
        if fr:
            frames = extract_frames(fr.longrepr)
            if frames:
                where = f"{frames[-1][2]}():{frames[-1][1]}"

        phases = " | ".join(
            f'<span style="color:{_phase_color(r.outcome)}">'
            f'{r.when[0].upper()}: {r.duration:.3f}s</span>'
            for r in a.reports if r.outcome != "skipped"
        )

        rows.append(
            f'<tr{row_class}>'
            f'<td style="color:var(--muted)">#{a.attempt}</td>'
            f'<td class="{outcome_class}">{a.outcome}</td>'
            f'<td>{a.total_duration:.3f}s</td>'
            f'<td style="font-size:10px">{phases}</td>'
            f'<td style="font-size:10px;color:#8888aa">{_escape(where)}</td>'
            f'</tr>'
        )

    return (
        '<table class="attempt-table">'
        '<thead><tr><th>#</th><th>Outcome</th><th>Total</th><th>Phase durations</th><th>Failed at</th></tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody>'
        '</table>'
    )


def _phase_color(outcome: str) -> str:
    return {"passed": "#00e5a0", "failed": "#ff4d6d", "error": "#ff4d6d", "skipped": "#555"}.get(outcome, "#888")


def _nice_ticks(lo: float, hi: float, n: int) -> list[float]:
    if hi == lo:
        return [lo]
    step = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(step)) if step > 0 else 1
    step = round(step / mag) * mag or mag
    ticks = []
    v = math.floor(lo / step) * step
    while v <= hi + step * 0.01:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _escape(s: str) -> str:
    return (s
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ──────────────────────────────────────────────
# History integration helpers (added for persistent CSV support)
# ──────────────────────────────────────────────

def _trend_sparkline(run_flake_rates: list[dict]) -> str:
    """Render a tiny inline SVG sparkline of flake rate over time."""
    if len(run_flake_rates) < 2:
        return ""
    rates = [r["flake_rate"] for r in run_flake_rates]
    W, H = 120, 28
    max_r = max(rates) or 1.0
    n = len(rates)

    def cx(i): return (i / max(n - 1, 1)) * W
    def cy(v): return H - (v / max_r) * H * 0.85 - 2

    pts = " ".join(f"{cx(i):.1f},{cy(v):.1f}" for i, v in enumerate(rates))
    last_color = "#ff4d6d" if rates[-1] > 0.1 else "#00e5a0"

    return (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:{W}px;height:{H}px;vertical-align:middle;margin-left:8px">'
        f'<polyline points="{pts}" fill="none" stroke="#333348" stroke-width="1.5"/>'
        f'<circle cx="{cx(n-1):.1f}" cy="{cy(rates[-1]):.1f}" r="3" fill="{last_color}"/>'
        f'</svg>'
    )


def _history_panel_html(summary: dict, history_rows: list = None) -> str:
    """Render a history panel showing per-run detail and cross-dimensional breakdowns."""
    if not summary or summary.get("total_attempts", 0) == 0:
        return ""

    hs = summary
    total = hs.get("total_attempts", 0)
    rate = hs.get("overall_flake_rate", 0)
    avg_d = hs.get("avg_duration_s", 0)
    p95_d = hs.get("p95_duration_s", 0)
    date_range = hs.get("date_range", ("", ""))

    # Trend arrow
    run_rates = hs.get("run_flake_rates", [])
    trend_html = ""
    if len(run_rates) >= 3:
        recent = sum(r["flake_rate"] for r in run_rates[-3:]) / 3
        early  = sum(r["flake_rate"] for r in run_rates[:3]) / 3
        delta  = recent - early
        if delta > 0.05:
            trend_html = f'<span style="color:#ff4d6d;font-size:11px">▲ getting worse ({delta:+.0%})</span>'
        elif delta < -0.05:
            trend_html = f'<span style="color:#00e5a0;font-size:11px">▼ improving ({delta:+.0%})</span>'
        else:
            trend_html = f'<span style="color:#aaaacc;font-size:11px">→ stable</span>'

    sparkline = _trend_sparkline(run_rates)
    date_str = f'{date_range[0][:10]} → {date_range[1][:10]}' if date_range[0] else ""

    # ── Per-run detail table (built from raw rows) ─────────────────────────────
    per_run_html = ""
    if history_rows:
        # Group rows by run_id, collect per-run metadata
        runs: dict[str, dict] = {}
        for row in history_rows:
            rid = row.get("run_id", "")
            if not rid:
                continue
            if rid not in runs:
                runs[rid] = {
                    "ts": row.get("timestamp_utc", ""),
                    "host": row.get("hostname", ""),
                    "branch": row.get("git_branch", "") or "—",
                    "commit": row.get("git_commit", "") or "—",
                    "total": 0, "failed": 0,
                }
            runs[rid]["total"] += 1
            if row.get("outcome") == "failed":
                runs[rid]["failed"] += 1

        # Sort by timestamp, most recent first, cap at 20 rows
        sorted_runs = sorted(runs.values(), key=lambda x: x["ts"], reverse=True)[:20]

        TH = "style='text-align:left;color:#8888aa;padding:4px 10px;border-bottom:1px solid var(--border);white-space:nowrap'"
        TD = "style='padding:4px 10px;border-bottom:1px solid rgba(255,255,255,0.04);white-space:nowrap'"

        run_rows = ""
        for rv in sorted_runs:
            fr = rv["failed"] / rv["total"] if rv["total"] else 0
            fc = "#ff4d6d" if fr > 0.2 else "#ffb347" if fr > 0 else "#00e5a0"
            date_short = rv["ts"][:16].replace("T", " ") if rv["ts"] else "—"
            run_rows += (
                f'<tr>'
                f'<td {TD} style="padding:4px 10px;border-bottom:1px solid rgba(255,255,255,0.04);'
                f'color:#8888aa;font-size:10px">{_escape(date_short)}</td>'
                f'<td {TD} style="padding:4px 10px;border-bottom:1px solid rgba(255,255,255,0.04)">'
                f'{_escape(rv["host"])}</td>'
                f'<td {TD} style="padding:4px 10px;border-bottom:1px solid rgba(255,255,255,0.04);'
                f'color:#9090cc">{_escape(rv["branch"])}</td>'
                f'<td {TD} style="padding:4px 10px;border-bottom:1px solid rgba(255,255,255,0.04);'
                f'color:#7070aa;font-size:10px">{_escape(rv["commit"])}</td>'
                f'<td {TD} style="padding:4px 10px;border-bottom:1px solid rgba(255,255,255,0.04);'
                f'color:{fc};font-weight:600;text-align:right">{fr:.0%}</td>'
                f'<td {TD} style="padding:4px 10px;border-bottom:1px solid rgba(255,255,255,0.04);'
                f'color:#aaaacc;text-align:right">{rv["failed"]}/{rv["total"]}</td>'
                f'</tr>'
            )

        per_run_html = f"""
<div style="margin-top:14px">
  <div style="font-family:var(--mono);font-size:9px;color:var(--muted);text-transform:uppercase;
  letter-spacing:1.5px;margin-bottom:6px">Per-Run Breakdown (most recent first)</div>
  <div style="overflow-x:auto">
    <table style="font-family:var(--mono);font-size:11px;border-collapse:collapse;width:100%;min-width:500px">
      <tr>
        <th {TH}>Date (UTC)</th>
        <th {TH}>Host</th>
        <th {TH}>Branch</th>
        <th {TH}>Commit</th>
        <th {TH} style="text-align:right;color:#8888aa;padding:4px 10px;border-bottom:1px solid var(--border)">Flake%</th>
        <th {TH} style="text-align:right;color:#8888aa;padding:4px 10px;border-bottom:1px solid var(--border)">Fail/Total</th>
      </tr>
      {run_rows}
    </table>
  </div>
</div>"""

    # ── Dimension breakdowns (host + branch side by side) ──────────────────────
    TH2 = "style='text-align:left;color:#8888aa;padding:3px 8px'"
    TD2 = "style='padding:3px 8px'"

    env_rows = ""
    env = hs.get("env_breakdown", {})
    for host, v in sorted(env.items(), key=lambda x: -x[1]["failed"]):
        hr = v["failed"] / v["total"] if v["total"] else 0
        color = "#ff4d6d" if hr > 0.2 else "#00e5a0" if hr == 0 else "#ffb347"
        env_rows += (
            f'<tr><td {TD2} style="padding:3px 8px;color:#aaaacc">{_escape(host)}</td>'
            f'<td {TD2} style="padding:3px 8px;color:{color};font-weight:600">{hr:.0%}</td>'
            f'<td {TD2} style="padding:3px 8px;color:#aaaacc">{v["failed"]}/{v["total"]}</td></tr>'
        )

    branch_stats: dict[str, dict] = {}
    if history_rows:
        for row in history_rows:
            br = row.get("git_branch", "") or "—"
            branch_stats.setdefault(br, {"total": 0, "failed": 0})
            branch_stats[br]["total"] += 1
            if row.get("outcome") == "failed":
                branch_stats[br]["failed"] += 1
    branch_rows = ""
    for br, v in sorted(branch_stats.items(), key=lambda x: -x[1]["failed"]):
        hr = v["failed"] / v["total"] if v["total"] else 0
        color = "#ff4d6d" if hr > 0.2 else "#00e5a0" if hr == 0 else "#ffb347"
        branch_rows += (
            f'<tr><td {TD2} style="padding:3px 8px;color:#9090cc">{_escape(br)}</td>'
            f'<td {TD2} style="padding:3px 8px;color:{color};font-weight:600">{hr:.0%}</td>'
            f'<td {TD2} style="padding:3px 8px;color:#aaaacc">{v["failed"]}/{v["total"]}</td></tr>'
        )

    arg_rows = ""
    arg_corr = hs.get("arg_correlation", {})
    high = sorted(
        [(k, v) for k, v in arg_corr.items() if v["total"] >= 2],
        key=lambda x: -x[1]["rate"]
    )[:5]
    for combo, v in high:
        color = "#ff4d6d" if v["rate"] > 0.3 else "#ffb347" if v["rate"] > 0.1 else "#00e5a0"
        arg_rows += (
            f'<tr><td {TD2} style="padding:3px 8px;color:#8888aa;font-size:10px">{_escape(combo[:40])}</td>'
            f'<td {TD2} style="padding:3px 8px;color:{color};font-weight:600">{v["rate"]:.0%}</td>'
            f'<td {TD2} style="padding:3px 8px;color:#aaaacc">{v["failed"]}/{v["total"]}</td></tr>'
        )

    def _dim_table(label: str, header: str, rows: str) -> str:
        if not rows:
            return ""
        return (
            f'<div><div style="font-family:var(--mono);font-size:9px;color:var(--muted);'
            f'text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px">{label}</div>'
            f'<table style="font-family:var(--mono);font-size:11px;border-collapse:collapse;width:100%">'
            f'<tr><th {TH2}>{header}</th>'
            f'<th {TH2}>Flake%</th><th {TH2}>Fail/Total</th></tr>'
            f'{rows}</table></div>'
        )

    dim_cols = [
        _dim_table("By Host", "Host", env_rows),
        _dim_table("By Branch", "Branch", branch_rows),
        _dim_table("By Params", "Params", arg_rows),
    ]
    dim_cols = [c for c in dim_cols if c]
    dim_grid = ""
    if dim_cols:
        cols = len(dim_cols)
        dim_grid = (
            f'<div style="display:grid;grid-template-columns:repeat({cols},1fr);gap:16px;margin-top:14px">'
            + "".join(dim_cols)
            + "</div>"
        )

    return f"""<div class="full-span" style="background:var(--surface2);border:1px solid var(--border);
border-radius:6px;padding:14px 16px;border-left:3px solid #7c6af7">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
    <span style="font-family:var(--mono);font-size:10px;font-weight:600;color:#7c6af7;
    letter-spacing:2px;text-transform:uppercase">Historical Trends</span>
    <span style="font-family:var(--mono);font-size:10px;color:var(--muted)">{date_str}</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px">
    <div style="text-align:center">
      <div style="font-family:var(--mono);font-size:20px;font-weight:700;color:#fff">{total}</div>
      <div style="font-family:var(--mono);font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1px">Total Attempts</div>
    </div>
    <div style="text-align:center">
      <div style="font-family:var(--mono);font-size:20px;font-weight:700;color:{"#ff4d6d" if rate > 0.2 else "#ffb347" if rate > 0.05 else "#00e5a0"}">{rate:.0%}</div>
      <div style="font-family:var(--mono);font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1px">Hist. Flake Rate</div>
    </div>
    <div style="text-align:center">
      <div style="font-family:var(--mono);font-size:20px;font-weight:700;color:#fff">{avg_d:.3f}s</div>
      <div style="font-family:var(--mono);font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1px">Avg Duration</div>
    </div>
    <div style="text-align:center">
      <div style="font-family:var(--mono);font-size:20px;font-weight:700;color:#fff">{p95_d:.3f}s</div>
      <div style="font-family:var(--mono);font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1px">P95 Duration</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px">
    <span style="color:var(--muted)">Trend over {len(run_rates)} runs:</span>
    {sparkline}
    {trend_html}
  </div>
  {per_run_html}
  {dim_grid}
</div>"""
