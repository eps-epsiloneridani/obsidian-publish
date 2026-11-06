# systemd USER units (installed by setup.sh, not committed in final form)

Watches the vault's publish scope for changes and runs the one-shot `deploy.sh`
republish pipeline on every change (build + leak-check + rsync to the target).
The pipeline is **fail-closed**: any error exits non-zero and leaves the target
docroot untouched. These are **user** units (run as the current user, no root).

## Files

| File | Purpose |
|---|---|
| `notes-publish.path` | `systemd.path` trigger — template; `@VAULT_WEB@` is substituted from `config.env` by `setup.sh` |
| `notes-publish.service` | `Type=oneshot`; `ExecStart=%h/obsidian-publish/deploy.sh` (repo expected at `~/obsidian-publish`) |

## How configuration reaches the units

There is one configuration source: the gitignored `config.env` next to
`deploy.sh`.

- `deploy.sh` **sources `config.env`** at start (and `NOTES_*` env vars
  override it for tests/CI). The service unit simply runs `deploy.sh`, so every
  site-specific value it uses comes from `config.env`.
- The service unit's only path is the repo location, expressed portably with the
  systemd `%h` specifier (`%h/obsidian-publish`). Clone the repo to
  `~/obsidian-publish` and no edit is needed.
- The **path** unit is the one exception: `systemd.path` has no
  `EnvironmentFile=` and `PathModified=`/`DirectoryNotEmpty=` must be literal
  paths. `setup.sh` therefore generates the concrete unit from `config.env`
  (`NOTES_VAULT` + `NOTES_PUBLISH_SCOPE`) and prints the install commands.

## Install

Run `./setup.sh` from the repo root; it prints the exact commands. In short:

```sh
mkdir -p ~/.config/systemd/user
sed 's|@VAULT_WEB@|/your/vault/web|' dist/notes-publish.path > ~/.config/systemd/user/notes-publish.path
cp dist/notes-publish.service ~/.config/systemd/user/notes-publish.service
systemctl --user daemon-reload
systemctl --user enable --now notes-publish.path
```

Replace `/your/vault/web` with `$NOTES_VAULT/$NOTES_PUBLISH_SCOPE` from
`config.env`.

## Verify

```sh
systemctl --user status notes-publish.path notes-publish.service
journalctl --user -u notes-publish.service -e
# force a manual republish (also exercises the live rsync):
systemctl --user start notes-publish.service
```

## Notes

- **Lingering** — for the path trigger to fire without an active login session,
  enable lingering once: `loginctl enable-linger "$USER"`.
- **Subfolders** — `systemd.path` does **not** recurse. The unit watches the
  top-level scope dir via `PathModified=` (new files, edits, deletes and renames
  directly in it). When notes are organised into subfolders, add one
  `PathModified=` line per subdirectory to `notes-publish.path` and re-run
  `systemctl --user daemon-reload`.
- **Why no `DirectoryNotEmpty=`** — `DirectoryNotEmpty=` re-triggers in a tight
  loop for as long as the watched directory stays non-empty (a systemd.path
  footgun that can fire the publish every second). `PathModified=` already fires
  when the first note is dropped into an empty scope, so it is unnecessary. On a
  fresh install, publish the current state once with `./deploy.sh` (or
  `systemctl --user start notes-publish.service`).
- **One publish at a time** — `deploy.sh` takes an `flock`; a trigger landing
  during an in-flight publish is skipped cleanly (exit 0).
- **Dry run** — `deploy.sh --dry-run` builds + leak-checks but skips the rsync.
