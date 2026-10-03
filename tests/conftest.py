"""Isolate the suite from the developer's real Antigravity registry and env.

Without this, tests silently inherit whatever the local machine has configured
(a real registry with accounts, a persistent rotation mode, a pinned account),
so the same commit can pass on CI and fail on a configured machine — or vice
versa. Every test starts from: no registered accounts, rotation off.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_antigravity_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("ANTIGRAVITY_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setenv("ANTIGRAVITY_ROTATION", "off")
    monkeypatch.delenv("ANTIGRAVITY_ACCOUNT", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_FAILOVER", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_SESSION_STICKINESS", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_QUOTA_IGNITION", raising=False)
    yield
