"""The cloud profile's operational scripts, run against stub uploaders (no network)."""

import gzip
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC
from pathlib import Path

import pytest
import yaml

from dayfile.db import connect
from dayfile.ingest.core import ingest_events
from dayfile.tenancy import DirectoryTenancy

DEPLOY = Path(__file__).parents[1] / "deploy"
SCRIPT = DEPLOY / "cloud" / "backup-tenants.sh"

RCLONE_STUB = """#!/bin/sh
echo "rclone $*" >> "$STUB_LOG"
if [ "$1" = copy ]; then mkdir -p "$3" && cp -R "$2"/. "$3"/; fi
"""
CURL_STUB = """#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
"""


@pytest.fixture
def env(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("rclone", RCLONE_STUB), ("curl", CURL_STUB)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    remote = tmp_path / "remote"
    return {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "stub.log"),
        "DAYFILE_TENANTS_DIR": str(tmp_path / "tenants"),
        "DAYFILE_CMD": f"{sys.executable} -m dayfile.cli",
        "BACKUP_REMOTE": f"{remote}/",
        "BACKUP_STAGE": str(tmp_path / "stage"),  # the same path on the host and "in the container"
        "BACKUP_STAGE_IN_CONTAINER": str(tmp_path / "stage"),
    }


def run(env, **extra):
    return subprocess.run(
        ["bash", str(SCRIPT)], env={**env, **extra}, capture_output=True, text=True, timeout=60
    )


def make_tenants(env, *slugs):
    tenancy = DirectoryTenancy(env["DAYFILE_TENANTS_DIR"], "example.test")
    for slug in slugs:
        tenancy.create(slug)
    return tenancy


def uploaded(env):
    return sorted(p for p in Path(env["BACKUP_REMOTE"]).rglob("*.db.gz"))


def slugs(files):
    return [f.name.split(".")[0] for f in files]


def test_every_tenant_is_backed_up_compressed_and_restorable(env, tmp_path):
    tenancy = make_tenants(env, "acme", "globex")
    conn = connect(tenancy.get("acme").db_path)
    ingest_events(
        conn,
        [
            {
                "specversion": "dayfile/1",
                "id": "01JC8Z4Q7M2X9K3T5W6N8R0001",
                "source": {"id": "a", "ref": "r"},
                "occurred_at": "2026-11-06T10:00:00Z",
                "title": "hello",
            }
        ],
    )
    conn.close()
    result = run(env)
    assert result.returncode == 0, result.stderr
    files = uploaded(env)
    assert slugs(files) == ["acme", "globex"]
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(files[0].read_bytes()))
    check = sqlite3.connect(restored)
    assert check.execute("SELECT title FROM events").fetchall() == [("hello",)]
    assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_staging_copies_do_not_stay_behind(env, tmp_path):
    make_tenants(env, "acme")
    assert run(env).returncode == 0
    assert list((tmp_path / "stage").iterdir()) == []


def test_old_backups_are_pruned_by_age(env):
    make_tenants(env, "acme")
    run(env, BACKUP_KEEP_DAYS="3")
    log = Path(env["STUB_LOG"]).read_text()
    expected = "rclone delete --min-age 3d --include *.db.gz --include *.tenant.json"
    assert f"{expected} {env['BACKUP_REMOTE']}" in log
    assert "rclone rmdirs --leave-root" in log


def test_one_broken_tenant_does_not_stop_the_others_but_fails_the_run(env):
    tenancy = make_tenants(env, "acme", "globex")
    tenancy.get("acme").db_path.write_bytes(b"this is not a database" * 100)
    result = run(env, BACKUP_PING_URL="https://ping.example/x")
    assert result.returncode == 1
    assert slugs(uploaded(env)) == ["globex"]
    assert "acme" in result.stderr
    log = Path(env["STUB_LOG"]).read_text()
    assert "https://ping.example/x/fail" in log  # a failure pings the fail address
    assert not [ln for ln in log.splitlines() if ln.endswith("https://ping.example/x")]


