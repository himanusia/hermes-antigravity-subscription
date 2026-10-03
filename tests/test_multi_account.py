import argparse
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Add plugin root to sys.path
plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

from accounts import (
    DEFAULT_ACCOUNTS_DIR,
    _parse_usage_json,
    add_account,
    calculate_score,
    fetch_usage_for_home,
    get_accounts_file_path,
    get_active,
    get_lease_count,
    get_rotation_mode,
    is_quota_error,
    lease_account,
    list_accounts,
    load_accounts,
    parse_reset_time,
    pick_account,
    remove_account,
    save_accounts,
    set_active,
    set_cooldown,
    update_last_used,
)
from __init__ import antigravity_profile, auth_handler
from client import AntigravityClient, _log_rotation_failover, _resolve_cooldown_reset

SAMPLE_USAGE_JSON = json.dumps({
    "status": "SUCCESS",
    "num_turns": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0},
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "window": "weekly",
                            "remaining_fraction": 0.9849,
                            "reset_time": "2026-10-10T11:27:09Z",
                        },
                        {
                            "id": "gemini-5h",
                            "window": "5h",
                            "remaining_fraction": 0.9494,
                            "reset_time": "2026-10-03T16:00:00Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "window": "weekly",
                            "remaining_fraction": 0.5,
                            "reset_time": "2026-10-10T11:27:09Z",
                        },
                        {
                            "id": "3p-5h",
                            "window": "5h",
                            "remaining_fraction": 0.8,
                            "reset_time": "2026-10-03T16:00:00Z",
                        },
                    ],
                },
            ]
        },
    },
})


