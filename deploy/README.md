# Deploy — reference artifacts for the target host

Static-site serving on a hardened nginx host, reached over a restricted rsync
deploy key. Everything here is a **template or reference**; the concrete values
come from the gitignored `config.env` (see `../config.example.env`) and are
substituted by `setup.sh` or by hand on the target host.

## What lives here

| Item | Purpose |
|---|---|
| `nginx-notes.conf.example` | Generic static-file nginx site config — copy to `/etc/nginx/sites-available/notes` and fill `<SERVER_NAME>` / `<DOCROOT>` |
| `authorized_keys.deploy.example` | Template for the deploy user's `~/.ssh/authorized_keys` line (least-privilege `restrict` + `rrsync`) |
| `index.html.placeholder` | Seed `index.html` for the docroot so the atomic swap never 403s before the first publish |

## Deploy key (least-privilege)

The build machine holds the private key (`NOTES_SSH_KEY` in `config.env`); only
the **public** key is installed on the target, in the deploy user's
`~/.ssh/authorized_keys`, as a single line matching
`authorized_keys.deploy.example`:

- `restrict` disables pty, port/agent/X11 forwarding.
- `from="<BUILD_HOST_IP>"` pins the build machine's address.
- `command="/usr/bin/rrsync <DOCROOT>/"` permits only `rsync --server`, and only
  against the docroot tree. Content is public-by-design, so read access is not a
  secret-exposure concern.

`setup.sh` generates an ed25519 keypair when `NOTES_GENERATE_DEPLOY_KEY=true`
and prints the public key plus the exact `authorized_keys` line.

## Deploy (from the build machine)

```sh
./deploy.sh            # build + leak-check + rsync to NOTES_RSYNC_TARGET
./deploy.sh --dry-run  # build + leak-check only; skip rsync
```

`/` in the rsync target maps to the docroot (rrsync root-relative argument).

## Verify

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://<your-host>/   # 200
```
