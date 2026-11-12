"""AWS Lambda webhook: GitHub calls it when a workflow run finishes.

Flow: verify the HMAC signature -> ignore anything but a failed run -> fetch
each failed job's log -> triage -> post (or update) one comment on the PR.
The comment carries a hidden marker so re-runs edit it instead of spamming.

"Failed" means conclusion "failure" or "timed_out" (a run that hit its time
limit). PRs from forks are not listed in the payload, so they are looked up
by the fork's owner and branch name.
"""

import base64
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request

from .explain import maybe_explain
from .report import MARKER, to_markdown
from .triage import triage_log

FAILED_CONCLUSIONS = {"failure", "timed_out"}
PR_EVENTS = {"pull_request", "pull_request_target"}
PAGE_SIZE = 100  # GitHub's maximum per page


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
    if payload.get("action") != "completed" or run.get("conclusion") not in FAILED_CONCLUSIONS:
        return _response(202, {"ignored": "run did not fail"})

    repo = payload["repository"]["full_name"]
    prs = find_prs(github, repo, run)
    triaged = []
    for job in github.failed_jobs(repo, run["id"]):
        diagnosis = maybe_explain(triage_log(github.job_log(repo, job["id"])), explainer)
        triaged.append({"job": job["name"], **diagnosis.to_dict()})
        comment = to_markdown(diagnosis, job["name"], job.get("html_url", ""))
        for pr in prs:
            github.upsert_comment(repo, pr, comment, MARKER)
    return _response(200, {"run_id": run["id"], "jobs": triaged})


def find_prs(github, repo: str, run: dict) -> list[int]:
    """PR numbers to comment on for this workflow run."""
    prs = [pr["number"] for pr in run.get("pull_requests") or []]
    if prs or run.get("event") not in PR_EVENTS:
        return prs
    # GitHub leaves pull_requests empty when the PR comes from a fork, so ask
    # for open PRs whose head is <fork owner>:<branch>.
    owner = ((run.get("head_repository") or {}).get("owner") or {}).get("login")
    branch = run.get("head_branch")
    if not owner or not branch:
        return []
    return github.open_prs_for_branch(repo, owner, branch)


class GitHubClient:
    API = "https://api.github.com"

    def __init__(self, token: str, urlopen=urllib.request.urlopen):
        self.token = token
        self.urlopen = urlopen  # tests pass a fake here, so no real network is needed

    def _req(self, method: str, path: str, data: dict | None = None, raw: bool = False):
        req = urllib.request.Request(
            self.API + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with self.urlopen(req, timeout=20) as resp:
            content = resp.read()
        return content.decode(errors="replace") if raw else json.loads(content or b"null")

    def _get_all(self, path: str, key: str | None = None) -> list:
        """GET every page of a list. A page shorter than PAGE_SIZE is the last one."""
        items, page = [], 1
        joiner = "&" if "?" in path else "?"
        while True:
            data = self._req("GET", f"{path}{joiner}per_page={PAGE_SIZE}&page={page}")
            batch = data[key] if key else data
            items += batch
            if len(batch) < PAGE_SIZE:
                return items
            page += 1

    def failed_jobs(self, repo, run_id):
        jobs = self._get_all(f"/repos/{repo}/actions/runs/{run_id}/jobs", key="jobs")
        return [j for j in jobs if j.get("conclusion") in FAILED_CONCLUSIONS]

    def open_prs_for_branch(self, repo, owner, branch):
        head = urllib.parse.quote(f"{owner}:{branch}", safe="")
        return [pr["number"] for pr in self._get_all(f"/repos/{repo}/pulls?state=open&head={head}")]

    def job_log(self, repo, job_id):
        return self._req("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs", raw=True)

    def upsert_comment(self, repo, pr, body, marker):
        comments = self._get_all(f"/repos/{repo}/issues/{pr}/comments")
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
