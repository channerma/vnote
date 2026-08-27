"""Shared fixtures: no test may read the developer's real config file or VNOTE_* environment."""

import pytest

from vnote import config


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for s in config.SETTINGS:
        monkeypatch.delenv(s.env, raising=False)
