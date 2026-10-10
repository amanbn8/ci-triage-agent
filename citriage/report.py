"""Render a diagnosis as Markdown for a pull-request comment."""

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
        lines += ["", "<details><summary>Evidence from the log</summary>", "", "```"]
        lines += d.evidence
        lines += ["```", "</details>"]
    if d.alternatives:
        others = ", ".join(f"`{a['category']}`" for a in d.alternatives)
        lines += ["", f"<sub>Also considered: {others}</sub>"]
    lines += ["", f"<sub>fingerprint `{d.fingerprint}`</sub>"]
    return "\n".join(lines)
