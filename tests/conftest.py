"""Test isolation from the developer's own machine.

The suite reads settings through :mod:`vnote.config`, which resolves
``VNOTE_*`` env vars and ``$XDG_CONFIG_HOME/vnote/config.json``. Without this,
tests assert against whatever the developer happens to have configured:
``test_opencode_runs_a_tool_free_agent_in_a_sandbox`` asserts no ``--model``
flag is passed, and went red the moment a real `vnote --setup` saved an
`opencode_model` (2026-08-26). CI never caught it — a fresh runner has no
config file — so the failure only ever appears on a machine that uses vnote.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Point config resolution at an empty dir and drop every VNOTE_* override.

    Autouse and applied before each test body, so a test that sets its own
    XDG_CONFIG_HOME or VNOTE_* value still wins.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    for key in [k for k in os.environ if k.startswith("VNOTE_")]:
        monkeypatch.delenv(key, raising=False)