def test_a_failed_run_pings_the_fail_address_and_never_the_success_one(env):
    tenancy = make_tenants(env, "acme")
    tenancy.get("acme").db_path.write_bytes(b"this is not a database" * 100)
    assert run(env, BACKUP_PING_URL="https://ping.example/x/").returncode == 1
    log = Path(env["STUB_LOG"]).read_text()
    assert "https://ping.example/x/fail" in log
    assert not [ln for ln in log.splitlines() if ln.endswith("https://ping.example/x/")]


def test_success_pings_the_dead_mans_switch(env):
    make_tenants(env, "acme")
    assert run(env, BACKUP_PING_URL="https://ping.example/x").returncode == 0
    assert "https://ping.example/x" in Path(env["STUB_LOG"]).read_text()


def test_missing_configuration_is_refused_before_doing_anything(env):
    bare = {k: v for k, v in env.items() if k != "BACKUP_REMOTE"}
    result = subprocess.run(["bash", str(SCRIPT)], env=bare, capture_output=True, text=True)
    assert result.returncode != 0 and "BACKUP_REMOTE" in result.stderr


def test_no_tenants_is_an_error_not_a_silent_success(env):
    Path(env["DAYFILE_TENANTS_DIR"]).mkdir()
    result = run(env)
    assert result.returncode == 1 and "no tenants" in result.stderr


# --- config files parse and agree with the code -------------------------------------


def test_cloud_compose_runs_one_worker_on_the_tenant_directory():
    compose = yaml.safe_load((DEPLOY / "cloud" / "compose.yaml").read_text())
    services = compose["services"]
    assert {"dayfile", "caddy", "litestream"} <= set(services)
    env = services["dayfile"]["environment"]
    assert env["DAYFILE_TENANTS_DIR"] == "/data/tenants" and "DAYFILE_BASE_DOMAIN" in env
    assert "command" not in services["dayfile"]  # the image's single-worker default
    assert services["litestream"]["profiles"] == ["replicate"]
    mounts = {
        v.split("   #")[0].rsplit(":", 1)[1]
        for name in ("dayfile", "litestream")
        for v in services[name]["volumes"]
        if "tenants" in v
    }
    assert mounts == {"/data/tenants"}


def test_litestream_watches_the_directory_layout_tenancy_creates():
    lite = yaml.safe_load((DEPLOY / "cloud" / "litestream.yml").read_text())
    [entry] = lite["dbs"]
    assert entry["dir"] == "/data/tenants" and entry["pattern"] == "dayfile.db"
    assert entry["recursive"] is True and entry["watch"] is True
    assert entry["replica"]["type"] == "s3"


def test_selfhost_compose_builds_from_the_repo_root():
    compose = yaml.safe_load((DEPLOY / "selfhost" / "compose.yaml").read_text())
    assert (DEPLOY / "selfhost" / compose["services"]["dayfile"]["build"] / "Dockerfile").is_file()


def test_the_public_proxy_hides_the_internal_hook_and_discards_logs():
    caddy = (DEPLOY / "cloud" / "Caddyfile").read_text()
    assert "/internal/*" in caddy and "respond @internal 404" in caddy
    assert "output discard" in caddy and "on_demand_tls" in caddy and "tls-ask" in caddy


def test_the_public_site_is_proxied_to_the_web_app_ahead_of_the_tenant_catch_all():
    caddy = (DEPLOY / "cloud" / "Caddyfile").read_text()
    site, catch_all = caddy.index("{$DAYFILE_SITE_DOMAIN:dayfile.app}"), caddy.index("https:// {")
    assert site < catch_all  # an explicit host must not fall into on-demand issuance
    block = caddy[site:catch_all]
    assert "reverse_proxy web:8001" in block and "output discard" in block
    assert "nosniff" in block and "Strict-Transport-Security" in block
    # The app sets its own policy; a second, different one from the proxy would intersect with it.
    assert "Content-Security-Policy" not in block


