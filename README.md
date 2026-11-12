# CI Triage Agent

**Tells you *why* a CI run failed and what to do about it, in seconds, from the raw log.**
Works as a command-line tool, as an **MCP server** that AI assistants (Claude, Copilot, Cursor)
can call, and as an **AWS Lambda webhook** that comments the diagnosis on the pull request.

Built after contributing CI fixes to [cncf/automation](https://github.com/cncf/automation):
most red builds there were not real bugs but platform quirks, missing permissions or network
blips, and someone had to read hundreds of log lines to find out which.

```
$ python -m citriage triage samples/macos_sed.log

### CI triage: Shell script uses GNU-only flags on macOS
Category: platform_specific · Confidence: 86% · Failing step: ./scripts/check-go-version.sh
> Needs a code or config change; a re-run will not fix it.
Suggested fix: macOS runners ship BSD tools. Use portable flags (sed -i '' vs sed -i, avoid grep -P)...
```

## What it does

| Capability | How it works |
|---|---|
| **Find the failing step** | Splits GitHub Actions logs into steps (`##[group]Run …`) and finds the one with `##[error]` |
| **Classify the failure** | 15 rule signatures: test failures (pytest, Jest, Go), compile errors, lint, missing dependencies, network flakes, rate limits, permissions, out-of-memory, disk full, timeouts, macOS BSD-vs-GNU tool mismatches, checkout errors |
| **Say if a re-run helps** | Each rule is marked retryable or not, so transient failures are not debugged by hand |
| **Score confidence** | Matches inside the failing step count double, and a rule that matched there always outranks rules that only matched in earlier steps. Confidence = top score / (all scores + 1), so one match is never 100%; runner-up categories are listed |
| **Group repeat failures** | Fingerprints errors after stripping paths, numbers, hashes and UUIDs, so the same failure hashes the same across runs |
| **Detect flaky tests** | Reads JUnit XML; a test that passed *and* failed on the same commit is flaky (reports fail rate, and flip rate in run order) |
| **AI fallback** | Only when rules are unsure, sends the failing step's evidence lines (not the whole log) to an LLM on **Amazon Bedrock** |

## Architecture

```
                 ┌─────────────── citriage ────────────────┐
 raw job log ──▶ │ logparse ─▶ triage (rules) ─▶ report    │ ─▶ Markdown / JSON
                 │                 │  unsure?              │
                 │                 └▶ explain (Bedrock)    │
 JUnit XML ────▶ │ flaky                                   │ ─▶ flaky test list
                 └─────────────────────────────────────────┘
        ▲                    ▲                      ▲
   CLI (__main__)     MCP server (stdio)     Lambda webhook (GitHub ─▶ PR comment)
```

The webhook is two Lambda functions joined by an SQS queue, because GitHub stops waiting
for a reply after 10 seconds:

```
GitHub ──▶ Receiver Lambda ──▶ SQS queue ──▶ Worker Lambda ──▶ one PR comment
           checks HMAC,                      fetches logs, triages
           replies 202 at once               every failed job
                                  │
                                  └─ fails 3 times ─▶ dead-letter queue (kept 14 days)
```

**Design choice: rules first, LLM second.** Known failures are diagnosed instantly, for free and
deterministically. The model is called only for the unknown or low-confidence cases, and gets
only the evidence lines, which keeps cost low and the answer grounded in the actual log.

## Quick start (Python 3.10+, no dependencies)

```bash
python -m unittest -v                                   # 43 tests
python -m citriage triage samples/pytest_failure.log
python -m citriage triage samples/npm_network.log --json
python -m citriage flaky --junit samples/junit_run1.xml:abc123 samples/junit_run2.xml:abc123
```

For `flaky`, list JUnit files oldest run first; in history JSON (or the MCP tool) give each
result a `run_number` so the flip rate uses the real run order.

## Use it from an AI assistant (MCP)

Add to your MCP client config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ci-triage": {
      "command": "python3",
      "args": ["-m", "citriage", "mcp"],
      "cwd": "/path/to/ci-triage-agent"
    }
  }
}
```

Tools exposed: `triage_ci_log`, `find_flaky_tests`, `fingerprint_error`. The server implements
the MCP JSON-RPC 2.0 protocol directly over stdio (initialize, version negotiation, tools/list,
tools/call, ping), so it needs no SDK.

## Run it on every failed PR build (AWS Lambda)

The GitHub token and webhook secret live in **AWS Secrets Manager**, not in Lambda environment
variables. Create the secret once. Put the JSON in a file so the token does not end up in your
shell history, and keep that file **outside this folder**: `sam build` packages everything in
the project folder into the Lambda code.

```bash
# ~/secret.json: {"github_token": "<token with actions:read, pull-requests:write>", "webhook_secret": "<random string>"}
aws secretsmanager create-secret --name ci-triage-agent --secret-string file://$HOME/secret.json
rm ~/secret.json
sam build && sam deploy --guided     # asks for SecretName and an optional BedrockModelId
```

Then add a GitHub webhook for **Workflow runs** pointing at the `WebhookUrl` output, with the
same webhook secret. On each failed or timed-out run the receiver verifies the
`X-Hub-Signature-256` HMAC, queues the run and replies at once. The worker then fetches every
failed job's log (all pages, not just the first 100), triages each one and posts **one comment
listing all failed jobs** on the PR, including PRs from forks. A hidden marker makes re-runs
**update** that comment instead of adding new ones, which also makes queue retries safe.

Set `BedrockModelId` to enable the AI fallback. Only the worker may call Bedrock, and only that
model (directly or through its inference profile); each function may read only the one secret.

> Status: the template passes `cfn-lint` and the webhook logic is unit-tested with fake GitHub
> and AWS clients, but it has not yet been deployed or called by a real GitHub webhook.

## Project layout

```
citriage/
  logparse.py     clean lines, split steps, find failing step, pick evidence
  rules.py        failure signatures (data, easy to extend)
  triage.py       scoring engine -> Diagnosis
  fingerprint.py  noise-free error hashing
  flaky.py        JUnit parsing + flaky detection
  report.py       Markdown for PR comments
  explain.py      optional Amazon Bedrock fallback
  mcp_server.py   MCP server (JSON-RPC over stdio)
  webhook.py      AWS Lambda + GitHub API client
samples/          realistic failing logs and JUnit files
tests/            unittest suite, run in GitHub Actions
template.yaml     AWS SAM deployment
```

## Adding a rule

```python
Rule("my-rule", "dependency", "Short title",
     (r"regex that identifies it",), "What the maintainer should do", retryable=False)
```

Add a sample log under `samples/` and a case in `tests/` so the rule stays correct.

## Limitations and next steps

- Rules are regexes: precise for known failures, blind to new ones (that is what the LLM fallback is for).
- A comment shows at most 20 failed jobs; the rest are counted but not detailed.
- Flaky detection needs history; the webhook does not store JUnit results yet (next: S3 or DynamoDB).
- Next: a GitHub Action wrapper and a small dashboard of the most common failure fingerprints.
