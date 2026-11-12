import base64
import hashlib
import hmac
import io
import json
import unittest
from pathlib import Path

from citriage.explain import build_prompt, maybe_explain, needs_llm
from citriage.fingerprint import fingerprint, normalise
from citriage.flaky import TestResult, find_flaky, parse_junit
from citriage.logparse import clean_line, failing_step, split_steps
from citriage.mcp_server import handle, serve
from citriage.report import MARKER, to_markdown
from citriage.triage import triage_log
from citriage.webhook import GitHubClient, handle_event, verify_signature

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text()


class LogParseTests(unittest.TestCase):
    def test_strips_timestamp_and_ansi(self):
        raw = "2026-10-08T09:14:02.1234567Z \x1b[31mFAILED\x1b[0m test_x"
        self.assertEqual(clean_line(raw), "FAILED test_x")

    def test_finds_failing_step(self):
        steps = split_steps(sample("pytest_failure.log"))
        self.assertEqual([s.name for s in steps][-1], "pytest -q")
        self.assertEqual(failing_step(steps).name, "pytest -q")


class TriageTests(unittest.TestCase):
    CASES = {
        "pytest_failure.log": ("test_failure", False),
        "macos_sed.log": ("platform_specific", False),
        "npm_network.log": ("network", True),
        "permission_denied.log": ("permission", False),
        "oom.log": ("resources", False),
    }

    def test_known_failures_classified(self):
        for name, (category, retryable) in self.CASES.items():
            with self.subTest(name):
                d = triage_log(sample(name))
                self.assertEqual(d.category, category)
                self.assertEqual(d.retryable, retryable)
                self.assertTrue(d.evidence)
                self.assertFalse(any("##[error]" in e for e in d.evidence))

    def test_unknown_still_shows_evidence(self):
        d = triage_log(sample("unknown.log"))
        self.assertEqual(d.category, "unknown")
        self.assertIn("deploy: remote rejected manifest v2 (schema mismatch)", d.evidence)

    def test_failing_step_outweighs_earlier_noise(self):
        log = (
            "##[group]Run pip install -r requirements.txt\n"
            "WARNING: Retrying after connection broken by ECONNRESET\n"
            "WARNING: got 503 Service Unavailable from mirror, retrying\n"
            "##[group]Run pytest\n"
            "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
            "##[error]Process completed with exit code 1.\n"
        )
        d = triage_log(log)
        self.assertEqual(d.category, "test_failure")
        self.assertEqual(d.alternatives[0]["category"], "network")
        self.assertLess(d.confidence, 1.0)

    def test_one_match_is_not_fully_confident(self):
        # Limitation 4: a single matching rule used to give 100% confidence.
        log = ("##[group]Run ./upload.sh\n"
               "cp: report.txt: Permission denied\n"
               "##[error]Process completed with exit code 1.\n")
        d = triage_log(log)
        self.assertEqual(d.category, "permission")
        self.assertGreater(d.confidence, 0.5)
        self.assertLess(d.confidence, 1.0)

    def test_failing_step_beats_many_earlier_warnings(self):
        # Limitation 5: three different network warnings in an earlier step
        # (score 3.0) used to beat one real test failure in the failing step (2.4).
        log = (
            "##[group]Run pip install -r requirements.txt\n"
            "WARNING: Retrying after connection broken by ECONNRESET\n"
            "WARNING: got 503 Service Unavailable from mirror, retrying\n"
            "WARNING: Temporary failure in name resolution, retrying\n"
            "##[group]Run pytest\n"
            "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
            "##[error]Process completed with exit code 1.\n"
        )
        d = triage_log(log)
        self.assertEqual(d.category, "test_failure")
        self.assertEqual(d.alternatives[0]["category"], "network")


class FingerprintTests(unittest.TestCase):
    def test_noise_removed(self):
        a = "Timeout after 3021ms in /home/runner/work/app/src/db.py line 88 (sha 9f2c1ab)"
        b = "Timeout after 4410ms in /home/runner/work/other/src/db.py line 12 (sha 77e01cd)"
        self.assertEqual(normalise(a), normalise(b))
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_different_errors_differ(self):
        self.assertNotEqual(fingerprint("connection refused"), fingerprint("permission denied"))


