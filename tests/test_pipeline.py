"""Phase 5 pipeline tests for deploy.sh.

Exercises the republish pipeline end-to-end against a LOCAL rsync target (never
a real host). ``NOTES_RSYNC_TARGET`` is pointed at a temp directory so rsync runs
``rsync -az --delete`` to a local path -- no ssh, no network. The deploy key and
any real host are never touched.

Covers:
- end-to-end: deploy.sh builds the fixture vault and publishes the correct tree
  to the local rsync target (target == built out/ tree, byte-for-byte)
- fail-closed: a seeded-leak vault makes deploy.sh exit non-zero and leaves the
  prior target tree byte-identical (rsync never ran)
- --dry-run: build + leak-check run, but the target is never created/touched
- flock: a run that loses the lock exits 0 cleanly and corrupts nothing
"""

import fcntl
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy.sh"
VAULT = REPO / "tests" / "fixtures" / "vault"
LEAK = REPO / "tests" / "fixtures" / "leak"


def _env(tmp_path: Path, **overrides) -> dict:
    """Test environment: point every pipeline knob at tmp_path, disable the
    real config.env (tests never read the deploy key or the live vault), and
    use a local rsync target so no real host is ever contacted."""
    env = os.environ.copy()
    env.update({
        "NOTES_VAULT": str(VAULT),
        "NOTES_OUT": str(tmp_path / "out"),
        "NOTES_RSYNC_TARGET": str(tmp_path / "target") + "/",
        "NOTES_LOCK": str(tmp_path / "deploy.lock"),
        "NOTES_CONFIG_FILE": str(tmp_path / "no-config.env"),
    })
    env.update(overrides)
    return env


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DEPLOY), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _snapshot(path: Path) -> dict:
    """Byte snapshot of a directory tree: {rel_posix_path: bytes}."""
    if not path.exists():
        return {}
    return {
        p.relative_to(path).as_posix(): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# End-to-end (local rsync target)
# ---------------------------------------------------------------------------

def test_deploy_end_to_end_local(tmp_path):
    proc = _run(_env(tmp_path))
    assert proc.returncode == 0, proc.stderr

    out = _snapshot(tmp_path / "out")
    target = _snapshot(tmp_path / "target")
    # the target mirrors the built tree exactly (same files, same bytes)
    assert out == target
    assert out, "publish produced an empty tree"

    # sanity: the fixture's key generated files landed in the target
    assert "index.html" in out
    assert "search-index.json" in out
    assert "graph.html" in out
    assert "style.css" in out
    assert any(rel.endswith(".html") for rel in out)

    # and no leak/private content escaped into the target
    for rel, data in target.items():
        text = data.decode("utf-8", errors="replace")
        assert "sk-" not in text, f"secret in {rel}"
        assert "Token Checker" not in text, f"deny basename in {rel}"


# ---------------------------------------------------------------------------
# Fail-closed: seeded leak leaves the prior target byte-identical
# ---------------------------------------------------------------------------

def test_deploy_fail_closed_leak_preserves_target(tmp_path):
    env = _env(tmp_path)
    assert _run(env).returncode == 0  # seed a clean publish + target

    before_target = _snapshot(tmp_path / "target")
    before_out = _snapshot(tmp_path / "out")

    leak_proc = _run(_env(tmp_path, NOTES_VAULT=str(LEAK)))
    assert leak_proc.returncode != 0
    assert "secret" in leak_proc.stderr.lower()

    # rsync never ran: the prior target tree is byte-identical
    assert _snapshot(tmp_path / "target") == before_target
    # the build's atomic swap also left out/ on the previous good tree
    assert _snapshot(tmp_path / "out") == before_out

    # no staging debris
    assert not (tmp_path / "out.staging").exists()
    assert not (tmp_path / "out.old").exists()


# ---------------------------------------------------------------------------
# --dry-run builds + leak-checks but never touches the target
# ---------------------------------------------------------------------------

def test_deploy_dry_run_leaves_target_untouched(tmp_path):
    env = _env(tmp_path)
    proc = _run(env, "--dry-run")
    assert proc.returncode == 0, proc.stderr

    # the build happened (a complete, leak-checked tree was produced) ...
    out = _snapshot(tmp_path / "out")
    assert "index.html" in out
    assert "search-index.json" in out

    # ... but the target was never created
    assert not (tmp_path / "target").exists()


# ---------------------------------------------------------------------------
# flock: a second concurrent run exits cleanly and corrupts nothing
# ---------------------------------------------------------------------------

def test_deploy_flock_second_run_exits_cleanly(tmp_path):
    env = _env(tmp_path)
    lock_path = Path(env["NOTES_LOCK"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # hold the lock out-of-band, simulating an in-flight publish
    fh = open(lock_path, "w")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        proc = _run(env)
        assert proc.returncode == 0
        assert "already in progress" in proc.stderr
        # the skipped run built and published nothing
        assert not (tmp_path / "out").exists()
        assert not (tmp_path / "target").exists()
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
