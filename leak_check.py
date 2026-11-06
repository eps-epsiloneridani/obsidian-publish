#!/usr/bin/env python3
"""Fail-closed pre-publish leak check (Phase 3).

Runs against a built (staging) artifact directory and fails (non-zero exit) on
any of:

1. **Allowlist violation** -- any emitted file that is neither a generated
   asset nor a note whose source path is allowlisted AND present in the vault.
2. **Deny-list content** -- API-key shapes (``sk-``, ``sk-or-``, ``xoxb-``,
   ``AKIA...``), a generic high-entropy secret heuristic, and a configurable
   sensitive-basename list matched against both artifact file paths and file
   bytes.

A clean artifact returns exit 0 and an empty problem list. This is a mandatory,
fail-closed step in the publish pipeline (``build.py``) -- the atomic swap to
the live ``out/`` only happens after it passes.

Usage::

    python3 leak_check.py VAULT_PATH STAGING_DIR [--allowlist web/]...

Pure Python 3 stdlib. No third-party dependencies, no network access.
"""

from __future__ import annotations

import argparse
import math
import posixpath
import re
import sys
from collections import Counter
from pathlib import Path

from filter import DEFAULT_ALLOWLIST, GENERATED_ASSETS, is_allowed, parse_allowlist_file

# ---------------------------------------------------------------------------
# Deny list (sensitive basenames / folders)
# ---------------------------------------------------------------------------

# Sensitive note basenames and folder names that must never survive into the
# artifact regardless of the allowlist (defence-in-depth against a future
# allowlist mistake). Single-word entries are matched against path segments
# only (to avoid false positives such as "work" inside "network"); multi-word
# entries are additionally scanned for inside file bytes.
DEFAULT_DENY_BASENAMES = [
    "Token Checker",
    "ADHD Diagnosis",
    "Interview PECT",
    "Network config",
    "work",
    "daily",
    "inbox",
]

# ---------------------------------------------------------------------------
# Secret patterns (case-sensitive; shapes from the binding operator decisions)
# ---------------------------------------------------------------------------

