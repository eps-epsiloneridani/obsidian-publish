"""Phase 1 + 2 + 3 tests for build.py.

Phase 1 covers:
- markdown -> HTML render correctness (unit + golden output)
- wikilink resolution across subfolders (relative paths)
- frontmatter -> title
- embed handling (exactly one in the fixture)
- broken-link detection (non-zero exit)

Phase 2 covers:
- backlinks panel (who links here), including the zero-backlinks orphan case
- dependency-free static-SVG graph (nodes == pages, edges == links, in-frame)
- search-index.json (every page, matching client-side paths)
- responsive CSS + self-contained client-side search.js
- no external (http/https) asset references anywhere in the output

Phase 3 covers:
- deny-by-default allowlist (web/ only): disallowed notes never emitted
- sidebar navigation listing only allowlisted notes
- index.html home page (Home note becomes index.html; otherwise generated)
- (the leak-check and atomic-publish pipeline are tested in test_leak_check.py)
"""

import html as html_mod
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build.py"
VAULT = REPO / "tests" / "fixtures" / "vault"
BROKEN = REPO / "tests" / "fixtures" / "broken"
GOLDEN = REPO / "tests" / "golden"

sys.path.insert(0, str(REPO))
import build as build_mod  # noqa: E402


def _run(vault: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BUILD), str(vault), "--out", str(out)],
        capture_output=True,
        text=True,
    )


def _build_in_memory(vault: Path, out: Path):
    builder = build_mod.Builder(vault, out)
    broken = builder.build()
    builder.write()
    return builder, broken


def _published(builder) -> list:
    return [n for n in builder.notes if n.allowed]


def _backlinks_section(html: str) -> str:
    m = re.search(r'<aside class="backlinks">(.*?)</aside>', html, re.S)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Render correctness (unit)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "markdown,expected",
    [
        ("**bold**", "<strong>bold</strong>"),
        ("*italic*", "<em>italic</em>"),
        ("`code`", "<code>code</code>"),
        ("plain text with <angle> & \"quote\"", "plain text with &lt;angle&gt; &amp; &quot;quote&quot;"),
        ("snake_case_left_alone", "snake_case_left_alone"),
        ("_word_", "<em>word</em>"),
        ("**a** and *b* and `c`", "<strong>a</strong> and <em>b</em> and <code>c</code>"),
    ],
)
def test_render_inline(markdown, expected):
    resolver = build_mod.Builder(Path("."), Path("."))
    assert build_mod.render_inline(markdown, resolver) == expected


def test_render_heading():
    resolver = build_mod.Builder(Path("."), Path("."))
    out = build_mod.render_blocks(["# Title", "", "## Sub"], resolver)
    assert "<h1>Title</h1>" in out
    assert "<h2>Sub</h2>" in out


def test_render_fenced_code_escaped():
    resolver = build_mod.Builder(Path("."), Path("."))
    out = build_mod.render_blocks(["```", "<b>x</b> & y", "```"], resolver)
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in out


# ---------------------------------------------------------------------------
# Golden output (full tree, exact bytes)
# ---------------------------------------------------------------------------

def _produced_files(out: Path) -> list[str]:
    return sorted(
        p.relative_to(out).as_posix()
        for p in out.rglob("*")
        if p.is_file()
    )


