"""Where the CLI keeps credentials, its database and reports.

Everything lives in the state dir (``$DNSIMPLE_STATE_DIR``, default ``~/.simt/dnsimple``),
never relative to the current directory or the install location. Credentials fall back to
``./.env`` for setups that predate the state dir.
"""

import os
import subprocess
import sys

from dnsimple_cli import storage

LOADED = "import os, dnsimple_cli.cli; print(os.environ.get('DNSIMPLE_ACCOUNT_ID', ''))"


def test_state_dir_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("DNSIMPLE_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert storage.get_state_dir() == tmp_path / ".simt" / "dnsimple"


def test_state_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DNSIMPLE_STATE_DIR", str(tmp_path / "elsewhere"))
    assert storage.get_state_dir() == tmp_path / "elsewhere"


def test_db_lives_in_state_dir_not_cwd(monkeypatch, tmp_path):
    state, cwd = tmp_path / "state", tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("DNSIMPLE_STATE_DIR", str(state))
    monkeypatch.chdir(cwd)
    assert storage.get_db_path() == state / "data" / "db.json"
    assert (state / "data").is_dir()
    assert list(cwd.iterdir()) == [], "nothing may be created in the current directory"


def _run(env_overrides, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DNSIMPLE_")}
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-c", LOADED], cwd=cwd, env=env,
                          capture_output=True, text=True, check=True).stdout.strip()


def test_credentials_from_state_dir_win_over_cwd_env(tmp_path):
    state, cwd = tmp_path / "state", tmp_path / "cwd"
    state.mkdir()
    cwd.mkdir()
    (state / "dnsimple.env").write_text("DNSIMPLE_ACCOUNT_ID=from-state\n")
    (cwd / ".env").write_text("DNSIMPLE_ACCOUNT_ID=from-cwd\n")
    assert _run({"DNSIMPLE_STATE_DIR": str(state)}, cwd) == "from-state"


def test_credentials_fall_back_to_cwd_env(tmp_path):
    state, cwd = tmp_path / "state", tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("DNSIMPLE_ACCOUNT_ID=from-cwd\n")
    assert _run({"DNSIMPLE_STATE_DIR": str(state)}, cwd) == "from-cwd"


def test_environment_wins_over_files(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "dnsimple.env").write_text("DNSIMPLE_ACCOUNT_ID=from-state\n")
    out = _run({"DNSIMPLE_STATE_DIR": str(state), "DNSIMPLE_ACCOUNT_ID": "from-env"}, tmp_path)
    assert out == "from-env"
