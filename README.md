# obsidian-publish

A zero-dependency, pure-stdlib static-site generator that turns an [Obsidian]
vault into an [Obsidian-Publish]-like website — with wikilinks, backlinks, a
link graph, full-text search, and a deny-by-default content filter so only what
you explicitly publish ever leaves your vault.

- **Pure static output** — a directory of HTML/CSS/JS served as-is. No SSR, no
  runtime, no database.
- **Python 3 stdlib only** — no third-party packages, no network access.
- **Fail-closed** — a leak check runs before every publish and refuses to
  overwrite the live site if anything looks wrong.

This repo is designed to be deployed by **anyone on their own machine**: every
site-specific value (vault path, target host, key path, docroot) lives in a
gitignored `config.env`, never in committed code.

## Features

- Markdown → HTML (hand-written stdlib renderer): ATX headings, paragraphs,
  lists (nested), blockquotes, fenced code blocks, horizontal rules, inline
  `**bold**` / `*italic*` / `` `code` `` / `[label](url)`.
- Obsidian **wikilinks** `[[Note]]`, `[[Note|alias]]`, `[[Note#Heading]]` and
  embeds `![[Note]]` — resolved case-insensitively by stem, emitted as relative
  URLs (no absolute paths, no server config).
- **Backlinks** panel on every page ("who links here").
- Dependency-free **static SVG link graph**.
- Client-side **search** (`search-index.json` + self-contained `search.js`).
- **Sidebar** navigation, sorted home-first then newest-published-first
  (frontmatter `published`/`date`, falling back to file mtime).
- **Deny-by-default allowlist** — only notes under the publish scope (default
  `web/`) are published.
- **Atomic-swap publish** — build to staging, verify, then swap; a failed build
  leaves the previous site byte-for-byte untouched.
- Privacy default: `noindex` meta + `robots.txt` disallow-all.

## Security model

1. **Deny-by-default allowlist.** A note is published only if its path is under
   the publish scope (`web/` by default). Everything else is silently excluded
   from every page, the search index, the graph, and the sidebar. A wikilink
   from a published note to an excluded note is a **broken link** and fails the
   build (never silently rendered).
2. **Fail-closed leak check.** `leak_check.py` runs before every swap and fails
   the build on: an allowlist violation (a disallowed note page in the output),
   API-key shapes (`sk-`, `sk-or-`, `xoxb-`, `AKIA…`), a generic high-entropy
   secret heuristic, and a configurable sensitive-basename list (path + bytes).
3. **Atomic swap.** Output is built into `out.staging`, leak-checked, then
   swapped into `out/`. On any failure the staging tree is discarded and the
   previous `out/` is left untouched — the live site never goes half-updated.
4. **Restricted deploy key.** The target host is reached over a dedicated
   ed25519 key forced to `rrsync <docroot>` (no shell), optionally pinned with
   `from=` and `restrict`.

## Requirements

| Tool | Why | Version |
|---|---|---|
| `python3` | builds the site | ≥ 3.11 (uses `datetime.fromisoformat`) |
| `rsync` | transfers the built tree | any |
| `ssh` | remote transfer (only if target is remote) | any |
| `flock` | serialises publishes | any (util-linux) |
| `systemd` (user units) | optional — instant publish-on-save trigger | any |

`pytest` is needed only to run the test suite (test-only dependency).

## Quickstart

```sh
git clone <repo-url> ~/obsidian-publish      # repo is expected at ~/obsidian-publish
cd ~/obsidian-publish
cp config.example.env config.env             # then edit it (chmod 600)
./setup.sh                                    # validates deps, tests, prints manual steps
```

`setup.sh` validates dependencies, creates `config.env` if missing, runs the
test suite, optionally generates an ed25519 deploy keypair, and **prints** (does
not execute) the remaining manual steps:

1. install the deploy public key on the target host (`restrict` + `rrsync`),
2. copy `deploy/nginx-notes.conf.example` to the target's nginx and fill the
   placeholders,
3. install the `systemctl --user` units (`notes-publish.path` +
   `notes-publish.service`).

Then dry-run and publish:

```sh
./deploy.sh --dry-run
./deploy.sh
```

