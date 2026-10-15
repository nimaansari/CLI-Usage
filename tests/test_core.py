import io
import json
import urllib.error
import unittest
from unittest.mock import Mock, patch

import cli_usage_core as core


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class CoreFormattingTests(unittest.TestCase):
    def test_bar_and_status_icons(self):
        self.assertEqual(core._bar(100), "██████████  100% left")
        self.assertEqual(core._bar(0), "░░░░░░░░░░  0% left")
        self.assertEqual(core._bar(None), "")
        self.assertEqual(core._status_icon(80), "🟢")
        self.assertEqual(core._status_icon(20), "🟡")
        self.assertEqual(core._status_icon(5), "🔴")
        self.assertEqual(core._status_icon(None), "⚪")

    def test_limit_row_contains_colored_icon(self):
        self.assertIn("🟡", core._limit_row("5h limit", 75, None, "5h"))
        self.assertIn("25% left", core._limit_row("5h limit", 75, None, "5h"))

    def test_worst_remaining_pct(self):
        data = {
            "Claude Code": {"rows": [("  🟢 5h limit [██] 80% left", False, None)]},
            "Codex CLI": {"rows": [("  🔴 Weekly [█] 8% left", False, None)]},
        }
        self.assertEqual(core.worst_remaining_pct(data), 8)


    def test_reset_str_relative_and_absolute(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(core._reset_str((now + timedelta(hours=2, minutes=5)).isoformat(), "5h", now), "resets in 2h 05m")
        self.assertEqual(core._reset_str((now + timedelta(minutes=20)).isoformat(), "5h", now), "resets in 20m")
        self.assertTrue(core._reset_str((now + timedelta(days=3)).isoformat(), "week", now).startswith("resets "))
        self.assertEqual(core._reset_str((now - timedelta(minutes=1)).isoformat(), "5h", now), "resetting now")
        self.assertEqual(core._reset_str(None, "5h", now), "")

    def test_provider_summary(self):
        self.assertEqual(core.provider_summary({"installed": False}), "  —  not installed")
        info = {"installed": True, "rows": [(core._limit_row("5h", 20, None, "5h"), False, None),
                                            (core._limit_row("Weekly", 70, None, "week"), False, None)]}
        self.assertEqual(core.provider_summary(info), "  —  30% left")
        self.assertEqual(core.provider_summary({"installed": True, "rows": [("  ⚠  login expired", False, None)]}),
                         "  —  needs attention")

    def test_usage_error_rows(self):
        e401 = urllib.error.HTTPError("u", 401, "unauth", {}, io.BytesIO())
        self.assertIn("claude /login", core._usage_error_rows(e401, "claude /login")[0][0])
        e500 = urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO())
        self.assertIn("HTTP 500", core._usage_error_rows(e500, "x")[0][0])

    def test_cached_usage_serves_last_good_on_failure(self):
        core._USAGE_CACHE.clear()
        data, _, stale = core._cached_usage("t", "tok", lambda: {"ok": 1}, ttl=0)
        self.assertEqual((data, stale), ({"ok": 1}, False))

        def boom():
            raise OSError("down")
        data, _, stale = core._cached_usage("t", "tok", boom, ttl=0)
        self.assertEqual((data, stale), ({"ok": 1}, True))
        with self.assertRaises(OSError):
            core._cached_usage("t", "other-token", boom, ttl=0)
        core._USAGE_CACHE.clear()


    def test_codex_local_tokens_counts_increases_once(self):
        import tempfile
        from pathlib import Path
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        def ev(inp, out):
            return json.dumps({"timestamp": now, "type": "event_msg", "payload": {"type": "token_count",
                               "info": {"total_token_usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": 999999}}}})
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "sessions" / "2026" / "10" / "12"
            d.mkdir(parents=True)
            # 100 -> 100 (repeat) -> 250: 250 tokens in total
            (d / "rollout-a.jsonl").write_text("\n".join([ev(90, 10), ev(90, 10), ev(220, 30)]) + "\n")
            core._CODEX_FILE_CACHE.clear()
            with patch("cli_usage_core.codex_home", return_value=Path(tmp)):
                rows = [r[0] for r in core._codex_local_token_rows()]
        self.assertIn("250 tok", rows[0])
        self.assertIn("250 tok", rows[1])


class ValidationTests(unittest.TestCase):
    def test_validate_claude_usage_accepts_expected_shape(self):
        payload = {"five_hour": {"utilization": 20}, "extra_usage": {"utilization": 0}}
        self.assertIs(core.validate_claude_usage(payload), payload)

    def test_validate_claude_usage_rejects_bad_shape(self):
        with self.assertRaises(core.ProviderResponseError):
            core.validate_claude_usage({"five_hour": {"utilization": "nope"}})

    def test_validate_codex_usage_accepts_expected_shape(self):
        payload = {
            "rate_limit": {"primary_window": {"used_percent": 10}},
            "additional_rate_limits": [
                {"rate_limit": {"secondary_window": {"used_percent": 30}}}
            ],
            "credits": {"has_credits": True},
        }
        self.assertIs(core.validate_codex_usage(payload), payload)

    def test_validate_codex_usage_rejects_bad_shape(self):
        with self.assertRaises(core.ProviderResponseError):
            core.validate_codex_usage({"additional_rate_limits": "wrong"})