def test_golden_output(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []

    produced = _produced_files(tmp_path / "out")
    expected = _produced_files(GOLDEN)
    assert produced == expected

    for rel in produced:
        got = (tmp_path / "out" / rel).read_text(encoding="utf-8")
        want = (GOLDEN / rel).read_text(encoding="utf-8")
        assert got == want, f"golden mismatch for {rel}"


def test_every_allowed_note_has_a_page(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []
    html_paths = {p for p in _produced_files(tmp_path / "out") if p.endswith(".html")}
    for n in builder.notes:
        if not n.allowed:
            continue
        expected = "index.html" if n is builder.home_note else n.out_rel
        assert expected in html_paths, n.rel_path
    # no page for disallowed notes
    assert not any(p.startswith("private/") for p in html_paths)


def test_fixture_note_count():
    assert len(list((VAULT / "web").rglob("*.md"))) == 12
    assert len(list(VAULT.rglob("*.md"))) == 14  # 12 web + 2 private (disallowed)


# ---------------------------------------------------------------------------
# Wikilink resolution across subfolders (relative paths)
# ---------------------------------------------------------------------------

def test_wikilink_resolution_relative_links(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []

    home = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    # home (rendered at site root) -> top-level web/ note
    assert 'href="web/Project%20Phoenix.html"' in home
    # home -> subfolder
    assert 'href="web/notes/Standup%20-%202026-11-06.html"' in home
    assert 'href="web/archive/Old%20Notes.html"' in home
    # parentheses are percent-encoded
    assert 'href="web/README%20%28draft%29.html"' in home

    meeting = (tmp_path / "out" / "web" / "notes" / "Meeting Notes.html").read_text(
        encoding="utf-8")
    # subfolder -> home (site root)
    assert 'href="../../index.html"' in meeting
    # subfolder -> other subfolder
    assert 'href="../some%20day/The%20Future.html"' in meeting

    phoenix = (tmp_path / "out" / "web" / "Project Phoenix.html").read_text(
        encoding="utf-8")
    # ampersand in filename
    assert 'href="Ideas%20%26%20Ramblings.html"' in phoenix
    # heading fragment
    assert 'href="notes/Meeting%20Notes.html#decisions"' in phoenix
    # web/ note -> home goes up to the site root
    assert 'href="../index.html"' in phoenix


def test_wikilink_alias(tmp_path):
    builder, _ = _build_in_memory(VAULT, tmp_path / "out")
    page = (tmp_path / "out" / "web" / "Ideas & Ramblings.html").read_text(
        encoding="utf-8")
    assert '<a href="Project%20Phoenix.html">the phoenix project</a>' in page


# ---------------------------------------------------------------------------
# Frontmatter -> title
# ---------------------------------------------------------------------------

def test_frontmatter_title(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")
    page = (tmp_path / "out" / "web" / "archive" / "Interview prep.html").read_text(
        encoding="utf-8")
    assert "<title>Interview Preparation</title>" in page
    assert "<h1>Interview Preparation</h1>" in page


def test_frontmatter_not_in_body(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")
    page = (tmp_path / "out" / "web" / "archive" / "Interview prep.html").read_text(
        encoding="utf-8")
    assert "topic:" not in page
    assert "---" not in page.split("</header>")[1]


# ---------------------------------------------------------------------------
# Embed handling
# ---------------------------------------------------------------------------

def test_exactly_one_embed(tmp_path):
    builder, _ = _build_in_memory(VAULT, tmp_path / "out")
    home = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert home.count('class="embed"') == 1
    assert '<a class="embed" href="web/Project%20Phoenix.html">Project Phoenix</a>' in home


# ---------------------------------------------------------------------------
# Backlinks panel (Phase 2)
# ---------------------------------------------------------------------------

def test_backlinks_hub(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")
    home = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    section = _backlinks_section(home)
    assert section.count("<li>") == 6
    assert 'href="web/Contact%20-%20John%27s%20list.html">Contact - John\'s list</a>' in section
    assert 'href="web/notes/Meeting%20Notes.html">Meeting Notes</a>' in section
    assert 'href="web/archive/Interview%20prep.html">Interview Preparation</a>' in section


def test_backlinks_frontmatter_title_used(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")
    phoenix = (tmp_path / "out" / "web" / "Project Phoenix.html").read_text(
        encoding="utf-8")
    section = _backlinks_section(phoenix)
    assert section.count("<li>") == 3
    assert 'href="../index.html">Home</a>' in section
    assert 'href="Ideas%20%26%20Ramblings.html">Ideas &amp; Ramblings</a>' in section


def test_backlinks_orphan_zero(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")
    orphan = (tmp_path / "out" / "web" / "Orphan Note.html").read_text(encoding="utf-8")
    section = _backlinks_section(orphan)
    assert section.count("<li>") == 0
    assert "No notes link here yet." in section


# ---------------------------------------------------------------------------
# Search index (Phase 2 + 3)
# ---------------------------------------------------------------------------

def test_search_index_contains_all_published_pages(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []
    idx = json.loads((tmp_path / "out" / "search-index.json").read_text(encoding="utf-8"))
    published = _published(builder)
    assert len(idx) == len(published)
    assert {e["title"] for e in idx} == {n.title for n in published}
    assert {e["path"] for e in idx} == {
        urllib.parse.quote(n.url_path) for n in published
    }
    # the orphan is indexed too (search corpus == published pages)
    assert "Orphan Note" in {e["title"] for e in idx}
    # the home note maps to index.html
    assert "index.html" in {e["path"] for e in idx}


def test_search_index_text_is_frontmatter_stripped(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")
    idx = json.loads((tmp_path / "out" / "search-index.json").read_text(encoding="utf-8"))
    interview = next(e for e in idx if e["title"] == "Interview Preparation")
    assert "topic:" not in interview["text"]


# ---------------------------------------------------------------------------
# Graph (Phase 2 + 3)
# ---------------------------------------------------------------------------

def test_graph_nodes_and_edges(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []
    graph = (tmp_path / "out" / "graph.html").read_text(encoding="utf-8")
    published = _published(builder)

    assert graph.count('class="graph-node-circle"') == len(published)
    assert graph.count('class="graph-edge"') == len(builder.edges)

    coords = re.findall(
        r'<circle class="graph-node-circle" cx="(-?\d+(?:\.\d+)?)" cy="(-?\d+(?:\.\d+)?)"',
        graph)
    assert len(coords) == len(published)
    for cx, cy in coords:
        assert 0.0 <= float(cx) <= 1200.0
        assert 0.0 <= float(cy) <= 800.0

    m = re.search(
        r'<script type="application/json" id="graph-data">(.*?)</script>', graph, re.S)
    assert m
    data = json.loads(m.group(1))
    assert len(data["nodes"]) == len(published)
    assert len(data["edges"]) == len(builder.edges)

    ids = {n["id"] for n in data["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in data["edges"])

    orphan = next(n for n in data["nodes"] if n["id"] == "web/Orphan Note.md")
    assert orphan["degree"] == 0
    home = next(n for n in data["nodes"] if n["id"] == "web/Home.md")
    assert home["url"] == "index.html"


def test_graph_edge_set_matches_builder(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []
    graph = (tmp_path / "out" / "graph.html").read_text(encoding="utf-8")
    m = re.search(
        r'<script type="application/json" id="graph-data">(.*?)</script>', graph, re.S)
    data = json.loads(m.group(1))
    assert {(e["source"], e["target"]) for e in data["edges"]} == set(builder.edges)


# ---------------------------------------------------------------------------
# Assets + no external references (Phase 2)
# ---------------------------------------------------------------------------

def test_assets_copied_and_self_contained(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")
    css = (tmp_path / "out" / "style.css").read_text(encoding="utf-8")
    js = (tmp_path / "out" / "search.js").read_text(encoding="utf-8")

    assert "@media (max-width: 600px)" in css
    assert "search-index.json" in js
    assert "fetch(" in js
    assert "https://" not in css and "http://" not in css
    assert "https://" not in js and "http://" not in js


def test_no_external_asset_references(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")

    tag_re = re.compile(
        r'<(?:script|link|img|source|iframe|embed)\b[^>]*\b(?:src|href)\s*=\s*["\']https?://'
    )
    import_re = re.compile(r'@import\s+["\']?https?://')
    url_re = re.compile(r'url\(["\']?https?://')

    for path in (tmp_path / "out").rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".html", ".css", ".js"):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        assert not tag_re.search(content), f"external asset tag in {path}"
        if path.suffix.lower() == ".css":
            assert not import_re.search(content), f"external @import in {path}"
            assert not url_re.search(content), f"external url() in {path}"


# ---------------------------------------------------------------------------
# Broken-link detection (non-zero exit)
# ---------------------------------------------------------------------------

def test_broken_link_surfaces_and_nonzero_exit(tmp_path):
    proc = _run(BROKEN, tmp_path / "out")
    assert proc.returncode != 0
    assert "Nowhere Land" in proc.stderr
    assert "1 unresolved wikilink" in proc.stderr


def test_broken_link_build_still_writes_output(tmp_path):
    builder, broken = _build_in_memory(BROKEN, tmp_path / "out")
    assert broken == [("web/Good Note.md", "Nowhere Land")]
    # output is still produced by a direct build so a partial build is inspectable
    assert (tmp_path / "out" / "web" / "Good Note.html").exists()
    assert (tmp_path / "out" / "web" / "Another Good.html").exists()
    good = (tmp_path / "out" / "web" / "Good Note.html").read_text(encoding="utf-8")
    assert 'class="broken-link"' in good
    assert (tmp_path / "out" / "graph.html").exists()
    assert (tmp_path / "out" / "search-index.json").exists()


def test_clean_build_exits_zero(tmp_path):
    proc = _run(VAULT, tmp_path / "out")
    assert proc.returncode == 0
    assert proc.stderr == ""


# ---------------------------------------------------------------------------
# Phase 3: deny-by-default filter, sidebar, index.html
# ---------------------------------------------------------------------------

def test_filter_excludes_disallowed_notes(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []
    allowed = {n.rel_path for n in builder.notes if n.allowed}
    assert all(p.startswith("web/") for p in allowed)
    disallowed = {n.rel_path for n in builder.notes if not n.allowed}
    assert "private/Token Checker.md" in disallowed
    assert "private/ADHD Diagnosis.md" in disallowed


def test_no_disallowed_content_in_output(tmp_path):
    _build_in_memory(VAULT, tmp_path / "out")
    for p in (tmp_path / "out").rglob("*"):
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            assert "sk-" not in text, f"leaked secret in {p}"
            assert "Token Checker" not in text, f"leaked basename in {p}"
            assert "ADHD Diagnosis" not in text, f"leaked basename in {p}"


def test_sidebar_lists_only_allowlisted_notes(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []
    page = (tmp_path / "out" / "web" / "Project Phoenix.html").read_text(encoding="utf-8")
    m = re.search(r'<aside class="sidebar">(.*?)</aside>', page, re.S)
    assert m
    sidebar_text = html_mod.unescape(m.group(1))
    published_titles = {n.title for n in builder.notes if n.allowed}
    for t in published_titles:
        assert t in sidebar_text, f"missing {t} in sidebar"
    assert "Token Checker" not in sidebar_text
    assert "ADHD Diagnosis" not in sidebar_text


def test_home_note_becomes_index(tmp_path):
    builder, broken = _build_in_memory(VAULT, tmp_path / "out")
    assert broken == []
    assert builder.home_note is not None
    assert builder.home_note.stem == "Home"
    idx = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert "<title>Home</title>" in idx
    assert "<h1>Home</h1>" in idx
    # the home note is not emitted as its own page
    assert not (tmp_path / "out" / "web" / "Home.html").exists()


def test_index_generated_when_no_home_note(tmp_path):
    v = tmp_path / "vault"
    (v / "web").mkdir(parents=True)
    (v / "web" / "Alpha.md").write_text("# Alpha\n\nFirst note.\n", encoding="utf-8")
    (v / "web" / "Beta.md").write_text("# Beta\n\nSecond note.\n", encoding="utf-8")
    builder, broken = _build_in_memory(v, tmp_path / "out")
    assert broken == []
    assert builder.home_note is None
    idx = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert "<h1>Notes</h1>" in idx
    assert "All published notes" in idx
    assert 'href="web/Alpha.html"' in idx
    assert 'href="web/Beta.html"' in idx

# ---------------------------------------------------------------------------
# Sidebar/index ordering: home first, then newest published first
# ---------------------------------------------------------------------------

def test_sidebar_sort_home_first_then_newest(tmp_path):
    vault = tmp_path / "vault"
    (vault / "web").mkdir(parents=True)
    (vault / "web/Home.md").write_text("# Home\n")
    (vault / "web/Old.md").write_text("---\npublished: 2024-01-01\n---\n# Old\n")
    (vault / "web/Mid.md").write_text("---\npublished: 2025-06-06\n---\n# Mid\n")
    (vault / "web/New.md").write_text("---\npublished: 2026-05-05\n---\n# New\n")
    # Undated note falls back to file mtime (now) -> newest of all.
    (vault / "web/Undated.md").write_text("# Undated\n")

    builder = build_mod.Builder(vault, tmp_path / "out")
    assert builder.build() == []
    html = builder.sidebar_html("")
    order = re.findall(r"<li><a href=\"[^\"]*\">([^<]*)</a></li>", html)
    assert order == ["Home", "Undated", "New", "Mid", "Old"], order

    listing = builder._index_listing()
    order2 = re.findall(r"<li><a href=\"[^\"]*\">([^<]*)</a></li>", listing)
    assert order2 == ["Home", "Undated", "New", "Mid", "Old"], order2


def test_frontmatter_date_beats_mtime_for_sort(tmp_path):
    # A note with an old frontmatter date must sort BELOW a newer-dated note
    # even if its file mtime is the newest.
    import os
    vault = tmp_path / "vault"
    (vault / "web").mkdir(parents=True)
    (vault / "web/Home.md").write_text("# Home\n")
    (vault / "web/Recent Note.md").write_text(
        "---\npublished: 2026-11-01\n---\n# Recent\n")
    old = vault / "web/Ancient.md"
    old.write_text("---\npublished: 2020-01-01\n---\n# Ancient\n")
    os.utime(old, (2_000_000_000, 2_000_000_000))  # mtime far in the future

    builder = build_mod.Builder(vault, tmp_path / "out")
    assert builder.build() == []
    html = builder.sidebar_html("")
    order = re.findall(r"<li><a href=\"[^\"]*\">([^<]*)</a></li>", html)
    assert order == ["Home", "Recent Note", "Ancient"], order
