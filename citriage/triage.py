"""Triage engine: log text in, structured diagnosis out.

Scoring: every rule pattern that matches adds the rule's weight, doubled when
the match is inside the failing step (an error there is far more likely to be
the real cause than a warning printed earlier).

Ranking: a rule that matched inside the failing step always beats a rule that
only matched in earlier steps; the score breaks ties. So a pile of retry
warnings during `pip install` cannot outvote the test that actually failed.

Confidence: the top rule's score divided by (all scores + UNEXPLAINED_SCORE).
The extra term stands for "a cause none of our rules knows", so one weak match
reads as fairly sure, never 100%, and competing matches lower it further.
"""

from dataclasses import asdict, dataclass, field

from .fingerprint import fingerprint
from .logparse import clean_line, failing_step, interesting_lines, split_steps, strip_marker
from .rules import RULES, Rule

IN_FAILING_STEP_BONUS = 2.0
UNEXPLAINED_SCORE = 1.0
MAX_EVIDENCE = 6


@dataclass
class Match:
    rule: Rule
    score: float
    evidence: list[str]
    in_failing_step: bool = False


@dataclass
class Diagnosis:
    category: str
    title: str
    rule_id: str | None
    confidence: float
    retryable: bool
    failing_step: str | None
    evidence: list[str]
    suggested_fix: str
    fingerprint: str
    alternatives: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _score_rule(rule: Rule, all_lines: list[str], failing: set[int]) -> Match | None:
    score, evidence, in_failing_step = 0.0, [], False
    for pattern in rule.compiled():
        hits = [i for i, line in enumerate(all_lines) if pattern.search(line)]
        if not hits:
            continue
        # One hit per pattern (breadth of evidence beats repetition). If the
        # pattern appears both early and in the failing step, use the failing one.
        hits_in_step = [i for i in hits if i in failing]
        i = hits_in_step[0] if hits_in_step else hits[0]
        if i in failing:
            score += rule.weight * IN_FAILING_STEP_BONUS
            in_failing_step = True
        else:
            score += rule.weight
        text = strip_marker(all_lines[i])
        if text not in evidence:
            evidence.append(text)
    return Match(rule, score, evidence[:MAX_EVIDENCE], in_failing_step) if score else None


def triage_log(log_text: str, rules: tuple[Rule, ...] = RULES) -> Diagnosis:
    steps = split_steps(log_text)
    failed = failing_step(steps)

    all_lines, failing_idx = [], set()
    for step in steps:
        for line in step.lines:
            if step is failed:
                failing_idx.add(len(all_lines))
            all_lines.append(line)

    matches = sorted(
        (m for r in rules if (m := _score_rule(r, all_lines, failing_idx))),
        # True sorts above False, so failing-step matches come first, then by score.
        key=lambda m: (m.in_failing_step, m.score), reverse=True,
    )
    context = interesting_lines(failed.lines if failed else [clean_line(l) for l in log_text.splitlines()])

    if not matches:
        return Diagnosis(
            category="unknown", title="No known failure signature matched", rule_id=None,
            confidence=0.0, retryable=False, failing_step=failed.name if failed else None,
            evidence=context[:MAX_EVIDENCE],
            suggested_fix="Read the evidence lines below; consider adding a rule for this failure.",
            fingerprint=fingerprint("unknown", *(context[:1] or [""])),
        )

    best, total = matches[0], sum(m.score for m in matches)
    return Diagnosis(
        category=best.rule.category,
        title=best.rule.title,
        rule_id=best.rule.id,
        confidence=round(best.score / (total + UNEXPLAINED_SCORE), 2),
        retryable=best.rule.retryable,
        failing_step=failed.name if failed else None,
        evidence=best.evidence,
        suggested_fix=best.rule.fix,
        fingerprint=fingerprint(best.rule.id, best.evidence[0]),
        alternatives=[
            {"rule_id": m.rule.id, "category": m.rule.category, "score": round(m.score, 2)}
            for m in matches[1:4]
        ],
    )
