# Self-hosting dayfile

Your log is one SQLite file. The server (API, feeds, `/mcp`) and an optional read-only web view run from the same
package. Pick the lightest option that fits: try it, run it by hand, keep it running with systemd, or use Docker with HTTPS.

## 1. Try it in a minute

```
bash try-web-view.sh
```

It sets up a throwaway copy with sample events and prints a sign-in URL and a command for adding more. Ctrl-C deletes
it. `FROM=<path or git URL>` tries a checkout instead of PyPI. Needs Python 3.11 or newer.

## 2. Run it by hand

```
pip install "dayfile[web]"            # drop [web] if you do not want the web view; add "dayfile[mcp]" for the assistant server
export DAYFILE_DB=~/dayfile.db        # where your log lives
dayfile token create --labels self    # prints feed URLs once; the long part after /t/ is your read token
dayfile add --db "$DAYFILE_DB" "hello"
```

Then start the two servers, each in its own terminal (or in `tmux`):

```
uvicorn dayfile.app:app --port 8000
DAYFILE_SINGLE_LOG=1 DAYFILE_API_URL=http://127.0.0.1:8000 uvicorn dayfile.web:app --port 8001
```

Open `http://localhost:8001` and paste the read token or any of the feed URLs. The web view is read-only and updates
by itself; add events with `dayfile add`, a script, or a write token
(`dayfile token create --labels self --write-source adapter:cli`). The feeds work without the web view, at
`http://127.0.0.1:8000/t/<read token>/feed.json` and `feed.atom`.

The session cookie is HTTPS-only, which browsers allow on `localhost`. On plain http elsewhere (a home network) set
`DAYFILE_INSECURE_COOKIES=1` for the web view, or put a TLS proxy in front. Both servers bind 127.0.0.1; put a TLS
proxy in front before exposing them.

### Keep it running with systemd

The units run the same two commands as user services, so nothing runs as root and no checkout is needed.

```
python3 -m venv ~/.local/share/dayfile/venv
~/.local/share/dayfile/venv/bin/pip install "dayfile[web]"   # add "dayfile[mcp]" for the assistant server
mkdir -p ~/.config/systemd/user
cp dayfile.service dayfile-web.service ~/.config/systemd/user/   # from this folder, or copy them from the repo
systemctl --user daemon-reload
systemctl --user enable --now dayfile dayfile-web
loginctl enable-linger $USER                                # keep them running when you are logged out
```

The units keep your database at `~/.local/share/dayfile/dayfile.db`; if you already ran it by hand, copy your file
there first. Create tokens with
`DAYFILE_DB=~/.local/share/dayfile/dayfile.db ~/.local/share/dayfile/venv/bin/dayfile token create --labels self`.
The units expect the virtualenv above; change the `ExecStart` path if yours is elsewhere. For plain http outside
localhost add `Environment=DAYFILE_INSECURE_COOKIES=1` to `dayfile-web.service`.

Optional workers, as `dayfile-email.service` (Gmail or Google Voice SMS over IMAP) and `dayfile-notify.service` (ntfy
push). They read their settings from `~/.config/dayfile/env`:

```
mkdir -p ~/.config/dayfile
echo 'NTFY_URL=https://ntfy.sh/<your-secret-topic>' > ~/.config/dayfile/env && chmod 600 ~/.config/dayfile/env
cp dayfile-email.service dayfile-notify.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now dayfile-notify   # and dayfile-email if you use it
```

## 3. Docker and HTTPS (a VPS on Hetzner, DigitalOcean, or anywhere)

```
git clone <repo> && cd dayfile/deploy/selfhost
echo 'DAYFILE_DOMAIN=feeds.example.com' > .env     # DNS A record must point at the server
docker compose up -d
docker compose exec dayfile dayfile --url https://feeds.example.com token create --labels self
```

`DAYFILE_DOMAIN` is the web address your server will have: a domain or subdomain you own (replace `feeds.example.com`),
with its DNS A record pointing at the server's public IP and ports 80 and 443 reachable from the internet. Caddy uses it
to get a free HTTPS certificate. No domain? Use the proxy compose below behind your own proxy, or the sections above, or
try it locally with `DAYFILE_DOMAIN=localhost` (Caddy issues itself a certificate, so browsers and feed readers warn).

The compose file also starts the **web view**: open `https://<your domain>/` and sign in with a read token. The API,
feeds and `/mcp` stay at the same address (the Caddyfile sends those paths to dayfile and the rest to the web view).
There is no prebuilt image yet, so the first `up` builds one from the repo.

### Behind your own reverse proxy

Already running Traefik, Nginx Proxy Manager or your own Caddy? Use `docker compose -f compose.proxy.yaml up -d`
instead. It starts dayfile on `127.0.0.1:8000` and the web view on `127.0.0.1:8001`. Point your proxy at them: the
paths listed in the `@api` line of the `Caddyfile` here go to 8000, everything else to 8001 (leave the web service out
if you only want the API). Your proxy should send `X-Forwarded-Proto` and `X-Forwarded-For` (most do by default) so
feed links say `https://`.

`DAYFILE_AUTH_URI` is optional; leave it unset for a log you read with tokens. It only matters if you run your own
sign-in service (see `docs/SPEC.md`).

### Workers, backups, health

Optional workers: `docker compose --profile email up -d` (put `GMAIL_USER` and `GMAIL_APP_PASSWORD` in `.env`),
`--profile notify` (`NTFY_URL`). Open only ports 22, 80 and 443 in the firewall.

Backups (nightly cron, then copy the file off the machine):

```
docker compose exec dayfile dayfile backup /data/backup-$(date +%F).db
```

Health check: `GET /healthz` returns `{"status":"ok"}`; point an uptime monitor at it.
