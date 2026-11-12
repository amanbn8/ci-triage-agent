"""Optional LLM fallback for failures no rule recognises.

Design choice: rules first, LLM only when needed. Known failures are diagnosed
instantly, for free and deterministically; the model is called only when the
rules are unsure (unknown category or low confidence). The prompt sends just
the failing step's evidence lines, not the whole log, which keeps it cheap and
keeps the model grounded in what actually happened.
"""

import json
import os
from typing import Callable, Protocol

from .triage import Diagnosis

LOW_CONFIDENCE = 0.5


class Explainer(Protocol):
    def __call__(self, prompt: str) -> str: ...


def needs_llm(d: Diagnosis) -> bool:
    return d.category == "unknown" or d.confidence < LOW_CONFIDENCE


def build_prompt(d: Diagnosis) -> str:
    evidence = "\n".join(d.evidence) or "(no error lines found)"
    return (
        "You are a CI engineer. A GitHub Actions job failed.\n"
        f"Failing step: {d.failing_step or 'unknown'}\n"
        f"Rule-based guess: {d.category} (confidence {d.confidence})\n"
        f"Log evidence:\n{evidence}\n\n"
        'Reply with JSON only: {"cause": "<one sentence>", "fix": "<one or two sentences>", '
        '"retryable": true|false}'
    )


def parse_answer(reply: str) -> dict | None:
    """Read the model's JSON reply. Returns None unless every field is present and valid."""
    # Models often wrap JSON in a ```json fence; take the text between the outer braces.
    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        answer = json.loads(reply[start:end + 1])
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    cause, fix = answer.get("cause"), answer.get("fix")
    if not (isinstance(cause, str) and cause.strip() and isinstance(fix, str) and fix.strip()):
        return None
    retryable = answer.get("retryable")
    if retryable is not None and not isinstance(retryable, bool):
        return None  # e.g. the string "false", which bool() would turn into True
    return {"cause": cause.strip(), "fix": fix.strip(), "retryable": retryable}


def maybe_explain(d: Diagnosis, explainer: Explainer | None) -> Diagnosis:
    """Ask the model only when the rules are unsure; never let it break triage.

    All-or-nothing: the diagnosis is changed only after the whole reply has been
    checked, so a bad reply can never leave it half-updated.
    """
    if explainer is None or not needs_llm(d):
        return d
    try:
        reply = explainer(build_prompt(d))
    except Exception:
        return d  # Bedrock down, throttled or misconfigured: keep the rule-based diagnosis
    answer = parse_answer(reply) if isinstance(reply, str) else None
    if answer is None:
        return d  # malformed model output: keep the rule-based diagnosis
    d.title = f"Likely cause: {answer['cause']}"
    d.suggested_fix = answer["fix"]
    if answer["retryable"] is not None:
        d.retryable = answer["retryable"]
    if d.category == "unknown":
        d.category = "llm_diagnosed"
    return d


def bedrock_explainer(model_id: str | None = None) -> Callable[[str], str]:  # pragma: no cover
    """Amazon Bedrock Converse API. Needs AWS credentials and BEDROCK_MODEL_ID."""
    import boto3

    model = model_id or os.environ["BEDROCK_MODEL_ID"]
    client = boto3.client("bedrock-runtime")

    def call(prompt: str) -> str:
        resp = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )
        return resp["output"]["message"]["content"][0]["text"]

    return call
