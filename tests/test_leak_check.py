"""Phase 3 leak-check + atomic-publish pipeline tests.

Covers:
- leak-check clean artifact -> zero problems / exit 0
- leak-check rejects a seeded sk- secret (inside the allowlist) -> non-zero
- leak-check rejects a high-entropy secret blob
- leak-check rejects an allowlist violation (a disallowed note page in the artifact)
- leak-check rejects a sensitive basename (path deny-list)
- atomic swap: a forced failure leaves the prior out/ tree byte-identical
- successful publish swaps the tree and leaves no staging debris
- search index and graph contain zero disallowed notes (test-asserted)
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VAULT = REPO / "tests" / "fixtures" / "vault"
LEAK = REPO / "tests" / "fixtures" / "leak"
BOUNDARY = REPO / "tests" / "fixtures" / "boundary"

sys.path.insert(0, str(REPO))
import build as build_mod  # noqa: E402
import leak_check  # noqa: E402
from filter import DEFAULT_ALLOWLIST  # noqa: E402


def _snapshot(path: Path) -> dict:
    out = {}
    for p in sorted(path.rglob("*")):
        if p.is_file():
            out[p.relative_to(path).as_posix()] = p.read_bytes()
    return out


def _build_clean(tmp_path: Path) -> Path:
    builder = build_mod.Builder(VAULT, tmp_path / "out")
    broken = builder.build()
    assert broken == []
    builder.write()
    return tmp_path / "out"


# ---------------------------------------------------------------------------
# Leak check: clean
# ---------------------------------------------------------------------------

def test_leak_check_clean_zero(tmp_path):
    out = _build_clean(tmp_path)
    problems = leak_check.check_artifact(VAULT, out, DEFAULT_ALLOWLIST)
    assert problems == []


def test_leak_check_cli_clean_exit_zero(tmp_path):
    out = _build_clean(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(REPO / "leak_check.py"), str(VAULT), str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_leak_check_cli_exits_nonzero_on_secret(tmp_path):
    out = tmp_path / "out"
    builder = build_mod.Builder(LEAK, out)
    broken = builder.build()
    assert broken == []
    builder.write()
    proc = subprocess.run(
        [sys.executable, str(REPO / "leak_check.py"), str(LEAK), str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "secret pattern 'openai'" in proc.stderr


def test_leak_check_cli_exits_nonzero_on_allowlist_violation(tmp_path):
    out = _build_clean(tmp_path)
    (out / "private").mkdir()
    (out / "private" / "Leak.html").write_text("<html></html>", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(REPO / "leak_check.py"), str(VAULT), str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "allowlist violation" in proc.stderr


# ---------------------------------------------------------------------------
# Leak check: rejections
# ---------------------------------------------------------------------------

def test_leak_check_rejects_seeded_sk(tmp_path):
    out = tmp_path / "out"
    builder = build_mod.Builder(LEAK, out)
    broken = builder.build()
    assert broken == []
    builder.write()
    problems = leak_check.check_artifact(LEAK, out, DEFAULT_ALLOWLIST)
    assert any("secret pattern 'openai'" in p for p in problems)
    assert any("high-entropy" in p for p in problems)


def test_publish_rejects_seeded_sk(tmp_path):
    out = tmp_path / "out"
    code, problems = build_mod.publish(LEAK, out)
    assert code == 1
    assert any("secret pattern 'openai'" in p for p in problems)
    # nothing is published and the staging tree is discarded
    assert not out.exists()
    assert not (tmp_path / "out.staging").exists()


def test_leak_check_allowlist_violation(tmp_path):
    out = _build_clean(tmp_path)
    # simulate a filter bug: a disallowed note page leaked into the artifact
    (out / "private").mkdir()
    (out / "private" / "Leak.html").write_text("<html></html>", encoding="utf-8")
    problems = leak_check.check_artifact(VAULT, out, DEFAULT_ALLOWLIST)
    assert any("allowlist violation" in p and "private/Leak.html" in p for p in problems)


def test_leak_check_deny_basename(tmp_path):
    v = tmp_path / "vault"
    (v / "web").mkdir(parents=True)
    (v / "web" / "Token Checker.md").write_text(
        "# Token Checker\n\nNothing secret.\n", encoding="utf-8")
    out = tmp_path / "out"
    builder = build_mod.Builder(v, out)
    broken = builder.build()
    assert broken == []
    builder.write()
    problems = leak_check.check_artifact(v, out, DEFAULT_ALLOWLIST)
    assert any("deny-list basename 'Token Checker'" in p for p in problems)


def test_high_entropy_heuristic(tmp_path):
    blob = "dGVzdHNlY3JldHRva2VudGhhdGlzYWJhc2U2NGVuY29kZWQxMjM0NTY3ODkwQUJDREVGRw=="
    hits = leak_check.high_entropy_hits(f"here is a token {blob} end")
    assert hits
    assert any(blob in h or h in blob for h in hits)
    # normal prose never trips the heuristic
    assert leak_check.high_entropy_hits(
        "The quick brown fox jumps over the lazy dog.") == []


# ---------------------------------------------------------------------------
# Atomic publish
# ---------------------------------------------------------------------------

def test_publish_success_swaps_and_leaves_no_debris(tmp_path):
    out = tmp_path / "out"
    code, problems = build_mod.publish(VAULT, out)
    assert code == 0
    assert problems == []
    assert (out / "index.html").exists()
    assert (out / "search-index.json").exists()
    assert (out / "graph.html").exists()
    assert not (tmp_path / "out.staging").exists()
    assert not (tmp_path / "out.old").exists()


def test_atomic_swap_preserves_prior_out_on_failure(tmp_path):
    out = tmp_path / "out"
    code, problems = build_mod.publish(VAULT, out)
    assert code == 0 and problems == []
    before = _snapshot(out)

    # force a failure mid-pipeline (a leak) and confirm out/ is untouched
    code, problems = build_mod.publish(LEAK, out)
    assert code == 1
    assert any("secret pattern 'openai'" in p for p in problems)

    after = _snapshot(out)
    assert before == after  # byte-identical

    assert not (tmp_path / "out.staging").exists()
    assert not (tmp_path / "out.old").exists()


def test_atomic_swap_preserves_prior_out_on_broken_link(tmp_path):
    out = tmp_path / "out"
    code, problems = build_mod.publish(VAULT, out)
    assert code == 0 and problems == []
    before = _snapshot(out)

    code, problems = build_mod.publish(BOUNDARY, out)
    assert code == 1
    assert any("unresolved wikilink" in p for p in problems)

    after = _snapshot(out)
    assert before == after


# ---------------------------------------------------------------------------
# Search index + graph contain zero disallowed notes
# ---------------------------------------------------------------------------

def test_search_index_and_graph_exclude_disallowed(tmp_path):
    out = _build_clean(tmp_path)

    idx = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
    titles = [e["title"] for e in idx]
    paths = [e["path"] for e in idx]
    assert "Token Checker" not in titles
    assert "ADHD Diagnosis" not in titles
    assert all(not p.startswith("private/") for p in paths)

    graph = (out / "graph.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="graph-data">(.*?)</script>', graph, re.S)
    assert m
    data = json.loads(m.group(1))
    ids = {n["id"] for n in data["nodes"]}
    assert all(not i.startswith("private/") for i in ids)
    labels = {n["label"] for n in data["nodes"]}
    assert "Token Checker" not in labels
    assert "ADHD Diagnosis" not in labels
