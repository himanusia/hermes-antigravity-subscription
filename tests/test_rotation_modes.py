"""Tests for persistent rotation modes, account pinning, session stickiness, and quota ignition."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import accounts  # noqa: E402
from accounts import (  # noqa: E402
    VALID_ROTATION_MODES,
    add_account,
    clear_session_pin,
    get_rotation_mode,
    is_quota_ignition_enabled,
    maybe_quota_ignition,
    pick_account,
    set_active,
    set_cooldown,
    set_rotation_mode,
)


def usage(gemini_5h: float, gemini_week: float, claude_5h: float = 1.0, claude_week: float = 1.0):
    return {
        "gemini": {"5h": {"remaining_fraction": gemini_5h}, "weekly": {"remaining_fraction": gemini_week}},
        "claude_gpt": {"5h": {"remaining_fraction": claude_5h}, "weekly": {"remaining_fraction": claude_week}},
    }


class RotationModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_ACCOUNTS_FILE": str(Path(self.tmp.name) / "accounts.json"),
                "ANTIGRAVITY_ACCOUNTS_DIR": str(Path(self.tmp.name) / "dir"),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("ANTIGRAVITY_ROTATION", None)
        os.environ.pop("ANTIGRAVITY_ACCOUNT", None)

    def test_registry_mode_is_persisted_and_read_back(self):
        self.assertEqual(get_rotation_mode(), "off")
        self.assertTrue(set_rotation_mode("round_robin"))
        self.assertEqual(get_rotation_mode(), "round_robin")

    def test_env_overrides_registry_mode(self):
        set_rotation_mode("round_robin")
        with patch.dict(os.environ, {"ANTIGRAVITY_ROTATION": "quota"}):
            self.assertEqual(get_rotation_mode(), "quota")
        self.assertEqual(get_rotation_mode(), "round_robin")

    def test_invalid_mode_is_rejected(self):
        self.assertFalse(set_rotation_mode("bogus"))
        self.assertIn("fixed", VALID_ROTATION_MODES)
        self.assertEqual(get_rotation_mode(), "off")

    def test_unknown_env_mode_falls_back_to_registry(self):
        set_rotation_mode("quota")
        with patch.dict(os.environ, {"ANTIGRAVITY_ROTATION": "nonsense"}):
            self.assertEqual(get_rotation_mode(), "quota")


class FixedModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_ACCOUNTS_FILE": str(Path(self.tmp.name) / "accounts.json"),
                "ANTIGRAVITY_ACCOUNTS_DIR": str(Path(self.tmp.name) / "dir"),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("ANTIGRAVITY_ROTATION", None)
        os.environ.pop("ANTIGRAVITY_ACCOUNT", None)

    def test_fixed_mode_uses_active_account(self):
        add_account("acc1", "/path/1", eligible=True, email="a@example.com")
        add_account("acc2", "/path/2", eligible=True, email="b@example.com")
        set_active("acc2")
        set_rotation_mode("fixed")
        self.assertEqual(pick_account()["label"], "acc2")

    def test_env_pin_overrides_active_account(self):
        add_account("acc1", "/path/1", eligible=True)
        add_account("acc2", "/path/2", eligible=True)
        set_active("acc2")
        set_rotation_mode("fixed")
        with patch.dict(os.environ, {"ANTIGRAVITY_ACCOUNT": "acc1"}):
            self.assertEqual(pick_account()["label"], "acc1")

    def test_fixed_mode_fails_open_when_pin_unavailable(self):
        add_account("acc1", "/path/1", eligible=False)
        set_active("acc1")
        set_rotation_mode("fixed")
        self.assertIsNone(pick_account())

    def test_fixed_mode_without_pin_falls_back_to_host(self):
        add_account("acc1", "/path/1", eligible=True)
        set_rotation_mode("fixed")
        self.assertIsNone(pick_account())


class SessionStickinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_ACCOUNTS_FILE": str(Path(self.tmp.name) / "accounts.json"),
                "ANTIGRAVITY_ACCOUNTS_DIR": str(Path(self.tmp.name) / "dir"),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("ANTIGRAVITY_ROTATION", None)
        os.environ.pop("ANTIGRAVITY_SESSION_STICKINESS", None)
        add_account("rich", "/path/rich", eligible=True)
        add_account("poor", "/path/poor", eligible=True)
        set_rotation_mode("quota")
        clear_session_pin("session-a")

    def test_session_keeps_its_first_account(self):
        def fake_usage(home, cached=True):
            return usage(1.0, 1.0) if home.endswith("rich") else usage(0.2, 0.3)

        with patch("accounts.fetch_usage_for_home", side_effect=fake_usage):
            first = pick_account(model="gemini-3.8-flash", session_id="session-a")
            # "poor" suddenly becomes the healthier account for a fresh session.
            def flipped(home, cached=True):
                return usage(0.2, 0.3) if home.endswith("rich") else usage(1.0, 1.0)

            with patch("accounts.fetch_usage_for_home", side_effect=flipped):
                sticky = pick_account(model="gemini-3.8-flash", session_id="session-a")
                fresh = pick_account(model="gemini-3.8-flash", session_id="session-b")
        clear_session_pin("session-b")
        self.assertEqual(first["label"], "rich")
        self.assertEqual(sticky["label"], "rich")  # pinned for the session
        self.assertEqual(fresh["label"], "poor")  # a new session re-picks

    def test_stickiness_can_be_disabled(self):
        def fake_usage(home, cached=True):
            return usage(1.0, 1.0) if home.endswith("rich") else usage(0.2, 0.3)

        with patch("accounts.fetch_usage_for_home", side_effect=fake_usage):
            pick_account(model="gemini-3.8-flash", session_id="session-a")
            with patch.dict(os.environ, {"ANTIGRAVITY_SESSION_STICKINESS": "0"}):
                def flipped(home, cached=True):
                    return usage(0.2, 0.3) if home.endswith("rich") else usage(1.0, 1.0)

                with patch("accounts.fetch_usage_for_home", side_effect=flipped):
                    reselected = pick_account(model="gemini-3.8-flash", session_id="session-a")
        self.assertEqual(reselected["label"], "poor")


class QuotaIgnitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_ACCOUNTS_FILE": str(Path(self.tmp.name) / "accounts.json"),
                "ANTIGRAVITY_ACCOUNTS_DIR": str(Path(self.tmp.name) / "dir"),
                "ANTIGRAVITY_QUOTA_IGNITION": "1",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home = Path(self.tmp.name) / "idle-home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.account = {"label": "idle", "home_dir": str(self.home), "enabled": True, "eligible": True}

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_QUOTA_IGNITION": "0"}):
            self.assertFalse(is_quota_ignition_enabled())
            with patch("accounts.subprocess.run") as run:
                self.assertFalse(maybe_quota_ignition(self.account, usage(1.0, 1.0)))
        run.assert_not_called()

    def test_fires_on_idle_account(self):
        with patch.dict(accounts._IGNITION_FIRED, {}, clear=True), patch(
            "accounts.subprocess.run", return_value=MagicMock(returncode=0)
        ) as run:
            self.assertTrue(maybe_quota_ignition(self.account, usage(1.0, 1.0)))
        run.assert_called_once()

    def test_skips_when_account_has_recent_usage(self):
        with patch.dict(accounts._IGNITION_FIRED, {}, clear=True), patch("accounts.subprocess.run") as run:
            self.assertFalse(maybe_quota_ignition(self.account, usage(0.4, 0.9)))
        run.assert_not_called()

    def test_skips_ineligible_account(self):
        ineligible = dict(self.account, eligible=False)
        with patch.dict(accounts._IGNITION_FIRED, {}, clear=True), patch("accounts.subprocess.run") as run:
            self.assertFalse(maybe_quota_ignition(ineligible, usage(1.0, 1.0)))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
