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


def maybe_explain(d: Diagnosis, explainer: Explainer | None) -> Diagnosis:
    """Ask the model only when the rules are unsure; never let it break triage."""
    if explainer is None or not needs_llm(d):
        return d
    try:
        answer = json.loads(explainer(build_prompt(d)))
        d.title = f"Likely cause: {answer['cause']}"
        d.suggested_fix = answer["fix"]
        d.retryable = bool(answer.get("retryable", d.retryable))
        d.category = d.category if d.category != "unknown" else "llm_diagnosed"
    except (ValueError, KeyError, TypeError):
        pass  # malformed model output: keep the rule-based diagnosis
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
