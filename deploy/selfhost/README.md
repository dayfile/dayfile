# Running dayfile as user services

```
mkdir -p ~/.local/share/dayfile ~/.config/dayfile ~/.config/systemd/user
cp deploy/selfhost/*.service ~/.config/systemd/user/
echo 'NTFY_URL=https://ntfy.sh/<your-secret-topic>' > ~/.config/dayfile/env && chmod 600 ~/.config/dayfile/env
systemctl --user daemon-reload
systemctl --user enable --now dayfile dayfile-email dayfile-notify
journalctl --user -u dayfile-email -f
```

Edit `WorkingDirectory` if the checkout is elsewhere. Run `loginctl enable-linger $USER` to keep the
services running when logged out. The server binds 127.0.0.1; put a TLS proxy in front before exposing it.
Create tokens with `DAYFILE_DB=~/.local/share/dayfile/dayfile.db uv run dayfile token create ...`.

## Docker and HTTPS (a VPS on Hetzner, DigitalOcean, or anywhere)

```
git clone <repo> && cd dayfile/deploy/selfhost
echo 'DAYFILE_DOMAIN=feeds.example.com' > .env     # DNS A record must point at the server
docker compose up -d
docker compose exec dayfile dayfile --url https://feeds.example.com token create --labels self
```

Optional workers: `docker compose --profile email up -d` (put `GMAIL_USER` and `GMAIL_APP_PASSWORD`
in `.env`), `--profile notify` (`NTFY_URL`). Open only ports 22, 80 and 443 in the firewall.

Backups (nightly cron, then copy the file off the machine):

```
docker compose exec dayfile dayfile backup /data/backup-$(date +%F).db
```

Health check: `GET /healthz` returns `{"status":"ok"}`; point an uptime monitor at it.
