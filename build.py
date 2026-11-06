#!/usr/bin/env python3
"""Obsidian vault -> static HTML generator (Phase 3).

Renders ``.md`` files under an allowlisted subfolder of a vault (or fixture)
into a static HTML site. Resolves Obsidian wikilinks (``[[Note Name]]``) across
subfolders and surfaces any that cannot be resolved -- including links that
point at a note outside the publish scope -- by reporting them and exiting
non-zero.

Phase 2 additions (all client-side-only static output, no external resources):
- **Backlinks** — an inverted link graph rendered as a "Linked here" panel on
  every page.
- **Graph** — a dependency-free, hand-computed static SVG of the whole site
  (``graph.html``). The node/edge data is also embedded as JSON so a vendored
  JS graph library can be swapped in later without touching the data model.
- **Search** — ``search-index.json`` built at build time plus a small,
  self-authored client-side JS search (``assets/search.js``), no libraries.
- **Responsive CSS** — ``assets/style.css``, system fonts only, no framework.

Phase 3 additions (deny-by-default publish, fail-closed):
- **Content filter** — only notes under the allowlist (default ``web/``) are
  indexed, rendered, searched, graphed or listed. A note outside the allowlist
  never appears in any output.
- **Sidebar + home page** — every page carries a sidebar of allowlisted notes,
  and ``index.html`` is the home page (a ``Home``/``index`` note becomes it;
  otherwise a generated note list is used).
- **Leak check + atomic publish** — ``publish()`` builds into a staging tree,
  runs the mandatory ``leak_check`` (allowlist assertion + secret deny-list),
  and only on a clean result atomically swaps it into ``out``. On any failure
  the previous ``out`` is left byte-for-byte untouched.

Pure Python 3 stdlib. No third-party dependencies, no network access.

Usage::

    python3 build.py VAULT_PATH [--out OUT_DIR] [--allowlist web/]...
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import posixpath
import random
import re
import shutil
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from filter import DEFAULT_ALLOWLIST, is_allowed, parse_allowlist_file
from leak_check import check_artifact

# Directory holding the hand-authored client-side assets (copied verbatim to
# the output on every build).
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# Characters that must be escaped in HTML text. Backtick, brackets, parens,
# asterisks and underscores are deliberately NOT escaped here: they are the
# markdown delimiters the inline renderer still has to act on.
_ESCAPE_TABLE = str.maketrans({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
})


def escape_html(text: str) -> str:
    return text.translate(_ESCAPE_TABLE)


def slugify(heading: str) -> str:
    """Best-effort GitHub-style anchor for ``[[Note#Heading]]`` fragments."""
    s = heading.strip().lower()
    s = re.sub(r"[^a-z0-9 _-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


# ---------------------------------------------------------------------------
# Frontmatter (minimal, stdlib only)
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading YAML frontmatter block from the note body.

    Supports only flat ``key: value`` lines (the vault has frontmatter in 2
    files and neither uses nesting/lists). Anything unparseable is ignored
    rather than treated as an error -- the note still renders.
    """
    if not text.startswith("---\n"):
        return {}, text
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text  # unclosed --- : treat whole text as body
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        meta[key] = value
    return meta, "\n".join(lines[end + 1:])


# ---------------------------------------------------------------------------
# Inline rendering
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"`([^`\n]+)`")
_EMBED_RE = re.compile(r"!\[\[([^\]\n]+)\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*|(?<!\w)__([^_\n]+)__(?!\w)")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!\w)_([^_\n]+)_(?!\w)")


def render_inline(text: str, resolver: "Builder") -> str:
    """Render inline markdown in a short string of text.

    Structural spans (code, embeds, wikilinks, links) are extracted on the RAW
    text first, so note names and URLs are resolved *before* any HTML escaping.
    Each extracted span is swapped for a placeholder and re-inserted last.
    """
    tokens: list[str] = []  # placeholder -> final HTML

    def _place(replacement: str) -> str:
        tokens.append(replacement)
        return f"\x00{len(tokens) - 1}\x00"

    # 1. Protected spans (raw text).
    text = _CODE_RE.sub(
        lambda m: _place(f"<code>{escape_html(m.group(1))}</code>"), text)
    text = _EMBED_RE.sub(
        lambda m: _place(resolver.render_link(m.group(1), is_embed=True)), text)
    text = _WIKILINK_RE.sub(
        lambda m: _place(resolver.render_link(m.group(1))), text)

    def _link_repl(m: re.Match[str]) -> str:
        label = render_inline(m.group(1), resolver)
        return _place(f'<a href="{escape_html(m.group(2))}">{label}</a>')

    text = _LINK_RE.sub(_link_repl, text)

    # 2. Escape whatever plain text remains.
    text = escape_html(text)

    # 3. Bold before italic.
    text = _BOLD_RE.sub(
        lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = _ITALIC_RE.sub(
        lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)

    # 4. Restore protected spans.
    for i, replacement in enumerate(tokens):
        text = text.replace(f"\x00{i}\x00", replacement)

    return text


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+)(.*)$")


def render_blocks(lines: list[str], resolver: "Builder") -> str:
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        # Fenced code block.
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(2)
            lang = m.group(3).strip()
            i += 1
            code: list[str] = []
            close_re = re.compile(r"^\s*" + re.escape(fence) + r"\s*$")
            while i < n and not close_re.match(lines[i]):
                code.append(lines[i])
                i += 1
            if i < n:
                i += 1  # consume closing fence
            cls = f' class="language-{escape_html(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{escape_html(chr(10).join(code))}</code></pre>")
            continue

        # ATX heading.
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{render_inline(m.group(2), resolver)}</h{level}>")
            i += 1
            continue

        # Horizontal rule.
        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        # Blockquote (single-level). Inner content is rendered as blocks so
        # multi-paragraph / list blockquotes survive.
        if line.lstrip().startswith(">"):
            quoted: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                raw = lines[i].lstrip()[1:]
                if raw.startswith(" "):
                    raw = raw[1:]
                quoted.append(raw)
                i += 1
            inner = render_blocks(quoted, resolver)
            out.append(f"<blockquote>\n{inner}\n</blockquote>")
            continue

        # List.
        if _LIST_ITEM_RE.match(line):
            html_list, i = _render_list(lines, i, resolver)
            out.append(html_list)
            continue

        # Paragraph: gather lines until a blank line or a block start.
        para: list[str] = [line]
        i += 1
        while i < n and lines[i].strip():
            nxt = lines[i]
            if (_FENCE_RE.match(nxt) or _HEADING_RE.match(nxt)
                    or _HR_RE.match(nxt) or nxt.lstrip().startswith(">")
                    or _LIST_ITEM_RE.match(nxt)):
                break
            para.append(nxt)
            i += 1
        out.append(f"<p>{render_inline(' '.join(para), resolver)}</p>")

    return "\n".join(out)


def _render_list(lines: list[str], i: int, resolver: "Builder") -> tuple[str, int]:
    """Consume a list (with nesting) starting at lines[i]. Returns (html, next_i)."""
    first = _LIST_ITEM_RE.match(lines[i])
    assert first is not None
    indent = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    tag = "ol" if ordered else "ul"
    items: list[str] = []

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            break  # a blank line terminates the list
        nm = _LIST_ITEM_RE.match(line)
        if not nm:
            break
        item_indent = len(nm.group(1))
        if item_indent > indent:
            sub, i = _render_list(lines, i, resolver)
            if items:
                items[-1] += sub
            else:
                items.append(sub)
            continue
        if item_indent < indent:
            break

        content = nm.group(4)
        i += 1
        cont = [content]
        # Continuation lines (lazy wrapping inside the item); stop at any
        # further list marker -- nesting is handled below.
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip():
                break
            if _LIST_ITEM_RE.match(nxt):
                break
            cont.append(nxt.strip())
            i += 1
        item_html = render_inline(" ".join(cont), resolver)
        # Nested list directly following this item.
        if i < len(lines) and lines[i].strip():
            nm3 = _LIST_ITEM_RE.match(lines[i])
            if nm3 and len(nm3.group(1)) > indent:
                sub, i = _render_list(lines, i, resolver)
                item_html += sub
        items.append(f"<li>{item_html}</li>")

    return f"<{tag}>" + "".join(items) + f"</{tag}>", i


# ---------------------------------------------------------------------------
# Build state
# ---------------------------------------------------------------------------

def _published_timestamp(meta: dict[str, str], note_path: Path) -> float:
    """Sort timestamp for a note: frontmatter published/date, else file mtime."""
    import datetime as _dt

    for key in ("published", "date"):
        raw = (meta.get(key) or "").strip()
        if not raw:
            continue
        try:
            dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    try:
        return note_path.stat().st_mtime
    except OSError:
        return 0.0


@dataclass
class Note:
    rel_path: str            # posix path relative to the vault, e.g. "notes/a.md"
    stem: str                # filename without ".md"
    title: str               # page title (frontmatter title or stem)
    body: str                # markdown body (frontmatter stripped)
    published_ts: float = 0.0  # sort key: frontmatter published/date, else mtime
    html: str = ""           # rendered <main> content
    allowed: bool = True     # Phase 3: within the deny-by-default allowlist
    url_path: str = ""       # Phase 3: output URL within the site ("" = unset)

    @property
    def out_rel(self) -> str:
        return self.rel_path[:-3] + ".html"


@dataclass
class Builder:
    vault: Path
    out: Path
    allowlist: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWLIST))
    notes: list[Note] = field(default_factory=list)
    by_stem: dict[str, list[Note]] = field(default_factory=dict)
    broken: list[tuple[str, str]] = field(default_factory=list)
    current_note: Note | None = None
    # Phase 2: link graph (note -> notes that link to it) and raw edges.
    edges: set[tuple[str, str]] = field(default_factory=set)
    backlinks: dict[str, list[Note]] = field(default_factory=dict)
    # Phase 3: the note (if any) that becomes the site home page (index.html).
    home_note: Note | None = None

    # -- discovery ---------------------------------------------------------
    def discover(self) -> None:
        for root, _dirs, files in os.walk(self.vault):
            for name in sorted(files):
                if not name.endswith(".md"):
                    continue
                full = Path(root) / name
                rel = posixpath.normpath(os.path.relpath(full, self.vault))
                note = Note(rel_path=rel, stem=name[:-3], title=name[:-3], body="")
                note.allowed = is_allowed(rel, self.allowlist)
                note.url_path = note.out_rel
                self.notes.append(note)

    def index(self) -> None:
        for note in self.notes:
            self.by_stem.setdefault(note.stem.lower(), []).append(note)

    # -- resolution --------------------------------------------------------
    def resolve(self, raw_target: str, source: Note) -> tuple[Note | None, str | None, str | None]:
        """Resolve a wikilink target. Returns (note, alias, heading)."""
        target = raw_target.strip()
        alias: str | None = None
        heading: str | None = None
        if "|" in target:
            target, alias = target.split("|", 1)
            target = target.strip()
            alias = alias.strip() or None
        if "#" in target:
            target, heading = target.split("#", 1)
            target = target.strip()
            heading = heading.strip() or None
        if not target:
            return None, alias, heading

        matches = self.by_stem.get(target.lower())
        if not matches:
            self.broken.append((source.rel_path, raw_target))
            return None, alias, heading
        if len(matches) > 1:
            # Ambiguous: two notes share a stem across folders. Fail closed.
            self.broken.append((source.rel_path, raw_target))
            return None, alias, heading
        note = matches[0]
        if not note.allowed:
            # Phase 3: the target exists but is outside the publish scope.
            # Treated as a broken link rather than silently rendering a page.
            self.broken.append((source.rel_path, raw_target))
            return None, alias, heading
        return note, alias, heading

    def relative_url(self, source: Note, target: Note) -> str:
        src_dir = posixpath.dirname(source.url_path)
        start = src_dir if src_dir else "."
        rel = posixpath.relpath(target.url_path, start)
        return urllib.parse.quote(rel)

    def render_link(self, raw_target: str, is_embed: bool = False) -> str:
        # render_link is called during inline rendering, where the current note
        # is tracked on the builder via `current_note`.
        source = self.current_note
        assert source is not None, "render_link called outside note rendering"
        note, alias, heading = self.resolve(raw_target, source)
        if note is not None:
            # Record a graph edge (embeds count as links too, matching Obsidian).
            self.edges.add((source.rel_path, note.rel_path))
        label = alias or (note.title if note else (raw_target.split("|")[0].split("#")[0].strip()))
        if note is None:
            return f'<span class="broken-link" title="unresolved wikilink">{escape_html(label)}</span>'
        url = self.relative_url(source, note)
        if heading:
            url += "#" + slugify(heading)
        cls = ' class="embed"' if is_embed else ""
        return f'<a{cls} href="{url}">{escape_html(label)}</a>'

    # -- build -------------------------------------------------------------
    def build(self) -> list[tuple[str, str]]:
        self.discover()
        self.index()
        self.home_note = self._pick_home()
        if self.home_note is not None:
            self.home_note.url_path = "index.html"
        for note in self.notes:
            if not note.allowed:
                continue
            raw = _read(note, self.vault)
            meta, body = parse_frontmatter(raw)
            note.title = meta.get("title") or note.stem
            note.body = body
            note.published_ts = _published_timestamp(meta, self.vault / note.rel_path)
        for note in self.notes:
            if not note.allowed:
                continue
            self.current_note = note
            lines = note.body.split("\n")
            note.html = render_blocks(lines, self)
        self.compute_backlinks()
        return self.broken

    def _pick_home(self) -> Note | None:
        """Choose the note that becomes the site home page (index.html).

        Prefers a note whose stem is ``home``, then ``index`` (case-insensitive);
        ties break deterministically by output path. Returns None if neither
        exists, in which case index.html is generated as a plain note list.
        """
        allowed = [n for n in self.notes if n.allowed]
        for stem in ("home", "index"):
            candidates = sorted(
                (n for n in allowed if n.stem.lower() == stem),
                key=lambda n: n.out_rel,
            )
            if candidates:
                return candidates[0]
        return None

    def compute_backlinks(self) -> None:
        by_rel = {n.rel_path: n for n in self.notes}
        self.backlinks = {n.rel_path: [] for n in self.notes}
        for src, dst in self.edges:
            if dst in by_rel:
                self.backlinks[dst].append(by_rel[src])
        # Deterministic order for stable golden output.
        for key in self.backlinks:
            self.backlinks[key].sort(key=lambda n: n.out_rel)

    # -- graph -------------------------------------------------------------
    def _graph_nodes_edges(self) -> tuple[list[dict], list[dict]]:
        nodes: list[dict] = []
        for note in sorted(self.notes, key=lambda n: n.url_path):
            if not note.allowed:
                continue
            nodes.append({
                "id": note.rel_path,
                "label": note.title,
                "url": urllib.parse.quote(note.url_path),
                "degree": 0,
            })
        node_map = {n["id"]: n for n in nodes}
        edges: list[dict] = []
        for s, t in self.edges:
            if s not in node_map or t not in node_map:
                continue
            edges.append({"source": s, "target": t})
            node_map[s]["degree"] += 1
            node_map[t]["degree"] += 1
        edges.sort(key=lambda e: (e["source"], e["target"]))
        return nodes, edges

    # -- search index ------------------------------------------------------
    def search_index_json(self) -> str:
        entries = []
        for note in sorted(self.notes, key=lambda n: n.url_path):
            if not note.allowed:
                continue
            entries.append({
                "title": note.title,
                "path": urllib.parse.quote(note.url_path),
                "text": note.body,
            })
        return json.dumps(entries, ensure_ascii=False, indent=2) + "\n"

    # -- backlinks panel ---------------------------------------------------
    def backlinks_html(self, note: Note) -> str:
        sources = self.backlinks.get(note.rel_path, [])
        if not sources:
            return ('<aside class="backlinks">\n'
                    '  <h2>Linked here</h2>\n'
                    '  <p class="backlinks-empty">No notes link here yet.</p>\n'
                    '</aside>')
        items = []
        for s in sources:
            url = self.relative_url(note, s)
            items.append(f'  <li><a href="{url}">{escape_html(s.title)}</a></li>')
        return ('<aside class="backlinks">\n'
                '  <h2>Linked here</h2>\n'
                '  <ul>\n' + "\n".join(items) + "\n  </ul>\n"
                '</aside>')

    # -- sidebar + index ---------------------------------------------------
    def sidebar_html(self, root: str) -> str:
        """Navigation sidebar listing every published note (allowlist-derived)."""
        notes = sorted(
            (n for n in self.notes if n.allowed),
            key=lambda n: (
                n is not self.home_note,       # home note first
                -n.published_ts,               # newest published first
                n.title.lower(),
                n.url_path,
            ),
        )
        items = []
        for n in notes:
            url = root + urllib.parse.quote(n.url_path)
            items.append(f'      <li><a href="{url}">{escape_html(n.title)}</a></li>')
        return (
            '<aside class="sidebar">\n'
            '  <nav aria-label="All notes">\n'
            '    <ul class="note-list">\n'
            + "\n".join(items) + "\n"
            '    </ul>\n'
            '  </nav>\n'
            '</aside>'
        )

    def _index_listing(self) -> str:
        notes = sorted(
            (n for n in self.notes if n.allowed),
            key=lambda n: (
                n is not self.home_note,       # home note first
                -n.published_ts,               # newest published first
                n.title.lower(),
                n.url_path,
            ),
        )
        items = []
        for n in notes:
            url = urllib.parse.quote(n.url_path)
            items.append(f'<li><a href="{url}">{escape_html(n.title)}</a></li>')
        return (
            "<h1>Notes</h1>\n"
            "<p>All published notes:</p>\n"
            "<ul>\n" + "\n".join(items) + "\n</ul>\n"
        )

    def index_page_html(self) -> str:
        """The site home page (index.html).

        If a Home/index note exists, it IS the home page (its rendered content
        becomes index.html and it is not emitted as a separate page). Otherwise
        a generated note list is used.
        """
        if self.home_note is not None:
            title = self.home_note.title
            body = self.home_note.html
            backlinks = self.backlinks_html(self.home_note)
        else:
            title = "Notes"
            body = self._index_listing()
            backlinks = ""
        nav = self.sidebar_html("")
        return page_shell(title=title, body=body, root="", backlinks=backlinks, nav=nav)

    # -- output ------------------------------------------------------------
    def write(self) -> None:
        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)

        # Hand-authored client-side assets (verbatim copies).
        for name in ("search.js", "style.css"):
            shutil.copy(ASSETS_DIR / name, self.out / name)

        # Search index (built from the allowlisted set only).
        (self.out / "search-index.json").write_text(
            self.search_index_json(), encoding="utf-8")

        # Privacy default (plan §5.5): robots.txt disallow-all. Remove this
        # write only if the operator later opts into search-engine indexing.
        (self.out / "robots.txt").write_text(
            "User-agent: *\nDisallow: /\n", encoding="utf-8")

        # Graph page (static SVG + embedded node/edge data).
        (self.out / "graph.html").write_text(
            graph_page_html(self), encoding="utf-8")

        # Home page (index.html).
        (self.out / "index.html").write_text(
            self.index_page_html(), encoding="utf-8")

        # Note pages (the home note, if any, lives at index.html instead).
        for note in self.notes:
            if not note.allowed or note is self.home_note:
                continue
            target = self.out / note.out_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            root = root_prefix(note)
            target.write_text(page_shell(
                title=note.title,
                body=note.html,
                root=root,
                backlinks=self.backlinks_html(note),
                nav=self.sidebar_html(root),
            ), encoding="utf-8")


def _read(note: Note, vault: Path) -> str:
    return (vault / note.rel_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Graph layout + SVG (dependency-free, deterministic)
# ---------------------------------------------------------------------------

def layout_graph(nodes: list[dict], edges: list[dict],
                 width: int = 1200, height: int = 800,
                 seed: int = 42, iterations: int = 250) -> dict[str, tuple[float, float]]:
    """Deterministic force layout.

    Returns ``{node_id: (x, y)}``. Fixed seed and pure float math keep the SVG
    byte-identical between builds (a requirement for golden-output tests).

    Isolated nodes (degree 0) have no attractive forces to hold them in place,
    so a naive repulsion-only layout pushes them off-canvas. They are pinned to
    a deterministic grid along the bottom of the frame instead, while the
    connected nodes (incident to at least one edge) get a Fruchterman-Reingold
    layout with a mild centering gravity that keeps any disconnected components
    in frame.
    """
    rng = random.Random(seed)
    degree = {n["id"]: n.get("degree", 0) for n in nodes}
    connected = [n["id"] for n in nodes if degree[n["id"]] > 0]
    orphans = [n["id"] for n in nodes if degree[n["id"]] == 0]

    pos: dict[str, list[float]] = {}

    # Orphans: deterministic grid along the bottom of the frame.
    if orphans:
        margin_x = 80.0
        orphan_y = float(height - 40)
        step = (width - 2 * margin_x) / len(orphans)
        for idx, node_id in enumerate(orphans):
            pos[node_id] = [margin_x + step * (idx + 0.5), orphan_y]

    ids = connected
    n = len(ids)
    for node_id in ids:
        pos[node_id] = [rng.uniform(width * 0.2, width * 0.8),
                        rng.uniform(height * 0.15, height * 0.75)]

    adj: dict[str, set[str]] = {i: set() for i in ids}
    for e in edges:
        s, t = e["source"], e["target"]
        if s in adj and t in adj:
            adj[s].add(t)
            adj[t].add(s)

    area = width * height
    k = math.sqrt(area / max(n, 1)) if n else 1.0
    k = max(k, 40.0)
    gravity = 0.06

    for it in range(iterations):
        disp = {i: [0.0, 0.0] for i in ids}

        # Repulsive forces (every connected pair).
        for a in range(n):
            for b in range(a + 1, n):
                ia, ib = ids[a], ids[b]
                dx = pos[ia][0] - pos[ib][0]
                dy = pos[ia][1] - pos[ib][1]
                d2 = dx * dx + dy * dy
                if d2 < 1e-9:
                    dx, dy = 1e-3, 1e-3
                    d2 = dx * dx + dy * dy
                d = math.sqrt(d2)
                force = k * k / d
                fx = force * dx / d
                fy = force * dy / d
                disp[ia][0] += fx
                disp[ia][1] += fy
                disp[ib][0] -= fx
                disp[ib][1] -= fy

        # Attractive forces (edges).
        for e in edges:
            s, t = e["source"], e["target"]
            if s not in pos or t not in pos:
                continue
            dx = pos[s][0] - pos[t][0]
            dy = pos[s][1] - pos[t][1]
            d = math.sqrt(dx * dx + dy * dy)
            if d < 1e-9:
                d = 1e-9
            force = d * d / k
            fx = force * dx / d
            fy = force * dy / d
            disp[s][0] -= fx
            disp[s][1] -= fy
            disp[t][0] += fx
            disp[t][1] += fy

        # Mild centering gravity keeps disconnected components in frame.
        cx, cy = width / 2, height / 2
        for i in ids:
            disp[i][0] += (cx - pos[i][0]) * gravity
            disp[i][1] += (cy - pos[i][1]) * gravity

        # Apply displacements with a cooling schedule.
        temp = max(1.0 - it / iterations, 0.02)
        max_step = temp * (min(width, height) * 0.1)
        for i in ids:
            dx, dy = disp[i]
            dlen = math.sqrt(dx * dx + dy * dy)
            if dlen < 1e-9:
                continue
            step = min(dlen, max_step)
            pos[i][0] += dx / dlen * step
            pos[i][1] += dy / dlen * step

    # Fit the connected nodes into a bounded sub-rectangle so the layout never
    # overflows the SVG frame (leaving the bottom band free for orphans).
    if ids:
        orphan_band = 90.0 if orphans else 0.0
        _fit_bounds(pos, ids, 0.0, 0.0, float(width), float(height) - orphan_band,
                    margin=30.0)

    return {i: (pos[i][0], pos[i][1]) for i in pos}


def _fit_bounds(pos: dict[str, list[float]], ids: list[str],
                x0: float, y0: float, x1: float, y1: float,
                margin: float = 30.0) -> None:
    """Uniformly scale+translate ``ids`` positions to fit inside the rect.

    Preserves aspect ratio so the force-layout shape is not distorted; the
    result is clamped to ``[x0+margin, x1-margin] x [y0+margin, y1-margin]``.
    """
    xs = [pos[i][0] for i in ids]
    ys = [pos[i][1] for i in ids]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    spanx = (maxx - minx) or 1.0
    spany = (maxy - miny) or 1.0
    avail_w = max(x1 - x0 - 2 * margin, 1.0)
    avail_h = max(y1 - y0 - 2 * margin, 1.0)
    scale = min(avail_w / spanx, avail_h / spany)
    for i in ids:
        pos[i][0] = x0 + margin + (pos[i][0] - minx) * scale
        pos[i][1] = y0 + margin + (pos[i][1] - miny) * scale


def render_graph_svg(nodes: list[dict], edges: list[dict],
                     pos: dict[str, tuple[float, float]],
                     width: int, height: int) -> str:
    parts = [f'<svg viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="Graph of notes and links">']
    for e in edges:
        x1, y1 = pos[e["source"]]
        x2, y2 = pos[e["target"]]
        parts.append(f'<line class="graph-edge" x1="{x1:.1f}" y1="{y1:.1f}" '
                     f'x2="{x2:.1f}" y2="{y2:.1f}"/>')
    for n in nodes:
        x, y = pos[n["id"]]
        r = 8.0 + 2.0 * min(n["degree"], 10)
        label = n["label"] if len(n["label"]) <= 26 else n["label"][:26] + "\u2026"
        parts.append(
            f'<a class="graph-node" href="{escape_html(n["url"])}">'
            f'<circle class="graph-node-circle" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"/>'
            f'<text class="graph-node-label" x="{x:.1f}" y="{y + r + 12:.1f}" '
            f'text-anchor="middle">{escape_html(label)}</text>'
            f'</a>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------

def root_prefix(note: Note) -> str:
    """Relative path from the note's output directory back to the site root."""
    d = posixpath.dirname(note.url_path)
    if d in ("", "."):
        return ""
    depth = len([p for p in d.split("/") if p])
    return "../" * depth


def page_shell(title: str, body: str, root: str = "", backlinks: str = "",
               nav: str = "") -> str:
    style = f"{root}style.css"
    script = f"{root}search.js"
    graph = f"{root}graph.html"
    home = f"{root}index.html"
    backlinks_block = f"\n{backlinks}\n" if backlinks else ""
    nav_block = f"\n{nav}\n" if nav else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f"<title>{escape_html(title)}</title>\n"
        f'<link rel="stylesheet" href="{style}">\n'
        "</head>\n"
        "<body>\n"
        '<header class="site-header">\n'
        '  <nav class="site-nav">\n'
        f'    <a class="site-title" href="{home}">Notes</a>\n'
        f'    <a class="nav-graph" href="{graph}">Graph</a>\n'
        '    <div class="search" role="search">\n'
        '      <input id="search-input" type="search" placeholder="Search notes\u2026" autocomplete="off" aria-label="Search notes">\n'
        '      <ul id="search-results" class="search-results" hidden></ul>\n'
        "    </div>\n"
        "  </nav>\n"
        "</header>\n"
        '<div class="layout">\n'
        f"{nav_block}"
        '  <div class="page">\n'
        '<main class="content">\n'
        f"{body}\n"
        "</main>\n"
        f"{backlinks_block}"
        "  </div>\n"
        "</div>\n"
        f"<script>window.SEARCH_ROOT = {json.dumps(root)};</script>\n"
        f'<script src="{script}"></script>\n'
        "</body>\n"
        "</html>\n"
    )


def graph_page_html(builder: Builder) -> str:
    nodes, edges = builder._graph_nodes_edges()
    pos = layout_graph(nodes, edges)
    svg = render_graph_svg(nodes, edges, pos, 1200, 800)
    body = (
        "<h1>Graph</h1>\n"
        '<div class="graph-wrap">\n'
        f"{svg}\n"
        "</div>\n"
    )
    # Embed node/edge data so a vendored JS graph library can be swapped in
    # later without changing the data model (the static SVG is the fallback).
    payload = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)
    # Escape "<" so the JSON can't terminate the <script> element early.
    body += '<script type="application/json" id="graph-data">' \
            + payload.replace("<", "\\u003c") + "</script>\n"
    nav = builder.sidebar_html("")
    return page_shell("Graph", body, root="", nav=nav)


# ---------------------------------------------------------------------------
# Publish pipeline (atomic swap) + CLI
# ---------------------------------------------------------------------------

def _swap(staging: Path, out: Path) -> None:
    """Atomically publish ``staging`` into ``out``, keeping the old tree on error."""
    if out.exists():
        backup = out.with_name(out.name + ".old")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        os.rename(out, backup)
        try:
            os.rename(staging, out)
        except Exception:
            os.rename(backup, out)  # roll back
            raise
        shutil.rmtree(backup, ignore_errors=True)
    else:
        os.rename(staging, out)


def publish(vault: Path, out: Path, allowlist: list[str] | None = None) -> tuple[int, list[str]]:
    """Full fail-closed pipeline: build -> leak check -> atomic swap.

    Builds into ``<out>.staging``, runs the mandatory leak check, and only on a
    clean result swaps the staging tree into ``out``. On any failure (build
    error, broken/excluded wikilinks, or a leak-check hit) the staging tree is
    discarded and any previous ``out`` is left byte-for-byte untouched.

    Returns ``(exit_code, problems)``.
    """
    eff = list(DEFAULT_ALLOWLIST) if allowlist is None else allowlist
    problems: list[str] = []
    staging = out.with_name(out.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        builder = Builder(vault, staging, allowlist=eff)
        broken = builder.build()
        builder.write()
    except Exception as exc:  # noqa: BLE001 - fail closed on any build error
        shutil.rmtree(staging, ignore_errors=True)
        return 1, [f"build error: {exc}"]

    if broken:
        problems.append(
            f"{len(broken)} unresolved wikilink(s) "
            f"(missing or outside the publish scope)"
        )
        for src, target in broken:
            problems.append(f"  {src}: [[{target}]]")

    problems.extend(check_artifact(vault, staging, eff))

    if problems:
        shutil.rmtree(staging, ignore_errors=True)
        return 1, problems

    _swap(staging, out)
    return 0, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render an Obsidian vault to static HTML, deny-by-default "
                    "on the web/ allowlist, with a mandatory leak check and "
                    "atomic publish (Phase 3).")
    parser.add_argument("vault", help="path to the vault (or fixture) directory")
    parser.add_argument("--out", default="out", help="output directory (default: ./out)")
    parser.add_argument("--allowlist", action="append", default=None,
                        help="allowlist prefix (repeatable); default web/")
    parser.add_argument("--allowlist-file", default=None,
                        help="flat allowlist file (one prefix per line)")
    args = parser.parse_args(argv)

    if args.allowlist_file:
        allowlist = parse_allowlist_file(Path(args.allowlist_file))
    elif args.allowlist is not None:
        allowlist = args.allowlist
    else:
        allowlist = DEFAULT_ALLOWLIST

    code, problems = publish(Path(args.vault), Path(args.out), allowlist=allowlist)
    for p in problems:
        print(f"build: {p}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
