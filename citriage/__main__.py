"""Command line.

    python -m citriage triage job.log            # Markdown diagnosis
    python -m citriage triage job.log --json     # machine-readable
    python -m citriage flaky history.json        # flaky tests from [{test_id, commit, passed}]
    python -m citriage flaky --junit a.xml:sha1 b.xml:sha2
    python -m citriage mcp                       # run as an MCP server on stdio
"""

import argparse
import json
import sys

from .flaky import TestResult, find_flaky, parse_junit
from .report import to_markdown
from .triage import triage_log


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citriage", description="Explain why CI failed.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("triage", help="diagnose a job log")
    t.add_argument("log", help="log file, or - for stdin")
    t.add_argument("--json", action="store_true")

    f = sub.add_parser("flaky", help="find flaky tests")
    f.add_argument("history", nargs="?", help="JSON list of {test_id, commit, passed}")
    f.add_argument("--junit", nargs="*", default=[], metavar="FILE:COMMIT")

    sub.add_parser("mcp", help="run the MCP server on stdio")
    args = parser.parse_args(argv)

    if args.cmd == "triage":
        text = sys.stdin.read() if args.log == "-" else open(args.log, encoding="utf-8").read()
        d = triage_log(text)
        print(json.dumps(d.to_dict(), indent=2) if args.json else to_markdown(d))
        return 0 if d.category != "unknown" else 2

    if args.cmd == "flaky":
        results = []
        if args.history:
            with open(args.history, encoding="utf-8") as fh:
                results += [TestResult(r["test_id"], r["commit"], bool(r["passed"]))
                            for r in json.load(fh)]
        for spec in args.junit:
            path, _, commit = spec.rpartition(":")
            with open(path, encoding="utf-8") as fh:
                results += parse_junit(fh.read(), commit)
        print(json.dumps(find_flaky(results), indent=2))
        return 0

    from .mcp_server import serve
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
