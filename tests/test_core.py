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
        self.assertEqual(core._bar(100), "[████████████] 100% left")
        self.assertEqual(core._bar(0), "[░░░░░░░░░░░░] 0% left")
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


class UsageCacheTests(unittest.TestCase):
    def setUp(self):
        core._USAGE_CACHE.clear()

    def test_fetches_and_caches_then_serves_within_ttl(self):
        calls = []
        def fetch():
            calls.append(1)
            return {"n": len(calls)}
        clock = [1000.0]
        with patch("cli_usage_core.time.monotonic", lambda: clock[0]):
            d1, a1, s1 = core._cached_usage("x", fetch, ttl=300)
            clock[0] = 1100.0  # 100s later, within TTL -> cache hit, no fetch
            d2, a2, s2 = core._cached_usage("x", fetch, ttl=300)
        self.assertEqual(d1, {"n": 1})
        self.assertEqual(d2, {"n": 1})
        self.assertEqual(len(calls), 1)
        self.assertFalse(s2)
        self.assertAlmostEqual(a2, 100.0)

    def test_refetches_after_ttl(self):
        calls = []
        def fetch():
            calls.append(1)
            return {"n": len(calls)}
        clock = [1000.0]
        with patch("cli_usage_core.time.monotonic", lambda: clock[0]):
            core._cached_usage("x", fetch, ttl=300)
            clock[0] = 1400.0  # 400s later, past TTL -> refetch
            d, a, s = core._cached_usage("x", fetch, ttl=300)
        self.assertEqual(len(calls), 2)
        self.assertEqual(d, {"n": 2})
        self.assertFalse(s)

    def test_serves_stale_on_error(self):
        state = {"fail": False}
        def fetch():
            if state["fail"]:
                raise RuntimeError("429")
            return {"ok": True}
        clock = [1000.0]
        with patch("cli_usage_core.time.monotonic", lambda: clock[0]):
            core._cached_usage("x", fetch, ttl=300)  # cache a good value
            clock[0] = 1400.0                         # past TTL -> refetch attempt
            state["fail"] = True
            d, a, s = core._cached_usage("x", fetch, ttl=300)
        self.assertEqual(d, {"ok": True})  # last good value, not blanked
        self.assertTrue(s)
        self.assertAlmostEqual(a, 400.0)

    def test_error_without_cache_raises(self):
        def fetch():
            raise RuntimeError("boom")
        with patch("cli_usage_core.time.monotonic", lambda: 1000.0):
            with self.assertRaises(RuntimeError):
                core._cached_usage("y", fetch)

    def test_fmt_age(self):
        self.assertEqual(core._fmt_age(30), "30s")
        self.assertEqual(core._fmt_age(120), "2m")
        self.assertEqual(core._fmt_age(7200), "2h")


class ApiShapeTests(unittest.TestCase):
    # ── Anthropic limits[] array ──────────────────────────────────────────
    def test_claude_limit_label_session_and_weekly(self):
        self.assertEqual(core._claude_limit_label({"kind": "session"}), ("5h limit", "5h"))
        self.assertEqual(core._claude_limit_label({"kind": "weekly_all"}), ("Weekly limit", "week"))

    def test_claude_limit_label_scoped_model(self):
        entry = {"kind": "weekly_scoped", "scope": {"model": {"display_name": "Fable"}}}
        self.assertEqual(core._claude_limit_label(entry), ("Weekly Fable", "week"))

    def test_claude_limit_label_generic_fallback(self):
        self.assertEqual(core._claude_limit_label({"kind": "monthly_all", "group": "monthly"}),
                         ("Monthly", "week"))

    def test_validate_claude_usage_accepts_limits_array(self):
        payload = {"limits": [{"kind": "weekly_scoped", "percent": 23,
                               "scope": {"model": {"display_name": "Fable"}}}]}
        self.assertIs(core.validate_claude_usage(payload), payload)

    def test_validate_claude_usage_rejects_bad_limit_percent(self):
        with self.assertRaises(core.ProviderResponseError):
            core.validate_claude_usage({"limits": [{"percent": "nope"}]})

    # ── Codex window labels from duration ─────────────────────────────────
    def test_codex_window_label_by_duration(self):
        self.assertEqual(core._codex_window_label({"limit_window_seconds": 18000}, "x", "y"),
                         ("5h limit", "5h"))
        self.assertEqual(core._codex_window_label({"limit_window_seconds": 604800}, "x", "y"),
                         ("Weekly limit", "week"))
        self.assertEqual(core._codex_window_label({"limit_window_seconds": 259200}, "x", "y"),
                         ("3d limit", "week"))

    def test_codex_window_label_fallback_when_absent(self):
        self.assertEqual(core._codex_window_label({}, "5h limit", "5h"), ("5h limit", "5h"))
        self.assertEqual(core._codex_window_label({"limit_window_seconds": None}, "Weekly limit", "week"),
                         ("Weekly limit", "week"))


if __name__ == "__main__":
    unittest.main()
