"""Stable fingerprints for error messages.

Two failures with the same cause rarely have identical text: line numbers,
temp paths, durations, hashes and ports change on every run. Normalising those
away and hashing what is left lets the same failure be grouped across runs
(and lets the bot update one PR comment instead of posting a new one each time).
"""

import hashlib
import re

_SUBS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b", re.I), "<hex>"),
    (re.compile(r"(/[\w.@-]+){2,}"), "<path>"),
    (re.compile(r"([A-Za-z]:)?(\\[\w.@-]+){2,}"), "<path>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b\d+(\.\d+)?(ms|s|m|kb|mb|gb)?\b", re.I), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalise(message: str) -> str:
    text = message.strip().lower()
    for pattern, token in _SUBS:
        text = pattern.sub(token, text)
    return text.strip()


def fingerprint(*parts: str) -> str:
    joined = "|".join(normalise(p) for p in parts if p)
    return hashlib.sha1(joined.encode()).hexdigest()[:12]