class HttpTests(unittest.TestCase):
    @patch("cli_usage_core.time.sleep", return_value=None)
    @patch("cli_usage_core.urllib.request.urlopen")
    def test_http_json_gives_up_on_long_retry_after(self, urlopen, sleep):
        error = urllib.error.HTTPError("url", 429, "rate limited", {"Retry-After": "3600"}, io.BytesIO())
        urlopen.side_effect = error
        with self.assertRaises(urllib.error.HTTPError):
            core._http_json("https://example.test", {}, retries=3)
        sleep.assert_not_called()

    @patch("cli_usage_core.time.sleep", return_value=None)
    @patch("cli_usage_core.urllib.request.urlopen")
    def test_http_json_retries_429_then_succeeds(self, urlopen, _sleep):
        headers = {"Retry-After": "0"}
        error = urllib.error.HTTPError("url", 429, "rate limited", headers, io.BytesIO())
        urlopen.side_effect = [error, FakeResponse({"ok": True})]
        self.assertEqual(core._http_json("https://example.test", {}, retries=2), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)

    @patch("cli_usage_core.time.sleep", return_value=None)
    @patch("cli_usage_core.urllib.request.urlopen")
    def test_http_json_does_not_retry_400(self, urlopen, _sleep):
        error = urllib.error.HTTPError("url", 400, "bad", {}, io.BytesIO())
        urlopen.side_effect = error
        with self.assertRaises(urllib.error.HTTPError):
            core._http_json("https://example.test", {}, retries=3)
        self.assertEqual(urlopen.call_count, 1)


class RateLimitBackoffTests(unittest.TestCase):
    def setUp(self):
        core._USAGE_CACHE.clear()
        core._BLOCKED_UNTIL.clear()

    def test_429_blocks_further_fetches_until_retry_after(self):
        error = urllib.error.HTTPError("u", 429, "rate limited", {"Retry-After": "600"}, io.BytesIO())
        fetch = Mock(side_effect=error)
        with self.assertRaises(core.RateLimited):
            core._cached_usage("p", "tok", fetch)
        with self.assertRaises(core.RateLimited):
            core._cached_usage("p", "tok", fetch)
        self.assertEqual(fetch.call_count, 1)  # second call did not hit the network
        rows = core._usage_error_rows(core.RateLimited(0), "x")
        self.assertIn("next try at", rows[0][0])

    def test_429_serves_last_good_payload(self):
        core._cached_usage("p", "tok", lambda: {"ok": 1}, ttl=0)
        error = urllib.error.HTTPError("u", 429, "rate limited", {"Retry-After": "600"}, io.BytesIO())
        data, _age, stale = core._cached_usage("p", "tok", Mock(side_effect=error), ttl=0)
        self.assertEqual(data, {"ok": 1})
        self.assertTrue(stale)


class ClaudeCredentialTests(unittest.TestCase):
    def test_credentials_file_without_claude_login_is_not_used(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            creds = Path(d) / ".credentials.json"
            creds.write_text(json.dumps({"mcpOAuth": {"x": {"accessToken": "mcp"}}}))
            with patch.object(core, "IS_MAC", False):
                self.assertIsNone(core._claude_oauth(creds))
            creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "subscriptionType": "max"}}))
            self.assertEqual(core._claude_oauth(creds)["subscriptionType"], "max")

    def test_env_token_is_used(self):
        core._SHELL_TOKEN.update(at=None, value=None)
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "envtok"}):
            self.assertEqual(core._claude_env_token(), "envtok")


if __name__ == "__main__":
    unittest.main()


class ClaudeHeaderFallbackTests(unittest.TestCase):
    @patch.object(core.urllib.request, "urlopen")
    def test_header_usage_parses_unified_headers(self, urlopen):
        resp = Mock()
        resp.headers = {
            "anthropic-ratelimit-unified-5h-utilization": "0.03",
            "anthropic-ratelimit-unified-5h-reset": "1792098000",
            "anthropic-ratelimit-unified-7d-utilization": "0.5",
            "anthropic-ratelimit-unified-7d-reset": "1792677600",
        }
        urlopen.return_value.__enter__ = Mock(return_value=resp)
        urlopen.return_value.__exit__ = Mock(return_value=False)
        u = core._claude_header_usage("tok")
        self.assertAlmostEqual(u["five_hour"]["utilization"], 3.0)
        self.assertEqual(u["seven_day"]["resets_at"], 1792677600)
        self.assertIn("50% left", core._limit_row("Weekly limit", u["seven_day"]["utilization"],
                                                 u["seven_day"]["resets_at"], "week"))

    def test_auth_errors_skip_the_fallback(self):
        e401 = urllib.error.HTTPError("u", 401, "unauth", {}, io.BytesIO())
        self.assertTrue(core._is_auth_error(e401))
        self.assertFalse(core._is_auth_error(core.RateLimited(0)))
