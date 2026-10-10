"""Known CI failure signatures.

Each rule is a category of failure, the regexes that identify it, whether a
simple re-run is likely to fix it, and the fix a maintainer would suggest.
Rules are plain data so new ones can be added without touching the engine.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    title: str
    patterns: tuple[str, ...]
    fix: str
    retryable: bool = False
    weight: float = 1.0

    def compiled(self) -> list[re.Pattern]:
        return [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in self.patterns]


RULES: tuple[Rule, ...] = (
    Rule(
        "pytest-failure", "test_failure", "Python tests failed",
        (r"^FAILED \S+::\S+", r"=+ .*\d+ failed", r"^E\s+(AssertionError|assert )"),
        "Open the first FAILED test, reproduce it locally with `pytest <test id> -x`, "
        "and fix the code or the assertion.",
        weight=1.2,
    ),
    Rule(
        "jest-failure", "test_failure", "JavaScript tests failed",
        (r"Tests:\s+\d+ failed", r"^\s*● .+ › ", r"^FAIL\s+\S+\.(test|spec)\.[jt]sx?"),
        "Run the failing spec locally with `npx jest <file>`; check snapshot or timing assumptions.",
        weight=1.2,
    ),
    Rule(
        "go-test-failure", "test_failure", "Go tests failed",
        (r"^--- FAIL: ", r"^FAIL\s+\S+\s+[\d.]+s"),
        "Re-run with `go test -run <TestName> -v ./...` to see the failing assertion.",
        weight=1.2,
    ),
    Rule(
        "bsd-gnu-tool-mismatch", "platform_specific", "Shell script uses GNU-only flags on macOS",
        (r"sed: \d+: .*(invalid command code|extra characters)", r"grep: invalid option -- P",
         r"readlink: illegal option -- f", r"(date|stat|xargs|find): illegal option",
         r"^usage: sed "),
        "macOS runners ship BSD tools. Use portable flags (e.g. `sed -i ''` vs `sed -i`, avoid "
        "`grep -P`), or install GNU coreutils (`brew install gnu-sed coreutils`) in the workflow.",
        weight=1.5,
    ),
    Rule(
        "missing-python-module", "dependency", "Python dependency missing",
        (r"ModuleNotFoundError: No module named ['\"][\w.]+['\"]", r"ImportError: cannot import name"),
        "Add the package to requirements/pyproject and pin a version; check the install step ran.",
        weight=1.3,
    ),
    Rule(
        "npm-resolve", "dependency", "npm could not resolve dependencies",
        (r"npm ERR! code (ERESOLVE|E404|ETARGET)", r"npm ERR! (notarget|404 Not Found)"),
        "Fix the version range in package.json or regenerate the lockfile with a clean `npm install`.",
        weight=1.3,
    ),
    Rule(
        "network-flake", "network", "Transient network failure",
        (r"Could not resolve host", r"ECONNRESET", r"ETIMEDOUT", r"Connection reset by peer",
         r"\b(502 Bad Gateway|503 Service Unavailable|504 Gateway Time-?out)\b",
         r"TLS handshake timeout", r"Temporary failure in name resolution"),
        "Likely transient: re-run the job. If it keeps happening, add retries "
        "(e.g. `nick-fields/retry`) or cache downloads.",
        retryable=True,
    ),
    Rule(
        "rate-limit", "rate_limit", "Hit an API or registry rate limit",
        (r"API rate limit exceeded", r"429 Too Many Requests", r"toomanyrequests"),
        "Authenticate requests (GITHUB_TOKEN / registry login) and cache dependencies; re-run later.",
        retryable=True, weight=1.2,
    ),
    Rule(
        "permission-denied", "permission", "Token or file permissions are insufficient",
        (r"Resource not accessible by integration", r"\b403 Forbidden\b",
         r"Permission denied", r"EACCES", r"is forbidden: User .* cannot"),
        "Grant the job the scopes it needs with a `permissions:` block, or fix the service "
        "account / file mode it runs as.",
        weight=1.3,
    ),
    Rule(
        "out-of-memory", "resources", "Process ran out of memory",
        (r"exit code 137", r"JavaScript heap out of memory", r"\bMemoryError\b",
         r"^Killed\s*$", r"OOMKilled"),
        "Reduce parallelism, raise the heap limit (e.g. `NODE_OPTIONS=--max-old-space-size=4096`), "
        "or use a larger runner.",
        weight=1.4,
    ),
    Rule(
        "disk-full", "resources", "Runner ran out of disk",
        (r"No space left on device",),
        "Free space before the build (remove unused toolchains/images) or use a larger runner.",
        weight=1.4,
    ),
    Rule(
        "job-timeout", "timeout", "Job timed out or was cancelled",
        (r"has exceeded the maximum execution time", r"The operation was canceled",
         r"timed out after \d+"),
        "Find the slow step; add `timeout-minutes`, cache dependencies, or split the job.",
        retryable=True,
    ),
    Rule(
        "compile-error", "compile_error", "Code failed to compile",
        (r"^\S+\.(c|cc|cpp|h|hpp|go|rs|java|kt):\d+(:\d+)?:\s*error", r"error\[E\d{4}\]",
         r"error TS\d+:", r"\bSyntaxError:", r"undefined reference to"),
        "Fix the first compiler error; later errors are often caused by it.",
        weight=1.3,
    ),
    Rule(
        "lint-failure", "lint", "Linter or formatter check failed",
        (r"would reformat", r"Found \d+ errors?\.", r"\d+ problems? \(\d+ errors?",
         r"golangci-lint", r"^Error: .*\(lint\)"),
        "Run the formatter/linter locally (e.g. `ruff check --fix`, `black .`, `npm run lint -- --fix`).",
    ),
    Rule(
        "checkout-failure", "infrastructure", "Git checkout or fetch failed",
        (r"fatal: couldn't find remote ref", r"fatal: reference is not a tree",
         r"fatal: unable to access"),
        "Check the ref exists and the token can read the repo; re-run if the remote was flaky.",
        retryable=True,
    ),
)
