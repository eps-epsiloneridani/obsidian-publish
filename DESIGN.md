# Design — Obsidian Vault → Static Site

The original design/implementation plan for this project is in git history
(`IMPLEMENTATION_PLAN.md` on the `master` branch). This document is the
deployment-agnostic summary.

## Goals

- Publish a curated subfolder of an Obsidian vault as a static website with
  Obsidian-Publish-like features (sidebar, wikilinks, backlinks, graph,
  full-text search, responsive layout).
- **Deny-by-default security**: only explicitly allowlisted content can ever
  reach the artifact; every build runs a fail-closed leak check; a failed build
  never touches the previously published site.
- Zero runtime dependencies beyond the Python standard library; no
  third-party code, no CDN fetches, no external assets in the output.

## Architecture

```
Obsidian vault ──edit──▶ systemd.path (instant trigger)
                              │
                              ▼
                        deploy.sh (flock-guarded, fail-closed)
                              │
              ┌───────────────┴────────────────┐
              ▼                                ▼
        build.py                          leak_check.py
   (render + filter +                   (allowlist assertion +
    atomic swap to out/)                 secret deny-list scan)
              │                                │
              └────────┬───────────────────────┘
                       ▼  only if everything passed
                 rsync over SSH  ──▶  static web host (nginx)
```

- **build.py** — stdlib markdown renderer (headings, lists, quotes, fenced
  code, bold/italic/code, links, `[[wikilinks]]`, `![[embeds]]`, flat
  frontmatter). Resolves wikilinks case-insensitively by note stem across the
  publish scope; unresolved or out-of-scope links fail the build. Emits pages
  with sidebar (home first, then newest `published:`/`date:` frontmatter, mtime
  fallback), per-page backlinks, a dependency-free static-SVG site graph,
  `search-index.json` + vanilla-JS client search, `robots.txt`
  (disallow-all) and `noindex` metas. Publish is atomic: built to a staging
  dir, leak-checked, then swapped in; any failure leaves the prior `out/`
  byte-identical.
- **filter.py** — the deny-by-default allowlist (single source of truth for
  builder and leak check). Default scope: the vault's `web/` subfolder.
- **leak_check.py** — hard gate before publish: (1) every artifact file must
  map to an allowlisted source note or a known generated asset; (2) every
  artifact byte is scanned for secret patterns (`sk-`, `sk-or-`, `xoxb-`,
  `AKIA`, high-entropy heuristic) and configurable deny-listed basenames.
  Any hit → non-zero exit, nothing published.
- **deploy.sh** — one-shot pipeline with `flock` serialisation, `--dry-run`
  mode, and `NOTES_*` environment overrides for tests/CI. Sources
  `config.env` (see `config.example.env` for the schema).
- **dist/** — systemd user units: `notes-publish.path` (watch the publish
  scope for edits; PathModified only — DirectoryNotEmpty is a tight-loop
  footgun) activating `notes-publish.service` (oneshot, resource-capped,
  `StartLimitIntervalSec=0` so rapid saves are safe under the flock).

## Security model

1. **Deny-by-default scope** — content outside the allowlist is not indexed,
   rendered, graphed, searched, listed, or linked (links to it are build
   failures).
2. **Leak check is mandatory and fail-closed** — it runs inside every publish,
   not as an optional extra step.
3. **Atomic swap** — a failed build leaves the previously published site
   untouched.
4. **Least-privilege deploy key** — SSH key restricted with
   `restrict`/`from=`/forced `rrsync` into the docroot only (template in
   `deploy/authorized_keys.deploy.example`).
5. **Privacy defaults** — search engines are disallowed via robots.txt and
   noindex until you deliberately opt out.

## Testing

`python3 -m pytest -q` — golden byte-comparison of the full output tree,
wikilink/backlink/search/graph correctness, leak-check exit codes against
seeded fixtures (clean/secret/allowlist-violation/deny-basename), atomic-swap
failure preservation, pipeline end-to-end against a local stub rsync target.
To regenerate goldens after an intentional template change: build
`tests/fixtures/vault` to a temp dir and replace `tests/golden/`.

## Layout

```
build.py filter.py leak_check.py   generator + gates
deploy.sh config.example.env       pipeline + config schema
setup.sh                           dependency check + bootstrap
dist/                              systemd user units (templates)
deploy/                            generic nginx + authorized_keys examples
assets/                            hand-authored CSS/JS
tests/                             fixtures, goldens, suite
```