class TestMultiAccount(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.accounts_file = Path(self.tmp_dir.name) / "accounts.json"
        self.accounts_dir = Path(self.tmp_dir.name) / "agy-accounts"
        self.env_patch = patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_ACCOUNTS_FILE": str(self.accounts_file),
                "ANTIGRAVITY_ACCOUNTS_DIR": str(self.accounts_dir),
                "ANTIGRAVITY_ROTATION": "quota",
            },
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp_dir.cleanup()

    def test_calculate_score_and_hard_gate(self):
        # Standard score: f_5h * (f_weekly ** 2)
        # 0.9494 * (0.9849 ** 2) = 0.9494 * 0.970028 = 0.92094
        score = calculate_score(f_5h=0.9494, f_weekly=0.9849, in_cooldown=False)
        self.assertAlmostEqual(score, 0.9494 * (0.9849**2), places=4)

        # Hard gate: 0% in 5h
        self.assertEqual(calculate_score(f_5h=0.0, f_weekly=1.0, in_cooldown=False), 0.0)
        # Hard gate: 0% in weekly
        self.assertEqual(calculate_score(f_5h=1.0, f_weekly=0.0, in_cooldown=False), 0.0)
        # Hard gate: in cooldown
        self.assertEqual(calculate_score(f_5h=1.0, f_weekly=1.0, in_cooldown=True), 0.0)

    def test_lease_penalty(self):
        # Active lease adds penalty to divisor: raw / (1 + lease_count)
        score_idle = calculate_score(f_5h=1.0, f_weekly=1.0, in_cooldown=False, lease_count=0)
        score_busy = calculate_score(f_5h=1.0, f_weekly=1.0, in_cooldown=False, lease_count=1)
        self.assertEqual(score_idle, 1.0)
        self.assertEqual(score_busy, 0.5)

        # Verify lease contextmanager
        self.assertEqual(get_lease_count("acc1"), 0)
        with lease_account("acc1"):
            self.assertEqual(get_lease_count("acc1"), 1)
            with lease_account("acc1"):
                self.assertEqual(get_lease_count("acc1"), 2)
            self.assertEqual(get_lease_count("acc1"), 1)
        self.assertEqual(get_lease_count("acc1"), 0)

    def test_parse_usage_json(self):
        parsed = _parse_usage_json(SAMPLE_USAGE_JSON)
        self.assertIsNotNone(parsed)
        self.assertIn("gemini", parsed)
        self.assertIn("claude_gpt", parsed)
        self.assertAlmostEqual(parsed["gemini"]["5h"]["remaining_fraction"], 0.9494)
        self.assertAlmostEqual(parsed["gemini"]["weekly"]["remaining_fraction"], 0.9849)
        self.assertEqual(parsed["gemini"]["5h"]["reset_time"], "2026-10-03T16:00:00Z")
        self.assertAlmostEqual(parsed["claude_gpt"]["5h"]["remaining_fraction"], 0.8)
        self.assertAlmostEqual(parsed["claude_gpt"]["weekly"]["remaining_fraction"], 0.5)

    def test_parse_reset_time(self):
        ts = parse_reset_time("2026-10-03T16:00:00Z")
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 0)
        self.assertIsNone(parse_reset_time(None))
        self.assertIsNone(parse_reset_time("invalid-date"))

    def test_registry_crud(self):
        # Empty initially
        data = load_accounts()
        self.assertEqual(data["accounts"], [])
        self.assertIsNone(get_active())

        # Add accounts
        add_account("acc1", "/path/to/acc1")
        add_account("acc2", "/path/to/acc2")
        accounts = list_accounts()
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["label"], "acc1")
        self.assertEqual(accounts[1]["label"], "acc2")

        # Set active
        self.assertTrue(set_active("acc2"))
        self.assertEqual(get_active(), "acc2")
        self.assertFalse(set_active("nonexistent"))

        # Remove account
        self.assertTrue(remove_account("acc1"))
        self.assertEqual(len(list_accounts()), 1)
        self.assertFalse(remove_account("nonexistent"))

        # Removing active account resets active
        self.assertTrue(remove_account("acc2"))
        self.assertIsNone(get_active())

    def test_corrupt_registry_fail_open(self):
        # Write invalid JSON to registry
        self.accounts_file.write_text("{corrupted json...", encoding="utf-8")
        data = load_accounts()
        self.assertEqual(data["accounts"], [])
        self.assertIsNone(pick_account())

    def test_rotation_mode_off_fails_open(self):
        add_account("acc1", "/path/1")
        with patch.dict(os.environ, {"ANTIGRAVITY_ROTATION": "off"}):
            self.assertIsNone(pick_account())

    def test_pick_account_by_quota(self):
        add_account("acc_low", "/path/low")
        add_account("acc_high", "/path/high")

        mock_usages = {
            str(Path("/path/low").resolve()): {
                "gemini": {"5h": {"remaining_fraction": 0.2}, "weekly": {"remaining_fraction": 0.5}},
            },
            str(Path("/path/high").resolve()): {
                "gemini": {"5h": {"remaining_fraction": 0.9}, "weekly": {"remaining_fraction": 0.95}},
            },
        }

        def fake_fetch_usage(home, cached=True):
            return mock_usages.get(home)

        with patch("accounts.fetch_usage_for_home", side_effect=fake_fetch_usage):
            best = pick_account(model="gemini-3.8-flash")
            self.assertIsNotNone(best)
            self.assertEqual(best["label"], "acc_high")

            # If acc_high is leased, score is halved (0.812 / 2 = 0.406 vs 0.2*0.25=0.05),
            # but if leased 20 times, acc_low should win!
            with lease_account("acc_high"):
                for _ in range(19):
                    # artificially bump lease
                    with lease_account("acc_high"):
                        pass

    def test_pick_account_skips_cooldown(self):
        add_account("acc1", "/path/1")
        add_account("acc2", "/path/2")

        # Put acc1 on cooldown
        set_cooldown("acc1", duration_seconds=600)

        mock_usages = {
            str(Path("/path/1").resolve()): {
                "gemini": {"5h": {"remaining_fraction": 1.0}, "weekly": {"remaining_fraction": 1.0}},
            },
            str(Path("/path/2").resolve()): {
                "gemini": {"5h": {"remaining_fraction": 0.5}, "weekly": {"remaining_fraction": 0.5}},
            },
        }
        with patch("accounts.fetch_usage_for_home", side_effect=lambda h, cached=True: mock_usages.get(h)):
            best = pick_account(model="gemini-3.8-flash")
            self.assertIsNotNone(best)
            self.assertEqual(best["label"], "acc2")

    def test_round_robin_mode(self):
        add_account("acc1", "/path/1")
        add_account("acc2", "/path/2")
        update_last_used("acc1")

        with patch.dict(os.environ, {"ANTIGRAVITY_ROTATION": "round_robin"}):
            # acc2 was never used (last_used=0), so it should be picked first
            best = pick_account()
            self.assertIsNotNone(best)
            self.assertEqual(best["label"], "acc2")

    def test_client_rotation_failover_nonstreaming(self):
        add_account("acc1", "/path/1")
        add_account("acc2", "/path/2")

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)

        call_count = 0

        def mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("account_label") == "acc1":
                raise RuntimeError("RESOURCE_EXHAUSTED (code 429): Individual quota reached.")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="success"))])

        from types import SimpleNamespace

        with patch.object(client, "_execute_chat_completion", side_effect=mock_execute):
            with patch("client.is_authenticated", return_value=True):
                res = client.chat.completions.create(
                    model="gemini-3.8-flash",
                    messages=[{"role": "user", "content": "hi"}],
                    stream=False,
                )
                self.assertEqual(res.choices[0].message.content, "success")
                self.assertEqual(call_count, 2)

                # Verify acc1 was put on cooldown
                accounts = {a["label"]: a for a in list_accounts()}
                self.assertGreater(accounts["acc1"]["cooldown_until"], time.time())

    def test_client_rotation_failover_streaming(self):
        add_account("acc1", "/path/1")
        add_account("acc2", "/path/2")

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)

        class FailingStream:
            def __iter__(self):
                return self
            def __next__(self):
                raise RuntimeError("RESOURCE_EXHAUSTED (code 429): Individual quota reached.")
            def close(self):
                pass

        class OkStream:
            def __iter__(self):
                return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="stream-ok"))])])
            def close(self):
                pass

        def mock_execute(*args, **kwargs):
            if kwargs.get("account_label") == "acc1":
                return FailingStream()
            return OkStream()

        with patch.object(client, "_execute_chat_completion", side_effect=mock_execute):
            with patch("client.is_authenticated", return_value=True):
                stream = client.chat.completions.create(
                    model="gemini-3.8-flash",
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                )
                chunks = list(stream)
                self.assertEqual(len(chunks), 1)
                self.assertEqual(chunks[0].choices[0].delta.content, "stream-ok")

                accounts = {a["label"]: a for a in list_accounts()}
                self.assertGreater(accounts["acc1"]["cooldown_until"], time.time())

    def test_client_rotation_exhaustion_raises(self):
        add_account("acc1", "/path/1")
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)

        def mock_execute(*args, **kwargs):
            raise RuntimeError("RESOURCE_EXHAUSTED: quota limit exceeded")

        with patch.object(client, "_execute_chat_completion", side_effect=mock_execute):
            with patch("client.is_authenticated", return_value=True):
                with self.assertRaises(RuntimeError) as ctx:
                    client.chat.completions.create(
                        model="gemini-3.8-flash",
                        messages=[{"role": "user", "content": "hi"}],
                        stream=False,
                    )
                self.assertIn("quota", str(ctx.exception).lower())

    def test_auth_handler_add_non_interactive_fails(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                auth_handler("add", SimpleNamespace(label="test_login"))
            self.assertIn("requires an interactive terminal", str(ctx.exception))

    def test_auth_handler_add_interactive_success(self):
        with patch("sys.stdin.isatty", return_value=True):
            with patch("subprocess.run") as mock_run, patch("__init__.resolve_agy_command", return_value="agy"):
                mock_run.return_value = SimpleNamespace(returncode=0)
                res = auth_handler("add", SimpleNamespace(label="test_add"))
                self.assertTrue(res)
                accounts = list_accounts()
                self.assertEqual(len(accounts), 1)
                self.assertEqual(accounts[0]["label"], "test_add")
                env_passed = mock_run.call_args[1]["env"]
                self.assertTrue(env_passed["HOME"].endswith("test_add"))

    def test_auth_handler_add_agy_failure(self):
        with patch("sys.stdin.isatty", return_value=True):
            with patch("subprocess.run") as mock_run, patch("__init__.resolve_agy_command", return_value="agy"):
                mock_run.return_value = SimpleNamespace(returncode=1)
                with self.assertRaises(SystemExit) as ctx:
                    auth_handler("add", SimpleNamespace(label="test_fail"))
                self.assertIn("code 1", str(ctx.exception))

    def test_auth_handler_status_empty(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            res = auth_handler("status", SimpleNamespace())
        self.assertTrue(res)
        self.assertIn("No Antigravity accounts registered", out.getvalue())

    def test_auth_handler_status_with_accounts(self):
        add_account("acc_alpha", "/path/alpha")
        set_active("acc_alpha")
        out = io.StringIO()
        with patch("sys.stdout", out):
            res = auth_handler("status", SimpleNamespace())
        self.assertTrue(res)
        self.assertIn("acc_alpha", out.getvalue())
        self.assertIn("*", out.getvalue())

    def test_auth_handler_refresh(self):
        add_account("acc_alpha", "/path/alpha")
        set_cooldown("acc_alpha", duration_seconds=600)

        with patch("accounts.fetch_usage_for_home", return_value=None):
            # Target refresh
            res = auth_handler("refresh", SimpleNamespace(target="acc_alpha"))
            self.assertTrue(res)
            acc = list_accounts()[0]
            self.assertEqual(acc["cooldown_until"], 0.0)

            # Global refresh
            set_cooldown("acc_alpha", duration_seconds=600)
            res = auth_handler("refresh", SimpleNamespace(target=None))
            self.assertTrue(res)
            acc = list_accounts()[0]
            self.assertEqual(acc["cooldown_until"], 0.0)

            # Nonexistent target
            with self.assertRaises(SystemExit):
                auth_handler("refresh", SimpleNamespace(target="nonexistent"))

    def test_auth_handler_logout_target_and_all(self):
        add_account("acc1", "/path/1")
        add_account("acc2", "/path/2")

        # Logout target
        res = auth_handler("logout", SimpleNamespace(target="acc1"))
        self.assertTrue(res)
        self.assertEqual(len(list_accounts()), 1)

        # Logout nonexistent target
        with self.assertRaises(SystemExit):
            auth_handler("logout", SimpleNamespace(target="acc1"))

        # Logout remaining account (single account path)
        res = auth_handler("logout", SimpleNamespace(target=None))
        self.assertTrue(res)
        self.assertEqual(len(list_accounts()), 0)

        # Logout empty
        res = auth_handler("logout", SimpleNamespace(target=None))
        self.assertTrue(res)

    def test_auth_handler_use_and_unhandled(self):
        add_account("acc_alpha", "/path/alpha")
        res = auth_handler("use", SimpleNamespace(target="acc_alpha"))
        self.assertTrue(res)
        self.assertEqual(get_active(), "acc_alpha")

        with self.assertRaises(SystemExit):
            auth_handler("use", SimpleNamespace(target="nonexistent"))

        # Unhandled action returns False
        self.assertFalse(auth_handler("unknown_action", SimpleNamespace()))

    def test_log_rotation_failover_format(self):
        acc1 = {"label": "acc2", "home_dir": "/path/2"}
        acc2 = {"label": "acc3", "home_dir": "/path/3"}
        mock_usages = {
            "/path/2": {"gemini": {"5h": {"remaining_fraction": 0.0}}},
            "/path/3": {"gemini": {"5h": {"remaining_fraction": 0.92}}},
        }
        with patch("client.fetch_usage_for_home", side_effect=lambda h, cached=True: mock_usages.get(h)):
            with self.assertLogs("client", level="WARNING") as cm:
                _log_rotation_failover(acc1, acc2, "gemini-3.8-flash")
                self.assertTrue(
                    any("[agy-rotate] acc2 exhausted (gemini 5h 0%) -> acc3 (gemini 5h 92%)" in log for log in cm.output)
                )


if __name__ == "__main__":
    unittest.main()