class FlakyTests(unittest.TestCase):
    def test_same_commit_pass_and_fail_is_flaky(self):
        results = (parse_junit(sample("junit_run1.xml"), "abc123")
                   + parse_junit(sample("junit_run2.xml"), "abc123"))
        flaky = find_flaky(results)
        self.assertEqual([f["test_id"] for f in flaky], ["tests.test_api::test_upload"])
        self.assertEqual(flaky[0]["fail_rate"], 0.5)

    def test_failure_fixed_by_new_commit_is_not_flaky(self):
        results = [TestResult("t", "c1", False), TestResult("t", "c2", True)]
        self.assertEqual(find_flaky(results), [])

    def test_flip_rate_follows_run_number_not_input_order(self):
        # Limitation 13: flips were counted in whatever order results arrived.
        # True order: pass, pass, fail, fail = 1 flip in 3 changes (0.33).
        shuffled = [TestResult("t", "c", False, run_number=3), TestResult("t", "c", True, run_number=1),
                    TestResult("t", "c", False, run_number=4), TestResult("t", "c", True, run_number=2)]
        self.assertEqual(find_flaky(shuffled)[0]["flip_rate"], 0.33)

    def test_skipped_tests_ignored(self):
        ids = {r.test_id for r in parse_junit(sample("junit_run1.xml"), "c")}
        self.assertNotIn("tests.test_api::test_skip", ids)


class ReportAndExplainTests(unittest.TestCase):
    def test_markdown_has_marker_and_fix(self):
        md = to_markdown(triage_log(sample("macos_sed.log")), "build (macos-latest)", "https://x")
        self.assertTrue(md.startswith(MARKER))
        self.assertIn("BSD", md)
        self.assertIn("[build (macos-latest)](https://x)", md)

    def test_backticks_in_log_cannot_escape_code_block(self):
        # Logs from fork PRs are written by strangers: a line containing ```
        # must not close our code block and inject Markdown into the comment.
        line = "Permission denied ``` [click me](https://evil.example) @everyone"
        md = to_markdown(triage_log(f"##[group]Run make\n{line}\n"))
        # The fence is one backtick longer than any run in the evidence, so it stays closed.
        self.assertIn(f"````\n{line}\n````", md)

    def test_llm_only_called_when_rules_unsure(self):
        calls = []

        def fake(prompt):
            calls.append(prompt)
            return json.dumps({"cause": "manifest schema changed", "fix": "regenerate manifest",
                               "retryable": False})

        confident = maybe_explain(triage_log(sample("pytest_failure.log")), fake)
        self.assertEqual(confident.category, "test_failure")
        self.assertEqual(calls, [])

        unsure = triage_log(sample("unknown.log"))
        self.assertTrue(needs_llm(unsure))
        out = maybe_explain(unsure, fake)
        self.assertEqual(out.category, "llm_diagnosed")
        self.assertEqual(out.suggested_fix, "regenerate manifest")
        self.assertIn("schema mismatch", calls[0])

    def test_bad_llm_output_keeps_rule_result(self):
        d = maybe_explain(triage_log(sample("unknown.log")), lambda p: "not json")
        self.assertEqual(d.category, "unknown")

    def test_incomplete_llm_answer_changes_nothing(self):
        # Limitation 10: a reply missing "fix" used to change the title but not the fix.
        before = triage_log(sample("unknown.log")).to_dict()

        def broken_bedrock(prompt):
            raise RuntimeError("ThrottlingException")

        for explainer in (lambda p: '{"cause": "manifest schema changed"}',
                          lambda p: '{"cause": "x", "fix": "y", "retryable": "false"}',
                          broken_bedrock):
            with self.subTest(explainer=explainer):
                after = maybe_explain(triage_log(sample("unknown.log")), explainer)
                self.assertEqual(after.to_dict(), before)

    def test_llm_json_inside_code_fence_is_accepted(self):
        reply = '```json\n{"cause": "schema changed", "fix": "regenerate", "retryable": true}\n```'
        d = maybe_explain(triage_log(sample("unknown.log")), lambda p: reply)
        self.assertEqual(d.category, "llm_diagnosed")
        self.assertTrue(d.retryable)

    def test_prompt_sends_evidence_not_whole_log(self):
        prompt = build_prompt(triage_log(sample("pytest_failure.log")))
        self.assertIn("FAILED tests/test_dates.py::test_parse_date", prompt)
        self.assertNotIn("Successfully installed", prompt)


