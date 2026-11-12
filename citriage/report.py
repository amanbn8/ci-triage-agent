"""Render diagnoses as Markdown for a pull-request comment."""

import re

from .triage import Diagnosis

MARKER = "<!-- ci-triage-agent -->"

# GitHub rejects comments longer than 65,536 characters. With at most 20 jobs,
# 6 evidence lines each cut to 300 characters, a comment stays well below that.
MAX_JOBS_IN_COMMENT = 20
MAX_LINE_CHARS = 300


def _shorten(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " …"


def _body(d: Diagnosis, job_name: str = "", run_url: str = "") -> list[str]:
    """Everything about one diagnosis except its heading."""
    meta = [f"**Category:** `{d.category}`", f"**Confidence:** {int(d.confidence * 100)}%"]
    if d.failing_step:
        meta.append(f"**Failing step:** `{d.failing_step}`")
    if job_name:
        meta.append(f"**Job:** {f'[{job_name}]({run_url})' if run_url else job_name}")
    verdict = ("Looks transient: a re-run will probably pass."
               if d.retryable else "Needs a code or config change; a re-run will not fix it.")
    lines = [" · ".join(meta), "", f"> {verdict}", "", f"**Suggested fix:** {d.suggested_fix}"]
    if d.evidence:
        evidence = [_shorten(line) for line in d.evidence]
        # Log text can contain ``` (and fork PR logs are written by strangers). A
        # Markdown code block only closes on a fence at least as long as the one
        # that opened it, so use one backtick more than the longest run in the evidence.
        longest = max((len(run) for line in evidence for run in re.findall(r"`+", line)), default=0)
        fence = "`" * max(3, longest + 1)
        lines += ["", "<details><summary>Evidence from the log</summary>", "", fence]
        lines += evidence
        lines += [fence, "</details>"]
    if d.alternatives:
        others = ", ".join(f"`{a['category']}`" for a in d.alternatives)
        lines += ["", f"<sub>Also considered: {others}</sub>"]
    lines += ["", f"<sub>fingerprint `{d.fingerprint}`</sub>"]
    return lines


def to_markdown(d: Diagnosis, job_name: str = "", run_url: str = "") -> str:
    """One diagnosis as a comment (used by the CLI, MCP, and runs with one failed job)."""
    return "\n".join([MARKER, f"### CI triage: {d.title}", "", *_body(d, job_name, run_url)])


def combined_markdown(jobs: list[tuple[str, str, Diagnosis]]) -> str:
    """One comment covering every failed job of a run: [(job name, job url, diagnosis), ...]."""
    if len(jobs) == 1:
        name, url, d = jobs[0]
        return to_markdown(d, name, url)
    transient = sum(1 for _, _, d in jobs if d.retryable)
    lines = [MARKER, f"### CI triage: {len(jobs)} failed jobs", "",
             f"{transient} look transient (a re-run will probably pass); "
             f"{len(jobs) - transient} need a code or config change."]
    for name, url, d in jobs[:MAX_JOBS_IN_COMMENT]:
        heading = f"[{name}]({url})" if url else name
        lines += ["", "---", "", f"#### {heading}: {d.title}", "", *_body(d)]
    hidden = len(jobs) - MAX_JOBS_IN_COMMENT
    if hidden > 0:
        lines += ["", f"<sub>…and {hidden} more failed jobs not shown.</sub>"]
    return "\n".join(lines)
