"""This repo is licence-clean: no name of the organisation it grew in, anywhere in it.

Every text file is read — code, docs, tests — and checked against the workspace's private
denylist. The list is itself sensitive, so it is never in this repo: it lives at
`../_boundary/denylist.txt` (the simt-commons workspace root is not a git repo) or at
`$COMMONS_DENYLIST`. Where neither exists (CI, another machine) the name check skips.
"Simtricity" may appear only as the copyright holder ("Simtricity Limited"), its GitHub
org or its package scope; that check needs no list and always runs.
"""

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DENY = Path(os.environ.get("COMMONS_DENYLIST") or ROOT.parent / "_boundary" / "denylist.txt")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "data", "reports"}
SKIP_FILES = {"uv.lock"}
BINARY = {".png", ".pdf", ".pyc", ".ico"}
HOLDER = "Simtricity Limited"
# The organisation's name may also appear as its GitHub org or package scope.
ALLOWED = (HOLDER, "github.com/simtricity/", "@simtricity-commons")


def words():
    if not DENY.is_file():
        pytest.skip(f"private denylist not found at {DENY} (set COMMONS_DENYLIST)")
    return [w.strip().lower() for w in DENY.read_text().splitlines()
            if w.strip() and not w.startswith("#")]


def texts():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix in BINARY or p.name in SKIP_FILES:
            continue
        if SKIP_DIRS.intersection(p.relative_to(ROOT).parts):
            continue
        yield p, p.read_text(encoding="utf-8", errors="replace")


def test_no_denylisted_name_appears():
    pats = [(w, re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", re.I))
            for w in words()]
    found = []
    for p, text in texts():
        if p == Path(__file__).resolve():
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for w, pat in pats:
                if pat.search(line):
                    found.append(f"{p.relative_to(ROOT)}:{n}: {w!r}")
    assert not found, "denylisted names in the repo:\n" + "\n".join(found[:40])


def test_simtricity_only_as_the_copyright_holder():
    found = []
    for p, text in texts():
        if p == Path(__file__).resolve():
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"simtricity", line, re.I) and not any(a in line for a in ALLOWED):
                found.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    assert not found, "Simtricity outside the copyright holder:\n" + "\n".join(found[:40])


def test_the_denylist_is_read():
    assert len(words()) > 15


def test_no_denylist_file_inside_the_repo():
    """The list must never be committed: it names exactly what it protects."""
    inside = [p for p, _ in texts() if "denylist" in p.name.lower()]
    assert not inside, f"a denylist file is inside the repo: {inside}"
