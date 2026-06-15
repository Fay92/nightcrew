"""Shared fixtures: isolated nightcrew home + fake claude binary."""

import os
from pathlib import Path

import pytest

from nightcrew.config import Config

FAKES_DIR = Path(__file__).parent / "fakes"
FAKE_CLAUDE = FAKES_DIR / "claude"


@pytest.fixture
def nightcrew_home(tmp_path, monkeypatch):
    """Point NIGHTCREW_HOME at a temp dir so tests never touch real state."""
    home = tmp_path / "nightcrew-home"
    monkeypatch.setenv("NIGHTCREW_HOME", str(home))
    for var in (
        "FAKE_CLAUDE_MODE",
        "FAKE_CLAUDE_SESSION_ID",
        "FAKE_CLAUDE_LIMIT_TEXT",
        "FAKE_CLAUDE_ARGS_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def fake_claude():
    assert FAKE_CLAUDE.exists(), "fake claude stub missing"
    assert os.access(FAKE_CLAUDE, os.X_OK), "fake claude stub must be executable"
    return str(FAKE_CLAUDE)


@pytest.fixture
def config(nightcrew_home, fake_claude):
    cfg = Config.load(home=nightcrew_home)
    cfg.claude_bin = fake_claude
    cfg.ensure_dirs()
    return cfg