def test_the_web_container_cannot_see_tenant_data_and_runs_without_privileges():
    compose = yaml.safe_load((DEPLOY / "cloud" / "compose.yaml").read_text())
    api, web = compose["services"]["dayfile"], compose["services"]["web"]
    assert web["image"] == api["image"]  # one image, built once
    assert "ports" not in web and "DAYFILE_TENANTS_DIR" not in web["environment"]
    # its only mounts are its own accounts database and read-only secrets, never tenant data
    targets = [v.split("}:", 1)[1].split(":")[0] for v in web["volumes"]]  # after ${VAR:-default}
    assert sorted(targets) == ["/accounts", "/run/secrets"]
    assert not any("tenants" in v or "/data" in v for v in web["volumes"])
    assert next(v for v in web["volumes"] if "/run/secrets" in v).endswith(":ro")
    assert web["environment"]["DAYFILE_API_URL"] == "http://dayfile:8000"
    assert web["read_only"] is True and web["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in web["security_opt"] and web["mem_limit"]
    assert "dayfile.web:app" in web["command"] and "8001" in web["command"]
    assert "8001" in " ".join(web["healthcheck"]["test"])
    assert set(compose["services"]["caddy"]["depends_on"]) >= {"dayfile", "web"}


def test_the_web_app_is_importable_with_the_deployed_settings(tmp_path):
    env = {**os.environ, "DAYFILE_API_URL": "http://dayfile:8000", "DAYFILE_BASE_DOMAIN": "x.test"}
    env["DAYFILE_SITE_ORIGIN"] = "https://site.test"
    code = "import dayfile.web as w; assert w.app.title == 'dayfile.app'"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- cloud-init ----------------------------------------------------------------------

CLOUD_INIT = DEPLOY / "cloud" / "cloud-init.yaml"


def test_cloud_init_is_valid_cloud_config_with_no_secrets_baked_in():
    text = CLOUD_INIT.read_text()
    assert text.startswith("#cloud-config\n")
    config = yaml.safe_load(text)
    [user] = config["users"]
    assert user["name"] == "dayfile" and user["lock_passwd"] is True
    assert user["ssh_authorized_keys"] == ["__SSH_PUBLIC_KEY__"]  # the operator fills this in
    assert config["ssh_pwauth"] is False and config["disable_root"] is True
    for needle in ("ssh-rsa AAAA", "ssh-ed25519 AAAA", "BEGIN ", "password:", "token"):
        assert needle not in text.replace("ssh_authorized_keys", ""), needle


def test_cloud_init_opens_exactly_ssh_http_and_https():
    config = yaml.safe_load(CLOUD_INIT.read_text())
    allowed = sorted(c.split()[2] for c in config["runcmd"] if c.startswith("ufw allow"))
    assert allowed == ["22/tcp", "443/tcp", "80/tcp"]
    assert "ufw default deny incoming" in config["runcmd"]
    assert "ufw --force enable" in config["runcmd"]


def test_cloud_init_prepares_the_tenant_directory_for_the_container_user():
    runcmd = yaml.safe_load(CLOUD_INIT.read_text())["runcmd"]
    assert "chown 10001:10001 /srv/dayfile/tenants" in runcmd
    assert "chmod 700 /srv/dayfile/tenants" in runcmd


def test_cloud_init_installs_what_the_runbook_uses():
    packages = set(yaml.safe_load(CLOUD_INIT.read_text())["packages"])
    assert {"rclone", "fail2ban", "ufw", "unattended-upgrades", "docker.io"} <= packages


# --- smoke test -----------------------------------------------------------------------

SMOKE = DEPLOY / "cloud" / "smoke-test.sh"


def _serve(tmp_path, base_domain, mcp=False):
    import socket
    import time
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    root = tmp_path / "tenants"
    env = {
        "PATH": os.environ["PATH"],
        "DAYFILE_TENANTS_DIR": str(root),
        "DAYFILE_BASE_DOMAIN": base_domain,
        **({"DAYFILE_MCP": "1", "DAYFILE_OAUTH_ISSUER": "https://dayfile.app"} if mcp else {}),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dayfile.app:app", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    root.mkdir(exist_ok=True)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start")
    return proc, port, env


def _smoke(env, port, domain, mcp=False):
    return subprocess.run(
        ["bash", str(SMOKE), domain, "smokecheck"],
        env={
            **env,
            "DAYFILE_CMD": f"{sys.executable} -m dayfile.cli",
            "SMOKE_ORIGIN": f"http://127.0.0.1:{port}",
            **({"SMOKE_MCP": "1"} if mcp else {}),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_smoke_test_passes_against_a_correctly_configured_server(tmp_path):
    proc, port, env = _serve(tmp_path, "example.test")
    try:
        result = _smoke(env, port, "example.test")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "smoke test passed" in result.stdout and "FAIL" not in result.stderr
    assert result.stdout.count("ok ") >= 9
    # the throwaway tenant was cleaned up
    assert not (tmp_path / "tenants" / "smokecheck").exists()
    assert any((tmp_path / "tenants" / ".trash").iterdir())


def test_smoke_test_checks_the_mcp_endpoint_when_it_is_served(tmp_path):
    proc, port, env = _serve(tmp_path, "example.test", mcp=True)
    try:
        result = _smoke(env, port, "example.test", mcp=True)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the MCP endpoint asks for a credential" in result.stdout


def test_the_cloud_profile_turns_mcp_on_and_names_the_site_as_the_issuer():
    compose = (DEPLOY / "cloud" / "compose.yaml").read_text()
    assert 'DAYFILE_MCP: "1"' in compose and "DAYFILE_OAUTH_ISSUER: https://" in compose
    assert "--extra mcp" in (DEPLOY.parent / "Dockerfile").read_text()


def test_smoke_test_fails_when_the_server_is_configured_for_another_domain(tmp_path):
    proc, port, env = _serve(tmp_path, "other.test")
    try:
        result = _smoke(env, port, "example.test")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert result.returncode != 0 and "FAIL" in result.stderr
    assert "smoke test passed" not in result.stdout


def test_every_service_rotates_its_logs():
    import yaml

    compose = yaml.safe_load((DEPLOY / "cloud" / "compose.yaml").read_text())
    for name, svc in compose["services"].items():
        options = svc.get("logging", {}).get("options", {})
        assert options.get("max-size") and options.get("max-file"), f"{name} has no log limits"


def test_backup_setup_script_is_valid_and_never_takes_secrets_as_arguments():
    script = DEPLOY / "cloud" / "setup-backup.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert "read -r -s" in text and "chmod 600" in text
    assert "rclone obscure -" in text  # the password goes in on stdin, not argv
    bare = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert bare.returncode != 0 and "usage" in bare.stderr


def test_tenant_settings_are_backed_up_with_the_database(env):
    tenancy = make_tenants(env, "acme")
    tenancy.update("acme", max_events=77, suspended=True)
    assert run(env).returncode == 0
    (meta,) = Path(env["BACKUP_REMOTE"]).rglob("acme.tenant.json")
    saved = json.loads(meta.read_text())
    assert saved["max_events"] == 77 and saved["suspended"] is True


def test_staged_and_uploaded_files_are_private(env):
    make_tenants(env, "acme")
    assert run(env).returncode == 0
    for f in Path(env["BACKUP_REMOTE"]).rglob("acme.*"):
        assert f.stat().st_mode & 0o077 == 0, f


def test_pruning_only_touches_our_own_file_kinds(env):
    make_tenants(env, "acme")
    run(env)
    log = Path(env["STUB_LOG"]).read_text()
    delete = next(line for line in log.splitlines() if line.startswith("rclone delete"))
    assert "--include *.db.gz" in delete and "--include *.tenant.json" in delete


def test_nothing_to_back_up_fails_without_uploading(env):
    result = run(env)
    assert result.returncode == 1
    assert "rclone copy" not in (
        Path(env["STUB_LOG"]).read_text() if Path(env["STUB_LOG"]).exists() else ""
    )


def test_systemd_units_point_at_the_script_and_run_nightly_with_catch_up():
    service = (DEPLOY / "cloud" / "systemd" / "dayfile-backup.service").read_text()
    timer = (DEPLOY / "cloud" / "systemd" / "dayfile-backup.timer").read_text()
    assert "deploy/cloud/backup-tenants.sh" in service and "Type=oneshot" in service
    assert (
        "BACKUP_REMOTE=backup:" in service and "RCLONE_CONFIG=/etc/dayfile/rclone.conf" in service
    )
    assert "Persistent=true" in timer and "OnCalendar=" in timer


def test_the_backup_staging_directory_is_mounted_into_the_dayfile_container_only():
    import yaml

    compose = yaml.safe_load((DEPLOY / "cloud" / "compose.yaml").read_text())
    mounts = {n: s.get("volumes", []) for n, s in compose["services"].items()}
    assert any(":/backups" in v for v in mounts["dayfile"])
    assert not any("/backups" in v for v in mounts["web"])


def test_github_setup_script_takes_the_secret_only_from_a_hidden_prompt():
    script = DEPLOY / "cloud" / "setup-github.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert "read -r -s" in text and "chmod 400" in text and "umask 277" in text
    assert "$1" not in text and "$@" not in text.replace('exec sudo "$0" "$@"', "")
    assert 'echo "$secret"' not in text and "echo $secret" not in text


def test_both_services_read_the_same_admin_secret_file_and_only_those_two():
    compose = yaml.safe_load((DEPLOY / "cloud" / "compose.yaml").read_text())
    svc = compose["services"]
    paths = {n: svc[n]["environment"].get("DAYFILE_ADMIN_SECRET_FILE") for n in ("dayfile", "web")}
    assert paths["dayfile"] == paths["web"] == "/run/secrets/dayfile_admin"
    for name in ("caddy", "litestream"):
        env = svc[name].get("environment", {})
        assert "DAYFILE_ADMIN_SECRET_FILE" not in env
        assert not any("/run/secrets" in v for v in svc[name].get("volumes", []))


def test_the_purge_unit_runs_weekly_with_catch_up_and_uses_the_thirty_day_promise():
    service = (DEPLOY / "cloud" / "systemd" / "dayfile-purge.service").read_text()
    timer = (DEPLOY / "cloud" / "systemd" / "dayfile-purge.timer").read_text()
    assert "tenant purge --older-than-days 30" in service and "Type=oneshot" in service
    assert "Persistent=true" in timer and "OnCalendar=Sun" in timer


def test_the_restore_drill_only_reads_the_remote_and_verifies_what_it_pulled():
    text = (DEPLOY / "cloud" / "restore-drill.sh").read_text()
    assert "tenant verify-backup" in text and "rclone copy" in text
    assert "rclone delete" not in text and "rclone purge" not in text and "rclone sync" not in text
    assert "elapsed" in text and "mktemp -d" in text and "never on the production" in text


def test_www_redirects_to_the_bare_site_ahead_of_the_tenant_catch_all():
    caddy = (DEPLOY / "cloud" / "Caddyfile").read_text()
    www, catch_all = (
        caddy.index("www.{$DAYFILE_SITE_DOMAIN:dayfile.app} {"),
        caddy.index("https:// {"),
    )
    assert www < catch_all
    assert "redir https://{$DAYFILE_SITE_DOMAIN:dayfile.app}{uri} permanent" in caddy[www:catch_all]


def test_the_heartbeat_unit_runs_the_check_every_minute():
    service = (DEPLOY / "cloud" / "systemd" / "dayfile-heartbeat.service").read_text()
    timer = (DEPLOY / "cloud" / "systemd" / "dayfile-heartbeat.timer").read_text()
    assert "dayfile heartbeat check" in service and "Type=oneshot" in service
    assert "OnCalendar=minutely" in timer


def test_set_ping_urls_writes_private_env_files_and_keeps_what_is_not_replaced(tmp_path):
    script = DEPLOY / "cloud" / "set-ping-urls.sh"
    answers = tmp_path / "answers"
    env = {**os.environ, "ENV_DIR": str(tmp_path / "etc"), "PROMPT_FROM": str(answers)}

    def run_with(text):
        answers.write_text(text)
        return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)

    first = run_with("https://hc-ping.com/aaa\nhttps://hc-ping.com/bbb\n")
    assert first.returncode == 0 and "hc-ping" not in first.stdout + first.stderr  # never echoed
    backup = tmp_path / "etc" / "backup.env"
    assert backup.read_text() == "BACKUP_PING_URL=https://hc-ping.com/aaa\n"
    assert oct(backup.stat().st_mode & 0o777) == "0o600"
    assert run_with("\n\n").returncode == 0 and "aaa" in backup.read_text()  # empty keeps it
    assert run_with("https://hc-ping.com/ccc\n\n").returncode == 0
    assert backup.read_text() == "BACKUP_PING_URL=https://hc-ping.com/ccc\n"  # replaced, not added
    assert run_with("http://insecure\n\n").returncode == 1


def test_the_bare_service_domain_serves_only_status():
    caddy = (DEPLOY / "cloud" / "Caddyfile").read_text()
    block = caddy.split("DAYFILE_BASE_DOMAIN:dayfile.cloud}, www.")[1].split("# Everything else")[0]
    assert "handle /status" in block and "redir" in block and "on_demand" not in block


def test_uptimerobot_setup_script_takes_a_read_only_key_from_a_hidden_prompt():
    script = DEPLOY / "cloud" / "setup-uptimerobot.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert "read -r -s" in text and "chmod 400" in text and "does not look like an API key" in text
    assert 'echo "$key"' not in text
    web = yaml.safe_load((DEPLOY / "cloud" / "compose.yaml").read_text())["services"]["web"]
    assert web["environment"]["DAYFILE_UPTIMEROBOT_KEY_FILE"] == "/run/secrets/uptimerobot_key"


def accounts_db(tmp_path):
    from dayfile.web.accounts import store

    path = tmp_path / "accounts.db"
    store.connect(path).close()
    return path


def test_the_accounts_database_is_backed_up_beside_the_tenants_and_verifies(env, tmp_path):
    make_tenants(env, "acme")
    path = accounts_db(tmp_path)
    cmd = f"{sys.executable} -m dayfile.web.admin --db {path}"
    assert run(env, ACCOUNTS_CMD=cmd).returncode == 0
    remote = Path(env["BACKUP_REMOTE"])
    (copy,) = remote.rglob("accounts.accounts.db.gz")
    checked = subprocess.run(
        [sys.executable, "-m", "dayfile.cli", "tenant", "verify-backup", str(copy.parent)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "accounts database: 0 accounts: ok" in checked.stdout and "acme:" in checked.stdout


def test_a_failed_accounts_copy_fails_the_run_but_the_tenants_are_still_uploaded(env):
    make_tenants(env, "acme")
    result = run(env, ACCOUNTS_CMD="false", BACKUP_PING_URL="https://ping.example/x")
    assert result.returncode == 1 and "accounts database could not be copied" in result.stderr
    assert slugs(uploaded(env)) == ["acme"]
    assert "https://ping.example/x/fail" in Path(env["STUB_LOG"]).read_text()


def test_the_backup_unit_names_the_accounts_command():
    service = (DEPLOY / "cloud" / "systemd" / "dayfile-backup.service").read_text()
    assert "ACCOUNTS_CMD=" in service and "exec -T web python -m dayfile.web.admin" in service


def test_drill_config_script_takes_secrets_only_from_hidden_prompts():
    script = DEPLOY / "cloud" / "setup-drill-config.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert text.count("read -r -s") == 1 and "umask 077" in text and "chmod 600" in text
    assert 'echo "$secret"' not in text and 'echo "$password"' not in text
    assert "/etc/dayfile" not in text  # it is for the operator's own machine, not the server


STATS = DEPLOY / "cloud" / "launch-stats.sh"
COMPOSE_STUB = """#!/bin/sh
# stands in for `docker compose`: answers the commands launch-stats.sh runs
case "$*" in
*"admin stats"*) cat "$STUB_DIR/accounts-stats.json" ;;
*"tenant stats"*) cat "$STUB_DIR/tenant-stats.json" ;;
*"account list"*) cat "$STUB_DIR/accounts.jsonl" ;;
*) echo "unexpected: $*" >&2; exit 9 ;;
esac
"""


def run_stats(tmp_path, *args):
    from datetime import datetime

    today = datetime.now(UTC).date().isoformat()
    stub = tmp_path / "compose"
    stub.write_text(COMPOSE_STUB)
    stub.chmod(0o755)
    (tmp_path / "accounts-stats.json").write_text(
        json.dumps(
            {
                "at": "2026-11-18T18:00:00Z",
                "accounts": {"active": 5, "suspended": 0, "deleted": 1},
                "new_per_day": {today: 2},
                "feature_interest": {"alerts": {"signed_in": 1, "signed_out": 2}},
                "with_signup_answer": 3,
                "with_contact_email": 4,
            }
        )
    )
    (tmp_path / "tenant-stats.json").write_text(
        json.dumps({"logs": 6, "unreadable": 0, "activated": 5})
    )
    (tmp_path / "accounts.jsonl").write_text(
        json.dumps(
            {
                "github_login": "octo",
                "created_at": "2026-11-18T16:43:07Z",
                "status": "active",
                "slug": "octo-log",
            }
        )
        + "\n"
        + json.dumps(
            {
                "github_login": "deleted",
                "created_at": "2026-11-11T00:57:54Z",
                "status": "deleted",
                "slug": None,
            }
        )
        + "\n"
    )
    env = {**os.environ, "COMPOSE": str(stub), "STUB_DIR": str(tmp_path)}
    return subprocess.run(
        ["bash", str(STATS), *args], env=env, capture_output=True, text=True, check=False
    )


def test_launch_stats_prints_the_headline_numbers(tmp_path):
    r = run_stats(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "5 active, 1 deleted" in r.stdout
    assert "2 today, 2 in the last 7 days" in r.stdout
    assert "6 (5 have events" in r.stdout
    assert "3 plan answers, 4 contact emails" in r.stdout
    assert "alerts 3" in r.stdout


def test_launch_stats_accounts_lists_who_signed_up_without_the_deleted_login(tmp_path):
    r = run_stats(tmp_path, "accounts")
    assert r.returncode == 0, r.stderr
    assert "octo" in r.stdout and "octo-log" in r.stdout
    assert "(deleted)" in r.stdout


def test_containers_trust_the_proxy_so_feed_links_say_https():
    """Caddy connects from the Docker network, not 127.0.0.1; else the https scheme is lost."""
    assert '"--forwarded-allow-ips", "*"' in (DEPLOY.parent / "Dockerfile").read_text()
    web = yaml.safe_load((DEPLOY / "cloud" / "compose.yaml").read_text())["services"]["web"]
    assert web["command"][-2:] == ["--forwarded-allow-ips", "*"]


def test_selfhost_proxy_compose_publishes_only_on_loopback():
    compose = yaml.safe_load((DEPLOY / "selfhost" / "compose.proxy.yaml").read_text())
    assert set(compose["services"]) == {"dayfile"}
    assert compose["services"]["dayfile"]["ports"] == ["127.0.0.1:8000:8000"]


def test_selfhost_units_do_not_need_a_checkout():
    for unit in (DEPLOY / "selfhost").glob("*.service"):
        text = unit.read_text()
        assert "python_projects" not in text and "WorkingDirectory" not in text, unit.name
        assert "/usr/bin/env uv" not in text, unit.name
