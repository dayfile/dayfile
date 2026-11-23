# Running dayfile as user services

```
python3 -m venv ~/.local/share/dayfile/venv
~/.local/share/dayfile/venv/bin/pip install dayfile      # add "dayfile[mcp]" for the assistant server
mkdir -p ~/.config/dayfile ~/.config/systemd/user
cp deploy/selfhost/*.service ~/.config/systemd/user/     # or copy them from the repo
echo 'NTFY_URL=https://ntfy.sh/<your-secret-topic>' > ~/.config/dayfile/env && chmod 600 ~/.config/dayfile/env
systemctl --user daemon-reload
systemctl --user enable --now dayfile                    # add dayfile-email and dayfile-notify if you use them
```

**Web view (optional).** `dayfile-web.service` serves a page for reading your log in a browser, next to the API:
`~/.local/share/dayfile/venv/bin/pip install "dayfile[web]"`, copy the unit, then
`systemctl --user enable --now dayfile-web` and open `http://127.0.0.1:8001`. Sign in with a read token
(`dayfile token create --labels self`). It is read-only and updates by itself; add events with `dayfile add`, a script
or a write token. The session cookie is HTTPS-only, which browsers allow on `localhost`; on plain http elsewhere
(a home network) add `Environment=DAYFILE_INSECURE_COOKIES=1` to the unit, or put a TLS proxy in front.

The units expect the virtualenv above; change the `ExecStart` path if yours is elsewhere. Run `loginctl enable-linger $USER` to keep the
services running when logged out. The server binds 127.0.0.1; put a TLS proxy in front before exposing it.
Create tokens with `DAYFILE_DB=~/.local/share/dayfile/dayfile.db ~/.local/share/dayfile/venv/bin/dayfile token create --labels self`.

## Docker and HTTPS (a VPS on Hetzner, DigitalOcean, or anywhere)

```
git clone <repo> && cd dayfile/deploy/selfhost
echo 'DAYFILE_DOMAIN=feeds.example.com' > .env     # DNS A record must point at the server
docker compose up -d
docker compose exec dayfile dayfile --url https://feeds.example.com token create --labels self
```

The compose file also starts a **web view** of your log: open `https://<your domain>/`, sign in with a read token from
`token create`, and watch events arrive. It is read-only; the API, feeds and `/mcp` stay at the same address (the
Caddyfile sends those paths to dayfile and the rest to the web view).

Already running Traefik, Nginx Proxy Manager or your own Caddy? Use `docker compose -f compose.proxy.yaml up -d`
instead: it starts dayfile on `127.0.0.1:8000` and the web view on `127.0.0.1:8001`. Point your proxy at them: the paths listed in the `@api` line of the `Caddyfile` here go to 8000, everything else to 8001 (leave the web service out if you only want the API). The proxy must send
`X-Forwarded-Proto` and `X-Forwarded-For` (most do by default) so feed links say `https://`.

`DAYFILE_AUTH_URI` is optional; leave it unset for a log you read with tokens. It only matters if you run your own
sign-in service (see `docs/SPEC.md`).

Optional workers: `docker compose --profile email up -d` (put `GMAIL_USER` and `GMAIL_APP_PASSWORD`
in `.env`), `--profile notify` (`NTFY_URL`). Open only ports 22, 80 and 443 in the firewall.

Backups (nightly cron, then copy the file off the machine):

```
docker compose exec dayfile dayfile backup /data/backup-$(date +%F).db
```

Health check: `GET /healthz` returns `{"status":"ok"}`; point an uptime monitor at it.
