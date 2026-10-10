"""AWS Lambda webhook: GitHub calls it when a workflow run finishes.

Flow: verify the HMAC signature -> ignore anything but a failed run -> fetch
each failed job's log -> triage -> post (or update) one comment on the PR.
The comment carries a hidden marker so re-runs edit it instead of spamming.
"""

import base64
import hashlib
import hmac
import json
import os
import urllib.request

from .explain import maybe_explain
from .report import MARKER, to_markdown
from .triage import triage_log


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)  # constant time: no timing leaks


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def handle_event(event: dict, github, secret: str, explainer=None) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()

    if not verify_signature(secret, body, headers.get("x-hub-signature-256")):
        return _response(401, {"error": "bad signature"})
    if headers.get("x-github-event") != "workflow_run":
        return _response(202, {"ignored": "not a workflow_run event"})

    payload = json.loads(body)
    run = payload.get("workflow_run") or {}
    if payload.get("action") != "completed" or run.get("conclusion") != "failure":
        return _response(202, {"ignored": "run did not fail"})

    repo = payload["repository"]["full_name"]
    prs = [pr["number"] for pr in run.get("pull_requests", [])]
    triaged = []
    for job in github.failed_jobs(repo, run["id"]):
        diagnosis = maybe_explain(triage_log(github.job_log(repo, job["id"])), explainer)
        triaged.append({"job": job["name"], **diagnosis.to_dict()})
        comment = to_markdown(diagnosis, job["name"], job.get("html_url", ""))
        for pr in prs:
            github.upsert_comment(repo, pr, comment, MARKER)
    return _response(200, {"run_id": run["id"], "jobs": triaged})


class GitHubClient:  # pragma: no cover - talks to the real GitHub API
    API = "https://api.github.com"

    def __init__(self, token: str):
        self.token = token

    def _req(self, method: str, path: str, data: dict | None = None, raw: bool = False):
        req = urllib.request.Request(
            self.API + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read()
        return content.decode(errors="replace") if raw else json.loads(content or b"null")

    def failed_jobs(self, repo, run_id):
        jobs = self._req("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")["jobs"]
        return [j for j in jobs if j.get("conclusion") == "failure"]

    def job_log(self, repo, job_id):
        return self._req("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs", raw=True)

    def upsert_comment(self, repo, pr, body, marker):
        comments = self._req("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100")
        mine = next((c for c in comments if marker in (c.get("body") or "")), None)
        if mine:
            self._req("PATCH", f"/repos/{repo}/issues/comments/{mine['id']}", {"body": body})
        else:
            self._req("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": body})


def lambda_handler(event, context):  # pragma: no cover - AWS entry point
    explainer = None
    if os.environ.get("BEDROCK_MODEL_ID"):
        from .explain import bedrock_explainer
        explainer = bedrock_explainer()
    return handle_event(event, GitHubClient(os.environ["GITHUB_TOKEN"]),
                        os.environ["WEBHOOK_SECRET"], explainer)