class McpTests(unittest.TestCase):
    def rpc(self, method, params=None, msg_id=1):
        return handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})

    def test_handshake_and_tool_list(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["triage_ci_log", "find_flaky_tests", "fingerprint_error"])

    def test_unknown_protocol_version_falls_back(self):
        init = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")

    def test_tool_call_returns_diagnosis(self):
        res = self.rpc("tools/call", {"name": "triage_ci_log",
                                      "arguments": {"log": sample("oom.log"), "format": "json"}})
        body = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(body["category"], "resources")
        self.assertFalse(res["result"]["isError"])

    def test_errors(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)
        bad = self.rpc("tools/call", {"name": "triage_ci_log", "arguments": {}})
        self.assertTrue(bad["result"]["isError"])

    def test_stdio_loop(self):
        stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            'not json\n'
        )
        stdout = io.StringIO()
        serve(stdin, stdout)
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual(replies[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertEqual(len(replies), 2)

    def test_invalid_requests(self):
        # Limitation 9: valid JSON that is not an object used to crash serve(),
        # and a request with no method got -32601 instead of -32600.
        stdin = io.StringIO('[1, 2]\n42\n{"jsonrpc":"2.0","id":3}\n'
                            '{"jsonrpc":"2.0","id":4,"method":"ping","params":[1]}\n'
                            '{"jsonrpc":"2.0","id":5,"method":"ping"}\n')
        stdout = io.StringIO()
        serve(stdin, stdout)
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual([r.get("error", {}).get("code") for r in replies],
                         [-32600, -32600, -32600, -32602, None])
        self.assertEqual([r["id"] for r in replies], [None, None, 3, 4, 5])

    def test_wrong_argument_type_is_tool_error(self):
        # A number instead of a string used to raise AttributeError and stop the server.
        res = self.rpc("tools/call", {"name": "triage_ci_log", "arguments": {"log": 123}})
        self.assertTrue(res["result"]["isError"])
        res = self.rpc("tools/call", {"name": "fingerprint_error", "arguments": {"message": None}})
        self.assertTrue(res["result"]["isError"])


class FakeGitHub:
    def __init__(self, log, fork_prs=()):
        self.log, self.comments, self.fork_prs, self.branch_lookups = log, [], list(fork_prs), []

    def failed_jobs(self, repo, run_id):
        return [{"id": 7, "name": "build (macos-latest)", "html_url": "https://gh/job/7"}]

    def open_prs_for_branch(self, repo, owner, branch):
        self.branch_lookups.append((repo, owner, branch))
        return self.fork_prs

    def job_log(self, repo, job_id):
        return self.log

    def upsert_comment(self, repo, pr, body, marker):
        self.comments.append((repo, pr, body))


class WebhookTests(unittest.TestCase):
    SECRET = "s3cret"

    def event(self, payload, signed=True, event_type="workflow_run", b64=False):
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        return {
            "headers": {"X-GitHub-Event": event_type,
                        "X-Hub-Signature-256": sig if signed else "sha256=bad"},
            "body": base64.b64encode(body).decode() if b64 else body.decode(),
            "isBase64Encoded": b64,
        }

    PAYLOAD = {
        "action": "completed",
        "repository": {"full_name": "amanbn8/demo"},
        "workflow_run": {"id": 99, "conclusion": "failure", "pull_requests": [{"number": 12}]},
    }

    def test_signature_check(self):
        self.assertFalse(verify_signature(self.SECRET, b"{}", None))
        gh = FakeGitHub("")
        self.assertEqual(handle_event(self.event(self.PAYLOAD, signed=False), gh, self.SECRET)["statusCode"], 401)

    def test_failed_run_gets_pr_comment(self):
        gh = FakeGitHub(sample("macos_sed.log"))
        resp = handle_event(self.event(self.PAYLOAD, b64=True), gh, self.SECRET)
        self.assertEqual(resp["statusCode"], 200)
        repo, pr, body = gh.comments[0]
        self.assertEqual((repo, pr), ("amanbn8/demo", 12))
        self.assertIn("platform_specific", body)

    def test_timed_out_run_is_triaged(self):
        # Limitation 8: runs that hit the time limit end as "timed_out", not "failure".
        gh = FakeGitHub(sample("oom.log"))
        run = {**self.PAYLOAD["workflow_run"], "conclusion": "timed_out"}
        resp = handle_event(self.event(dict(self.PAYLOAD, workflow_run=run)), gh, self.SECRET)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(len(gh.comments), 1)

    def test_fork_pr_found_by_branch(self):
        # Limitation 7: GitHub leaves pull_requests empty for PRs from forks.
        gh = FakeGitHub(sample("macos_sed.log"), fork_prs=[34])
        run = {**self.PAYLOAD["workflow_run"], "pull_requests": [], "event": "pull_request",
               "head_branch": "fix-sed", "head_repository": {"owner": {"login": "someone"}}}
        handle_event(self.event(dict(self.PAYLOAD, workflow_run=run)), gh, self.SECRET)
        self.assertEqual(gh.branch_lookups, [("amanbn8/demo", "someone", "fix-sed")])
        self.assertEqual([pr for _, pr, _ in gh.comments], [34])

    def test_successful_run_and_other_events_ignored(self):
        gh = FakeGitHub("")
        ok = dict(self.PAYLOAD, workflow_run={**self.PAYLOAD["workflow_run"], "conclusion": "success"})
        self.assertEqual(handle_event(self.event(ok), gh, self.SECRET)["statusCode"], 202)
        self.assertEqual(handle_event(self.event(self.PAYLOAD, event_type="push"), gh, self.SECRET)["statusCode"], 202)
        self.assertEqual(gh.comments, [])


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.data if isinstance(self.data, bytes) else json.dumps(self.data).encode()


class FakeHTTP:
    """Stands in for urllib.request.urlopen: answers from a dict of (method, url) -> data."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return FakeResponse(self.routes.get((req.get_method(), req.full_url)))


class GitHubClientTests(unittest.TestCase):
    """Limitation 13: the real GitHub client, tested against a fake HTTP layer."""
    API = "https://api.github.com/repos/o/r"

    def test_reads_every_page_of_jobs(self):
        # Limitation 6: only the first 100 jobs used to be read.
        page1 = [{"id": i, "conclusion": "success"} for i in range(99)] + [{"id": 99, "conclusion": "failure"}]
        page2 = [{"id": 100, "conclusion": "timed_out"}]
        http = FakeHTTP({
            ("GET", f"{self.API}/actions/runs/5/jobs?per_page=100&page=1"): {"jobs": page1},
            ("GET", f"{self.API}/actions/runs/5/jobs?per_page=100&page=2"): {"jobs": page2},
        })
        jobs = GitHubClient("tok", urlopen=http).failed_jobs("o/r", 5)
        self.assertEqual([j["id"] for j in jobs], [99, 100])
        self.assertEqual(http.requests[0].get_header("Authorization"), "Bearer tok")

    def test_updates_marker_comment_found_on_a_later_page(self):
        page1 = [{"id": i, "body": "lgtm"} for i in range(100)]
        page2 = [{"id": 555, "body": "<!-- m -->\nold diagnosis"}]
        http = FakeHTTP({
            ("GET", f"{self.API}/issues/12/comments?per_page=100&page=1"): page1,
            ("GET", f"{self.API}/issues/12/comments?per_page=100&page=2"): page2,
        })
        GitHubClient("tok", urlopen=http).upsert_comment("o/r", 12, "new", "<!-- m -->")
        last = http.requests[-1]
        self.assertEqual((last.get_method(), last.full_url), ("PATCH", f"{self.API}/issues/comments/555"))
        self.assertEqual(json.loads(last.data), {"body": "new"})

    def test_posts_new_comment_when_none_has_the_marker(self):
        http = FakeHTTP({("GET", f"{self.API}/issues/12/comments?per_page=100&page=1"): []})
        GitHubClient("tok", urlopen=http).upsert_comment("o/r", 12, "new", "<!-- m -->")
        last = http.requests[-1]
        self.assertEqual((last.get_method(), last.full_url), ("POST", f"{self.API}/issues/12/comments"))

    def test_finds_open_pr_from_a_fork(self):
        http = FakeHTTP({("GET", f"{self.API}/pulls?state=open&head=someone%3Afix%2Fsed&per_page=100&page=1"):
                         [{"number": 34}]})
        self.assertEqual(GitHubClient("tok", urlopen=http).open_prs_for_branch("o/r", "someone", "fix/sed"), [34])


if __name__ == "__main__":
    unittest.main()
