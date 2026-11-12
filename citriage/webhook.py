"""AWS Lambda webhook: GitHub calls it when a workflow run finishes.

Two Lambda functions, joined by an SQS queue:

1. Receiver (receive): verify the HMAC signature, ignore anything but a failed
   run, put a small note on the queue and reply 202 at once. GitHub waits at
   most 10 seconds for a reply, so this step does no slow work.
2. Worker (process_run): started by SQS for each note. Fetches every failed
   job's log, triages each one, and posts (or updates) ONE comment listing all
   of them. The comment carries a hidden marker so re-runs edit it instead of
   spamming; that also makes SQS retries safe.

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
from .report import MARKER, combined_markdown
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


def receive(event: dict, secret: str, queue) -> dict:
    """Step 1: check the delivery and queue it. Must answer GitHub within 10 seconds."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()

    if not verify_signature(secret, body, headers.get("x-hub-signature-256")):
        return _response(401, {"error": "bad signature"})
    if headers.get("x-github-event") != "workflow_run":
        return _response(202, {"ignored": "not a workflow_run event"})

    try:
        payload = json.loads(body)
    except ValueError:
        return _response(400, {"error": "body is not JSON"})
    if not isinstance(payload, dict):
        return _response(400, {"error": "body is not a JSON object"})
    run = payload.get("workflow_run") or {}
    if payload.get("action") != "completed" or run.get("conclusion") not in FAILED_CONCLUSIONS:
        return _response(202, {"ignored": "run did not fail"})

    repo = (payload.get("repository") or {}).get("full_name")
    if not repo or "id" not in run:
        return _response(400, {"error": "payload is missing repository or run id"})
    queue.send({"repo": repo, "run": _slim_run(run)})
    return _response(202, {"queued": run["id"]})


def _slim_run(run: dict) -> dict:
    """Only the fields the worker needs, so the queued note stays small."""
    owner = ((run.get("head_repository") or {}).get("owner") or {}).get("login")
    return {
        "id": run["id"],
        "event": run.get("event"),
        "head_branch": run.get("head_branch"),
        "head_repository": {"owner": {"login": owner}},
        "pull_requests": [{"number": pr["number"]} for pr in run.get("pull_requests") or []],
    }


def process_run(message: dict, github, explainer=None) -> dict:
    """Step 2: triage every failed job of the run and post one combined comment."""
    repo, run = message["repo"], message["run"]
    prs = find_prs(github, repo, run)
    jobs = []
    for job in github.failed_jobs(repo, run["id"]):
        diagnosis = maybe_explain(triage_log(github.job_log(repo, job["id"])), explainer)
        jobs.append((job["name"], job.get("html_url", ""), diagnosis))
    if jobs:
        comment = combined_markdown(jobs)
        for pr in prs:
            github.upsert_comment(repo, pr, comment, MARKER)
    return {"run_id": run["id"], "prs": prs,
            "jobs": [{"job": name, **d.to_dict()} for name, _, d in jobs]}


def handle_sqs_event(event: dict, github, explainer=None) -> None:
    """The worker's input: SQS hands over a batch of records, each holding one note.

    If anything raises, SQS makes the note visible again and retries it; after
    3 failed tries it moves to the dead-letter queue (see template.yaml).
    """
    for record in event["Records"]:
        process_run(json.loads(record["body"]), github, explainer)


class SqsQueue:
    """Sends notes to an SQS queue. The boto3 client is passed in, so tests can fake it."""

    def __init__(self, client, queue_url: str):
        self.client, self.queue_url = client, queue_url

    def send(self, message: dict) -> None:
        self.client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(message))


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


def load_secrets(secret_id: str, client) -> tuple[str, str]:
    """Read (github_token, webhook_secret) from one AWS Secrets Manager secret.

    The secret's value is JSON: {"github_token": "...", "webhook_secret": "..."}.
    Keeping them there instead of in Lambda environment variables means anyone
    who can view the function's configuration still cannot see them; only this
    function's IAM role may read the secret.
    """
    value = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    return value["github_token"], value["webhook_secret"]


_secrets = None  # kept between calls in the same Lambda container: one read per cold start


def _get_secrets() -> tuple[str, str]:  # pragma: no cover - needs AWS
    global _secrets
    if _secrets is None:
        import boto3  # ships with the AWS Lambda Python runtime, so nothing extra to package
        _secrets = load_secrets(os.environ["SECRET_ID"], boto3.client("secretsmanager"))
    return _secrets


def receiver_handler(event, context):  # pragma: no cover - AWS entry point (Function URL)
    import boto3
    _, webhook_secret = _get_secrets()
    return receive(event, webhook_secret, SqsQueue(boto3.client("sqs"), os.environ["QUEUE_URL"]))


def worker_handler(event, context):  # pragma: no cover - AWS entry point (SQS trigger)
    token, _ = _get_secrets()
    explainer = None
    if os.environ.get("BEDROCK_MODEL_ID"):
        from .explain import bedrock_explainer
        explainer = bedrock_explainer()
    handle_sqs_event(event, GitHubClient(token), explainer)
