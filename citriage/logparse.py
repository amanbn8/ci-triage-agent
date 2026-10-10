"""Parse raw GitHub Actions job logs into steps and pull out the lines that matter.

GitHub job logs look like:

    2026-10-08T09:14:02.1234567Z ##[group]Run pytest -q
    2026-10-08T09:14:02.1234567Z pytest -q
    2026-10-08T09:14:02.1234567Z ##[endgroup]
    ...test output...
    2026-10-08T09:14:09.0000000Z ##[error]Process completed with exit code 1.

A step starts at "##[group]Run <command>"; its output continues until the next
step starts. The step containing "##[error]" is the one that failed.
"""

import re
from dataclasses import dataclass, field

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\s?")
STEP_START_RE = re.compile(r"^##\[group\]Run (.+)$")
ERROR_MARKER = "##[error]"

# Lines worth showing a human: errors, failures, tracebacks, panics.
INTERESTING_RE = re.compile(
    r"(error|failed|failure|fatal|exception|traceback|panic|denied|killed|"
    r"timed out|cannot|could not|unable to|not found|illegal option|invalid|"
    r"rejected|refused|mismatch|abort)",
    re.IGNORECASE,
)


@dataclass
class Step:
    name: str
    lines: list[str] = field(default_factory=list)
    first_line_no: int = 0

    @property
    def failed(self) -> bool:
        return any(line.startswith(ERROR_MARKER) for line in self.lines)


def clean_line(raw: str) -> str:
    """Remove colour codes and the leading timestamp GitHub adds to every line."""
    return TIMESTAMP_RE.sub("", ANSI_RE.sub("", raw)).rstrip()


def split_steps(log_text: str) -> list[Step]:
    steps: list[Step] = [Step(name="(setup)")]
    for line_no, raw in enumerate(log_text.splitlines(), start=1):
        line = clean_line(raw)
        match = STEP_START_RE.match(line)
        if match:
            steps.append(Step(name=match.group(1).strip(), first_line_no=line_no))
            continue
        if line.startswith("##[endgroup]"):
            continue
        steps[-1].lines.append(line)
    return [s for s in steps if s.lines or s.name != "(setup)"]


def failing_step(steps: list[Step]) -> Step | None:
    """The first step with an error marker; if none is marked, the last step."""
    for step in steps:
        if step.failed:
            return step
    return steps[-1] if steps else None


def interesting_lines(lines: list[str], context: int = 1, limit: int = 12) -> list[str]:
    """Error-looking lines plus a little context, de-duplicated, in log order."""
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if line.startswith(ERROR_MARKER) and "exit code" in line:
            continue  # the generic "exit code 1" line says nothing useful
        if INTERESTING_RE.search(line):
            keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    if not keep:
        # Nothing looks like an error: the last output before the step died is
        # still the best clue, so show the tail rather than nothing.
        tail = [i for i, l in enumerate(lines)
                if l.strip() and not (l.startswith(ERROR_MARKER) and "exit code" in l)]
        keep.update(tail[-3:])
    out, seen = [], set()
    for i in sorted(keep):
        text = strip_marker(lines[i])
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out[:limit]


def strip_marker(line: str) -> str:
    return line.replace(ERROR_MARKER, "").strip()
