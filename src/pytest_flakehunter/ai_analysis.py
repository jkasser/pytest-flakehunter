"""
AI-powered root cause analysis for flaky tests.
Calls the Anthropic API with clustered stack traces AND historical trend data
to generate richer, more specific hypotheses.
"""

from __future__ import annotations
import os
import json
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pytest_flakehunter.plugin import TestFlakeRecord

from pytest_flakehunter.plugin import extract_frames, extract_error, short_path


def analyze_flaky_test(
    record: "TestFlakeRecord",
    history_summary: Optional[dict] = None,
) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""

    try:
        import urllib.request
        import urllib.error

        clusters = record.failure_clusters()
        cluster_summaries = []
        for fp, attempts in clusters.items():
            fr = attempts[0].failed_report
            if not fr:
                continue
            error_type, error_msg = extract_error(fr.longrepr)
            frames = extract_frames(fr.longrepr)
            frames_text = "\n".join(
                f"  {short_path(f[0])}:{f[1]} in {f[2]}() -- {f[3]}"
                for f in frames[-5:]
            )
            durations = [a.total_duration for a in attempts]
            cluster_summaries.append(
                f"Cluster {fp} ({len(attempts)} occurrences, "
                f"avg {sum(durations)/len(durations):.2f}s):\n"
                f"Error: {error_type}: {error_msg}\n"
                f"Stack (innermost last):\n{frames_text}"
            )

        if not cluster_summaries and not history_summary:
            return ""

        history_section = ""
        if history_summary and history_summary.get("total_attempts", 0) > 0:
            hs = history_summary
            lines = [
                f"\nHISTORICAL DATA ({hs.get('date_range', ('?','?'))[0]} -> {hs.get('date_range', ('?','?'))[1]}):",
                f"  Total historical attempts: {hs.get('total_attempts')}",
                f"  Overall historical flake rate: {hs.get('overall_flake_rate', 0):.1%}",
                f"  Avg duration: {hs.get('avg_duration_s', 0):.3f}s  P95: {hs.get('p95_duration_s', 0):.3f}s",
            ]
            run_rates = hs.get("run_flake_rates", [])
            if len(run_rates) >= 3:
                recent_3 = [r["flake_rate"] for r in run_rates[-3:]]
                early_3  = [r["flake_rate"] for r in run_rates[:3]]
                trend = sum(recent_3)/3 - sum(early_3)/3
                direction = "INCREASING" if trend > 0.05 else "DECREASING" if trend < -0.05 else "STABLE"
                lines.append(f"  Flake rate trend: {direction} (delta {trend:+.1%} vs earliest runs)")
            env = hs.get("env_breakdown", {})
            if len(env) > 1:
                lines.append("  Per-host flake rates:")
                for host, v in env.items():
                    rate = v["failed"] / v["total"] if v["total"] else 0
                    lines.append(f"    {host}: {rate:.0%} ({v['failed']}/{v['total']})")
            arg_corr = hs.get("arg_correlation", {})
            if arg_corr:
                high_fail = {k: v for k, v in arg_corr.items() if v["rate"] > 0.3 and v["total"] >= 3}
                if high_fail:
                    lines.append("  High-failure parameter combinations:")
                    for combo, v in sorted(high_fail.items(), key=lambda x: -x[1]["rate"])[:3]:
                        lines.append(f"    {combo}: {v['rate']:.0%} fail rate ({v['failed']}/{v['total']})")
            branches = hs.get("git_branches_seen", [])
            if branches:
                lines.append(f"  Git branches with failures: {', '.join(branches[:5])}")
            history_section = "\n".join(lines)

        prompt = f"""You are an expert software engineer analyzing a flaky pytest test.

CURRENT RUN:
Test: {record.nodeid}
Flake rate this run: {record.flake_rate:.0%} ({record.fail_count} failures in {len(record.attempts)} runs)
Avg duration on failure: {_avg_fail_duration(record):.2f}s

Failure clusters:
{chr(10).join(cluster_summaries) if cluster_summaries else "(no failures this run -- see historical data)"}
{history_section}

In 3-4 sentences, give a concise hypothesis about WHY this test is flaky.
Be specific: timing races, state pollution, resource exhaustion, external deps, parameter-specific bugs.
If historical data shows a trend, host correlation, or parameter correlation, call it out explicitly.
Do not repeat the stack trace. Be direct and actionable."""

        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
                return data["content"][0]["text"].strip()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"\n[flakehunter] AI analysis failed for {record.nodeid}: {exc} — {body}")
            return ""

    except Exception as exc:
        print(f"\n[flakehunter] AI analysis failed for {record.nodeid}: {exc}")
        return ""


def _avg_fail_duration(record: "TestFlakeRecord") -> float:
    durations = [a.total_duration for a in record.attempts if a.outcome == "failed"]
    return sum(durations) / len(durations) if durations else 0.0
