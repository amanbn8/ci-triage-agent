"""Render a diagnosis as Markdown for a pull-request comment."""

import re

from .triage import Diagnosis

MARKER = "<!-- ci-triage-agent -->"


def to_markdown(d: Diagnosis, job_name: str = "", run_url: str = "") -> str:
    head = f"### CI triage: {d.title}"
    meta = [f"**Category:** `{d.category}`", f"**Confidence:** {int(d.confidence * 100)}%"]
    if d.failing_step:
        meta.append(f"**Failing step:** `{d.failing_step}`")
    if job_name:
        meta.append(f"**Job:** {f'[{job_name}]({run_url})' if run_url else job_name}")
    verdict = ("Looks transient: a re-run will probably pass."
               if d.retryable else "Needs a code or config change; a re-run will not fix it.")
    lines = [MARKER, head, "", " · ".join(meta), "", f"> {verdict}", "",
             f"**Suggested fix:** {d.suggested_fix}"]
    if d.evidence:
        # Log text can contain ``` (and fork PR logs are written by strangers). A
        # Markdown code block only closes on a fence at least as long as the one
        # that opened it, so use one backtick more than the longest run in the evidence.
        longest = max((len(run) for line in d.evidence for run in re.findall(r"`+", line)), default=0)
        fence = "`" * max(3, longest + 1)
        lines += ["", "<details><summary>Evidence from the log</summary>", "", fence]
        lines += d.evidence
        lines += [fence, "</details>"]
    if d.alternatives:
        others = ", ".join(f"`{a['category']}`" for a in d.alternatives)
        lines += ["", f"<sub>Also considered: {others}</sub>"]
    lines += ["", f"<sub>fingerprint `{d.fingerprint}`</sub>"]
    return "\n".join(lines)
