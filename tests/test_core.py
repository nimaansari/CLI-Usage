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

    def test_validate_claude_usage_accepts_limits_array(self):
        payload = {
            "five_hour": {"utilization": 5},
            "limits": [
                {"kind": "weekly_scoped", "group": "weekly", "percent": 23,
                 "scope": {"model": {"display_name": "Fable"}}},
            ],
        }
        self.assertIs(core.validate_claude_usage(payload), payload)

    def test_validate_claude_usage_rejects_bad_limit_percent(self):
        with self.assertRaises(core.ProviderResponseError):
            core.validate_claude_usage({"limits": [{"percent": "nope"}]})


class ClaudeModelBarometerTests(unittest.TestCase):
    def setUp(self):
        core._USAGE_CACHE.clear()  # usage is TTL-cached; isolate each test

    def _rows(self, payload):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with patch("cli_usage_core.shutil.which", return_value="/usr/bin/claude"), \
             patch("cli_usage_core.Path.exists", return_value=True), \
             patch("cli_usage_core.Path.read_text", return_value=creds), \
             patch("cli_usage_core._http_json", return_value=payload):
            return [r[0] for r in core.claude_data()["rows"]]

    def test_weekly_scoped_model_renders_named_row(self):
        rows = self._rows({
            "five_hour": {"utilization": 6, "resets_at": None},
            "seven_day": {"utilization": 29, "resets_at": None},
            "limits": [
                {"group": "weekly", "percent": 23, "resets_at": None,
                 "scope": {"model": {"display_name": "Fable"}}},
            ],
        })
        self.assertTrue(any("Weekly Fable" in r and "77% left" in r for r in rows))

    def test_summary_reports_5h_and_weekly_remaining(self):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        payload = {
            "five_hour": {"utilization": 6, "resets_at": None},
            "seven_day": {"utilization": 29, "resets_at": None},
        }
        with patch("cli_usage_core.shutil.which", return_value="/usr/bin/claude"), \
             patch("cli_usage_core.Path.exists", return_value=True), \
             patch("cli_usage_core.Path.read_text", return_value=creds), \
             patch("cli_usage_core._http_json", return_value=payload):
            summary = core.claude_data()["summary"]
        self.assertEqual(summary, {"5h": 94, "weekly": 71})

    def test_unscoped_limits_do_not_duplicate_rows(self):
        rows = self._rows({
            "seven_day": {"utilization": 29, "resets_at": None},
            "limits": [
                {"kind": "weekly_all", "group": "weekly", "percent": 29},
                {"group": "session", "percent": 5},
            ],
        })
        # Only the top-level Weekly row; unscoped limits[] entries are skipped.
        self.assertEqual(sum("Weekly" in r for r in rows), 1)


class CodexDetectionTests(unittest.TestCase):
    def setUp(self):
        core._USAGE_CACHE.clear()  # usage is TTL-cached; isolate each test

    def test_installed_via_auth_file_when_not_on_path(self):
        # Simulates a systemd user service whose PATH lacks the nvm bin dir:
        # `codex` is not resolvable but ~/.codex/auth.json exists.
        payload = {"email": "x@y.z", "plan_type": "plus", "rate_limit": {}}
        with patch("cli_usage_core.shutil.which", return_value=None), \
             patch("cli_usage_core.Path.exists", return_value=True), \
             patch("cli_usage_core.Path.read_text",
                   return_value=json.dumps({"tokens": {"access_token": "t"}})), \
             patch("cli_usage_core._http_json", return_value=payload):
            result = core.codex_data()
        self.assertTrue(result["installed"])
        self.assertFalse(any("not installed" in r[0] for r in result["rows"]))

    def test_not_installed_when_no_binary_and_no_auth(self):
        with patch("cli_usage_core.shutil.which", return_value=None), \
             patch("cli_usage_core.Path.exists", return_value=False):
            result = core.codex_data()
        self.assertFalse(result["installed"])
        self.assertEqual(result["summary"], {"5h": None, "weekly": None})

    def test_summary_maps_weekly_only_plan(self):
        # Plus plan: primary_window is the weekly window, no 5h.
        payload = {"email": "x@y.z", "plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 15, "limit_window_seconds": 604800},
        }}
        with patch("cli_usage_core.shutil.which", return_value=None), \
             patch("cli_usage_core.Path.exists", return_value=True), \
             patch("cli_usage_core.Path.read_text",
                   return_value=json.dumps({"tokens": {"access_token": "t"}})), \
             patch("cli_usage_core._http_json", return_value=payload):
            summary = core.codex_data()["summary"]
        self.assertEqual(summary, {"5h": None, "weekly": 85})


class CodexWindowLabelTests(unittest.TestCase):
    def test_weekly_window_by_duration(self):
        label, kind = core._codex_window_label({"limit_window_seconds": 604800})
        self.assertEqual((label, kind), ("Weekly limit", "week"))

    def test_five_hour_window_by_duration(self):
        label, kind = core._codex_window_label({"limit_window_seconds": 18000})
        self.assertEqual((label, kind), ("5h limit", "5h"))

    def test_missing_duration_uses_fallback(self):
        self.assertEqual(core._codex_window_label({}, "Weekly limit", "week"),
                         ("Weekly limit", "week"))


class ErrorRowTests(unittest.TestCase):
    def test_http_401_maps_to_relogin_row(self):
        error = urllib.error.HTTPError("url", 401, "unauthorized", {}, io.BytesIO())
        rows = core._usage_error_rows(error, "codex login")
        self.assertIn("re-login required", rows[0][0])
        self.assertIn("codex login", rows[0][0])

    def test_other_http_error_shows_status_code(self):
        error = urllib.error.HTTPError("url", 503, "down", {}, io.BytesIO())
        rows = core._usage_error_rows(error, "codex login")
        self.assertIn("HTTP 503", rows[0][0])

    def test_non_http_error_shows_type_name(self):
        rows = core._usage_error_rows(TimeoutError(), "codex login")
        self.assertIn("TimeoutError", rows[0][0])


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


if __name__ == "__main__":
    unittest.main()
