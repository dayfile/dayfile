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

`DAYFILE_DOMAIN` is the web address your server will have: a domain or subdomain you own (replace `feeds.example.com`),
with its DNS A record pointing at the server's public IP and ports 80 and 443 reachable from the internet. Caddy uses it
to get a free HTTPS certificate. No domain? Use `compose.proxy.yaml` below behind your own proxy, the `pip install` route,
or try it locally with `DAYFILE_DOMAIN=localhost` (Caddy issues itself a certificate, so browsers and feed readers warn).

Already running Traefik, Nginx Proxy Manager or your own Caddy? Use `docker compose -f compose.proxy.yaml up -d`
instead: it starts only dayfile, published on `127.0.0.1:8000`, and you point your proxy at it. The proxy must send
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