## Configuration reference (`config.env`)

Copy `config.example.env` → `config.env` and edit. All paths absolute.

| Variable | Meaning | Default |
|---|---|---|
| `NOTES_VAULT` | Absolute path to the Obsidian vault root (contains the publish scope) | *(required)* |
| `NOTES_PUBLISH_SCOPE` | Deny-by-default allowlist prefix, relative to `NOTES_VAULT` | `web/` |
| `NOTES_RSYNC_TARGET` | Full rsync destination (`user@host:/` remote, or a local dir) | *(required for deploy)* |
| `NOTES_DEPLOY_USER` | rsync user on the target (composes `NOTES_RSYNC_TARGET` if that's empty) | — |
| `NOTES_DEPLOY_HOST` | target host (composes `NOTES_RSYNC_TARGET` if that's empty) | — |
| `NOTES_SSH_KEY` | Absolute path to the deploy private key (remote targets only) | — |
| `NOTES_DOCROOT` | Remote docroot `/` maps to (for `authorized_keys`/nginx steps) | `/var/www/notes` |
| `NOTES_GENERATE_DEPLOY_KEY` | `true` → `setup.sh` generates an ed25519 keypair at `NOTES_SSH_KEY` | `false` |

`deploy.sh` sources `config.env`; `NOTES_*` environment variables override it
(used by the test suite). See `config.example.env` for full comments.

## How publishing triggers work

- `notes-publish.path` is a `systemd.path` unit that watches the publish scope
  (`$NOTES_VAULT/$NOTES_PUBLISH_SCOPE`) and activates `notes-publish.service`
  (same basename) on every change.
- `notes-publish.service` runs `deploy.sh` once per trigger.
- `deploy.sh` takes an exclusive `flock`: overlapping triggers are skipped
  cleanly (the in-flight run wins; the next write re-triggers).
- `deploy.sh` builds → leak-checks → atomically swaps → rsyncs. Any error exits
  non-zero and never rsyncs.

You can also publish manually at any time:

```sh
systemctl --user start notes-publish.service
./deploy.sh
```

## Troubleshooting

- **Leak-check rejection.** The build fails with `secret pattern 'openai'`,
  `high-entropy`, or a `deny-list basename`. That means real-looking secret
  material (or a filename on the sensitive list) is inside the publish scope.
  Move it outside the scope or remove the secret; the previous site stays live.
- **Broken wikilinks.** A `[[Note]]` pointing at a missing note, or at a note
  outside the publish scope, fails the build ("unresolved wikilink"). Fix the
  link, rename the note to match, or move the target into scope.
- **Start limit / rapid triggers.** The units set `StartLimitIntervalSec=0` and
  `deploy.sh` is flock-guarded, so rapid saves are safe. If you see start-limit
  hits on an older unit, reload the units: `systemctl --user daemon-reload`.
- **Path unit doesn't fire after adding a subfolder.** `systemd.path` does not
  recurse — add a `PathModified=` line for the new subdirectory and
  `systemctl --user daemon-reload`.
- **Trigger only fires while logged in.** Enable lingering once:
  `loginctl enable-linger "$USER"`.
- **`NOTES_VAULT is unset` from deploy.sh.** `config.env` is missing or empty.
  Copy and edit `config.example.env`, or run `./setup.sh`.

## Development

```sh
python3 -m pytest          # 53 tests: golden output, renderer, leak check, pipeline
python3 build.py VAULT_PATH [--out OUT_DIR] [--allowlist web/]...
```

Golden-output tests pin the full HTML tree byte-for-byte under `tests/golden/`;
fixtures are **synthetic** notes under `tests/fixtures/` (no real vault content,
seeded fake `sk-` keys only). The leak check and atomic-publish pipeline are
tested end-to-end in `tests/test_pipeline.py` against a **local** rsync target
via the `NOTES_*` overrides — no host is ever contacted in tests.

`IMPLEMENTATION_PLAN.md` is the original design document (phases 0–6, with the
history of how the pipeline was built). Read it for the full rationale.

[Obsidian]: https://obsidian.md
[Obsidian-Publish]: https://obsidian.md/publish