# API-key / secret shapes: OpenAI (sk-), OpenRouter (sk-or-), Slack (xoxb-...),
# and AWS access-key IDs (AKIA + 16 uppercase alnum). A generic high-entropy
# heuristic below catches anything else of unknown shape.
SECRET_PATTERNS = [
    ("openai", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("openrouter", re.compile(r"sk-or-[A-Za-z0-9]{20,}")),
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
]

# Candidate "token-like" runs (base64/hex/url-safe alphabet) longer than this
# are scored for entropy. 24 chars is comfortably above any real English word
# run in the fixture (longest is ~21), so normal prose never trips it.
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_MIN_ENTROPY = 4.5   # bits/char: hex is ~4.0, base64 ~5.5-6, prose ~4.2
_MIN_DISTINCT = 12   # a real secret has many distinct characters


def _shannon_entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def high_entropy_hits(text: str) -> list[str]:
    """Return token-like substrings of ``text`` that look like high-entropy secrets."""
    hits: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if len(tok) < 24:
            continue
        if len(set(tok)) < _MIN_DISTINCT:
            continue
        if _shannon_entropy(tok) < _MIN_ENTROPY:
            continue
        hits.append(tok)
    return hits


# ---------------------------------------------------------------------------
# Deny-list matching helpers
# ---------------------------------------------------------------------------

def _path_segment_stems(rel_path: str) -> set[str]:
    norm = posixpath.normpath(rel_path)
    stems: set[str] = set()
    for seg in norm.split("/"):
        low = seg.lower()
        if low.endswith(".html"):
            stems.add(low[:-5])
        elif low.endswith(".md"):
            stems.add(low[:-3])
        else:
            stems.add(low)
    return stems


def _path_deny_hit(rel_path: str, deny_basenames: list[str]) -> str | None:
    norm = posixpath.normpath(rel_path)
    lower = norm.lower()
    stems = _path_segment_stems(rel_path)
    for name in deny_basenames:
        bl = name.lower()
        if bl in stems:
            return name
        # multi-word names also match as a substring of the path (defence in
        # depth against e.g. "Token Checker (backup).html")
        if " " in name and bl in lower:
            return name
    return None


def _content_deny_names(deny_basenames: list[str]) -> list[str]:
    # Only distinctive multi-word names are safe to scan inside bytes;
    # single words like "work"/"daily"/"inbox" would false-positive on prose.
    return [name for name in deny_basenames if " " in name]


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def check_artifact(
    vault: Path,
    artifact: Path,
    allowlist: list[str] | None,
    deny_basenames: list[str] | None = None,
    secret_patterns: list[tuple[str, re.Pattern]] | None = None,
) -> list[str]:
    """Return a list of human-readable problems; empty means the artifact is clean.

    ``vault`` is used to confirm each emitted note page maps back to a source
    note that both exists and is allowlisted.
    """
    if deny_basenames is None:
        deny_basenames = DEFAULT_DENY_BASENAMES
    if secret_patterns is None:
        secret_patterns = SECRET_PATTERNS

    problems: list[str] = []

    files = sorted(p for p in artifact.rglob("*") if p.is_file())

    # 1. Allowlist assertion: every emitted file is a generated asset or maps
    #    to an allowlisted, existing source note.
    for p in files:
        rel = p.relative_to(artifact).as_posix()
        if rel in GENERATED_ASSETS:
            continue
        if rel.endswith(".html"):
            src = rel[:-5] + ".md"
            if is_allowed(src, allowlist) and (vault / src).is_file():
                continue
            problems.append(
                f"allowlist violation: {rel} does not map to an allowlisted, "
                f"existing note (source {src})"
            )
            continue
        problems.append(f"allowlist violation: unexpected non-note file {rel}")

    # 2. Deny-list scans over every file's path and bytes.
    content_deny = _content_deny_names(deny_basenames)
    for p in files:
        rel = p.relative_to(artifact).as_posix()

        hit = _path_deny_hit(rel, deny_basenames)
        if hit:
            problems.append(f"deny-list basename '{hit}' in artifact path {rel}")

        data = p.read_bytes()
        text = data.decode("utf-8", errors="replace")

        for name, pat in secret_patterns:
            if pat.search(text):
                problems.append(f"secret pattern '{name}' matched in {rel}")

        for tok in high_entropy_hits(text):
            problems.append(
                f"high-entropy secret candidate in {rel} (ends ...{tok[-8:]})"
            )

        for name in content_deny:
            if name.lower() in text.lower():
                problems.append(f"deny-list basename '{name}' found in content of {rel}")

    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed pre-publish leak check (Phase 3).")
    parser.add_argument("vault", help="path to the vault (or fixture) directory")
    parser.add_argument("artifact", help="path to the built (staging) directory")
    parser.add_argument("--allowlist", action="append", default=None,
                        help="allowlist prefix (repeatable); default web/")
    parser.add_argument("--allowlist-file", default=None,
                        help="flat allowlist file (one prefix per line)")
    parser.add_argument("--deny", action="append", default=None,
                        help="extra sensitive basename to deny (repeatable)")
    args = parser.parse_args(argv)

    if args.allowlist_file:
        allowlist = parse_allowlist_file(Path(args.allowlist_file))
    elif args.allowlist is not None:
        allowlist = args.allowlist
    else:
        allowlist = DEFAULT_ALLOWLIST

    deny = list(DEFAULT_DENY_BASENAMES)
    if args.deny:
        deny.extend(args.deny)

    problems = check_artifact(Path(args.vault), Path(args.artifact), allowlist,
                              deny_basenames=deny)
    for prob in problems:
        print(f"leak-check: {prob}", file=sys.stderr)
    if problems:
        print(f"leak-check: {len(problems)} problem(s); artifact rejected",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
