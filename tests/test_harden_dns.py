import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "harden_dns", Path(__file__).parents[1] / "deploy" / "cloud" / "harden-dns.py"
)
dns = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dns)


class FakeDO:
    """A tiny stand-in for the DigitalOcean API: records per zone, and a log of what was created."""

    def __init__(self, zones):
        self.zones = zones
        self.created = []

    def __call__(self, method, path, body):
        zone = path.split("/")[2]
        if method == "GET":
            return {"domain_records": self.zones[zone]}
        assert method == "POST"
        self.created.append((zone, body))
        self.zones[zone].append(body)
        return {}


def test_an_empty_zone_gets_the_mail_lockdown_and_caa_and_nothing_is_changed_without_apply():
    api = FakeDO({"dayfile.cloud": [], "dayfile.app": []})
    lines = []
    assert dns.run(api, apply=False, out=lines.append) == 0
    assert api.created == [] and "Nothing was changed" in lines[-1]
    assert dns.run(api, apply=True, out=lambda _: None) == 0
    cloud = [r for z, r in api.created if z == "dayfile.cloud"]
    app = [r for z, r in api.created if z == "dayfile.app"]
    assert not any(r["type"] == "MX" for r in cloud)  # DigitalOcean refuses a null MX
    assert any(r["data"] == "v=spf1 -all" for r in cloud)
    assert {"letsencrypt.org.", "sectigo.com.", "zerossl.com."} <= {
        r["data"] for r in cloud if r.get("tag") == "issue"
    }
    assert any(r["name"] == "_dmarc" and r["data"] == "v=DMARC1; p=reject" for r in cloud)
    assert len(cloud) == 7 and len(app) == 5  # app: CAA only, its mail is left alone
    assert all(r["type"] == "CAA" for r in app)


def test_running_again_creates_nothing_because_it_is_idempotent():
    api = FakeDO({"dayfile.cloud": [], "dayfile.app": []})
    dns.run(api, apply=True, out=lambda _: None)
    api.created.clear()
    assert dns.run(api, apply=True, out=lambda _: None) == 0 and api.created == []


def test_existing_different_records_are_conflicts_and_never_overwritten():
    existing = {
        "dayfile.cloud": [
            {"type": "TXT", "name": "@", "data": "v=spf1 include:x.example ~all"},
            {"type": "TXT", "name": "_dmarc", "data": "v=DMARC1; p=none"},
            {"type": "CAA", "name": "@", "data": "other.example", "flags": 0, "tag": "issue"},
            {"type": "TXT", "name": "@", "data": "site-verification=abc"},
        ],
        "dayfile.app": [
            {"type": "MX", "name": "@", "data": "mx1.hostinger.com.", "priority": 5},
        ],
    }
    api = FakeDO(existing)
    lines = []
    assert dns.run(api, apply=True, out=lines.append) == 1
    created = [(z, r["type"], r["name"]) for z, r in api.created]
    assert ("dayfile.cloud", "TXT", "@") not in created  # the other SPF is not replaced
    assert ("dayfile.cloud", "TXT", "_dmarc") not in created
    assert not any(t == "CAA" and z == "dayfile.cloud" for z, t, _ in created)
    assert any("CONFLICT" in ln for ln in lines)
    assert sum(1 for z, t, _ in created if z == "dayfile.app") == 5  # dayfile.app still gets CAA


def test_the_script_never_deletes_or_updates():
    text = (Path(__file__).parents[1] / "deploy" / "cloud" / "harden-dns.py").read_text()
    assert '"DELETE"' not in text and '"PUT"' not in text and '"PATCH"' not in text
    assert "getpass.getpass" in text and "print(token" not in text


def test_a_partly_done_zone_only_gets_what_is_missing():
    done = [
        {"type": "CAA", "name": "@", "data": ";", "flags": 0, "tag": "issuewild"},
        {"type": "CAA", "name": "@", "data": "mailto:team@dayfile.app", "flags": 0, "tag": "iodef"},
        {"type": "TXT", "name": "@", "data": "v=spf1 -all"},
        {"type": "TXT", "name": "_dmarc", "data": "v=DMARC1; p=reject"},
    ]
    api = FakeDO({"dayfile.cloud": list(done), "dayfile.app": done[:2]})
    assert dns.run(api, apply=True, out=lambda _: None) == 0
    assert [(z, r["tag"], r["data"]) for z, r in api.created if z == "dayfile.cloud"] == [
        ("dayfile.cloud", "issue", "letsencrypt.org."),
        ("dayfile.cloud", "issue", "sectigo.com."),
        ("dayfile.cloud", "issue", "zerossl.com."),
    ]
    assert len([1 for z, _ in api.created if z == "dayfile.app"]) == 3


def test_a_refused_record_is_reported_and_the_others_are_still_created():
    class Picky(FakeDO):
        def __call__(self, method, path, body):
            if method == "POST" and body["data"] == "v=spf1 -all":
                raise dns.ApiError("HTTP 422 data is invalid")
            return super().__call__(method, path, body)

    api = Picky({"dayfile.cloud": [], "dayfile.app": []})
    lines = []
    assert dns.run(api, apply=True, out=lines.append) == 1
    assert any("FAILED" in ln and "HTTP 422 data is invalid" in ln for ln in lines)
    assert len([r for z, r in api.created if z == "dayfile.cloud"]) == 6  # all but the SPF
