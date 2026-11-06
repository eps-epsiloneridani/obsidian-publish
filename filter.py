#!/usr/bin/env python3
"""Shared deny-by-default allowlist rules for the publish pipeline (Phase 3).

This module is the single source of truth for:

- the default publish scope (the ``web/`` subfolder only, per the binding
  operator decision), and
- the list of generated (non-note) artifact files.

Both ``build.py`` (which filters what gets indexed/rendered) and
``leak_check.py`` (which independently re-verifies the artifact) import from
here, so the two halves can never drift apart. Pure stdlib, no dependencies.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

# Deny-by-default publish scope: only notes under this folder are published.
# A note matches if its normalised relative path equals the prefix or starts
# with ``prefix + "/"``. The binding operator decision fixes this to ``web/``.
DEFAULT_ALLOWLIST = ["web/"]

# Generated site-level files (produced by build.py from allowlisted notes plus
# hand-authored assets). The leak check treats these as always-allowlisted --
# they are not note pages and so have no source note to map back to.
GENERATED_ASSETS = frozenset({
    "index.html",
    "graph.html",
    "robots.txt",
    "search-index.json",
    "style.css",
    "search.js",
})


def is_allowed(rel_path: str, allowlist: list[str] | None) -> bool:
    """Return True if ``rel_path`` (a posix path like ``web/foo.md``) is in scope.

    An empty or missing allowlist -- or a ``""`` prefix -- means "publish
    everything" (the legacy dev default). A prefix ``web`` allows the folder
    ``web/`` and everything beneath it, but not a file named ``web.md``.
    """
    if not allowlist or "" in allowlist:
        return True
    rp = posixpath.normpath(rel_path)
    for prefix in allowlist:
        p = posixpath.normpath(prefix).strip("/")
        if p == "":
            return True
        if rp == p or rp.startswith(p + "/"):
            return True
    return False


def parse_allowlist_file(path: Path) -> list[str]:
    """Parse a flat allowlist file: one prefix per line.

    Tolerates ``#`` comments, ``- `` list markers and surrounding quotes so a
    hand-written ``publish-allowlist.yaml`` list-of-folders works without a
    YAML library. Not a general YAML parser.
    """
    prefixes: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        line = line.strip("'\"")
        if line:
            prefixes.append(line)
    return prefixes
