"""The `--json` contract, the id column, UTC stamps and send-only health — offline.

A synthetic state dir is built with the real storage code, then the CLI runs in a
subprocess with a scrubbed environment and no credentials, so nothing can reach the API.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from dnsimple_cli import output
from dnsimple_cli.health_check import run_health_check
from dnsimple_cli.models import DNSRecord
from dnsimple_cli.storage import Storage, process_api_data


def rec(id, name, type, content, **kw):
    return {"id": id, "zone_id": "example.com", "name": name, "type": type,
            "content": content, "ttl": 3600, "priority": kw.get("priority")}


RECEIVE = [
    rec(101, "", "MX", "aspmx.l.google.com", priority=1),
    rec(102, "", "TXT", "v=spf1 include:_spf.google.com ~all"),
    rec(103, "www", "CNAME", "example.org"),
]
SEND_ONLY = [
    rec(201, "send", "MX", "feedback-smtp.example.net", priority=10),
    rec(202, "send", "TXT", "v=spf1 include:amazonses.com ~all"),
    rec(203, "resend._domainkey", "TXT", "p=MIGf"),
]


@pytest.fixture
def state(tmp_path, monkeypatch):
    st = tmp_path / "state"
    monkeypatch.setenv("DNSIMPLE_STATE_DIR", str(st))
    domains = [{"name": "example.com", "state": "registered"},
               {"name": "example.org", "state": "hosted"}]
    zones = {d["name"]: {"updated_at": "2026-09-01T00:00:00Z"} for d in domains}
    records = {"example.com": RECEIVE,
               "example.org": [dict(r, zone_id="example.org") for r in SEND_ONLY]}
    doms, meta = process_api_data(domains, zones, records, "1")
    with Storage() as s:
        s.save_domains(doms)
        s.save_metadata(meta)
    return st


def cli(state, *args, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DNSIMPLE_")}
    env["DNSIMPLE_STATE_DIR"] = str(state)
    env.pop("NO_COLOR", None)
    return subprocess.run([sys.executable, "-m", "dnsimple_cli", *args], cwd=cwd or state.parent,
                          env=env, capture_output=True, text=True)


def one_json(stdout):
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout must be exactly one JSON line, got: {stdout!r}"
    return json.loads(lines[0])


def test_records_json_has_ids_and_utc_stamp(state):
    r = cli(state, "records", "example.com", "--json")
    assert r.returncode == 0, r.stderr
    d = one_json(r.stdout)
    assert d["ok"] is True and d["domain"] == "example.com"
    assert {x["id"] for x in d["records"]} == {101, 102, 103}
    assert set(d["records"][0]) == {"id", "name", "type", "content", "ttl", "priority"}
    assert d["synced_at"].endswith("Z") and "." not in d["synced_at"]


def test_json_flag_anywhere_and_progress_on_stderr(state):
    r = cli(state, "--json", "domains")
    assert r.returncode == 0
    d = one_json(r.stdout)
    assert [x["name"] for x in d["domains"]] == ["example.com", "example.org"]
    assert "Domains" not in r.stdout  # the human table is not on stdout


def test_errors_are_json_with_nonzero_exit(state):
    r = cli(state, "records", "missing.example", "--json")
    assert r.returncode == 1
    assert one_json(r.stdout) == {
        "ok": False, "error": "Domain 'missing.example' not found. Run 'dnsimple sync' if it is new."}
    r = cli(state, "records", "--bogus", "--json")
    assert r.returncode == 2 and one_json(r.stdout)["ok"] is False


def test_human_records_shows_id_column_and_no_ansi_when_piped(state):
    r = cli(state, "records", "example.com")
    assert r.returncode == 0
    assert "ID" in r.stdout and "101" in r.stdout
    assert "\x1b[" not in r.stdout, "no ANSI codes when stdout is not a terminal"


def test_nameservers_plain_form_refuses_write_flags(state):
    r = cli(state, "nameservers", "example.com", "--set-dnsimple", "--json")
    assert r.returncode == 1
    assert "nameservers-set" in one_json(r.stdout)["error"]


def test_health_json_send_only_domain(state):
    r = cli(state, "health", "example.org", "--json")
    assert r.returncode == 0, r.stderr
    d = one_json(r.stdout)
    assert d["mail_mode"] == "send-only"
    res = {x["check"]: x for x in d["results"]}
    assert res["MX Records"]["status"] == "pass"
    assert res["SPF Record"]["status"] == "warn"
    assert res["SPF Record"]["recommendation"].endswith('"v=spf1 -all" so nothing can send as the root domain')
    assert set(d["summary"]) == {"pass", "warn", "fail"}


def _records(raw):
    return [DNSRecord.from_api(r) for r in raw]


def test_health_modes():
    assert run_health_check("example.com", _records(RECEIVE)).mail_mode == "receive"
    assert run_health_check("example.org", _records(SEND_ONLY)).mail_mode == "send-only"
    assert run_health_check("example.net", []).mail_mode == "none"
    receive = {x.check: x for x in run_health_check("example.com", _records(RECEIVE[:1])).results}
    assert receive["SPF Record"].status == "fail"  # a receiving domain without SPF still fails


def test_utc_iso():
    aware = datetime(2026, 9, 28, 15, 15, 32, 496709, tzinfo=timezone.utc)
    assert output.utc_iso(aware) == "2026-09-28T15:15:32Z"
    naive_local = aware.astimezone().replace(tzinfo=None)  # how 0.1.0 stored stamps
    assert output.utc_iso(naive_local) == "2026-09-28T15:15:32Z"
    assert output.utc_iso(None) is None
