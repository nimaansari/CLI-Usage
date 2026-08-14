"""Shared, GUI-free data layer for the AI CLI tray indicator.

Works on Linux, macOS, and Windows. Used by both the GTK and pystray frontends.
"""

import json
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BAR_WIDTH   = 12
NET_TIMEOUT = 6
NET_RETRIES = 3
NET_BACKOFF = 0.6
# A rate-limited usage endpoint can return a Retry-After of thousands of
# seconds; sleeping that long would freeze the single refresh thread (and every
# provider it fetches in sequence). Cap the retry wait and let the next
# scheduled refresh try again instead.
NET_MAX_RETRY_DELAY = 15
# These endpoints are rate-limited, so fetch them at most once per this many
# seconds even though the tray refreshes more often (see _cached_usage).
USAGE_TTL = 300


def _bar(remaining_pct):
    if remaining_pct is None:
        return ""
    r = max(0.0, min(100.0, float(remaining_pct)))
    filled = round(r / 100 * BAR_WIDTH)
    return f"[{'█'*filled}{'░'*(BAR_WIDTH-filled)}] {int(round(r))}% left"


def _status_icon(remaining_pct):
    """Emoji color cue that works in most native tray menus.

    Native menu APIs do not consistently support arbitrary colored text, so we
    use portable colored icons in the label itself.
    """
    if remaining_pct is None:
        return "⚪"
    r = float(remaining_pct)
    if r < 10:
        return "🔴"
    if r < 30:
        return "🟡"
    return "🟢"


def _parse_when(when):
    if when in (None, "", 0):
        return None
    try:
        if isinstance(when, (int, float)):
            return datetime.fromtimestamp(float(when)).astimezone()
        return datetime.fromisoformat(str(when).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def _reset_str(when, kind):
    dt = _parse_when(when)
    if not dt:
        return ""
    if kind == "5h":
        return f"resets {dt.strftime('%H:%M')}"
    day = dt.strftime("%d %b").lstrip("0")
    return f"resets {dt.strftime('%H:%M')} on {day}"


def _kv(key, val, key_w=10):
    return f"  {key:<{key_w}} {val}"


def _limit_row(label, used_pct, reset_when, kind, label_w=14):
    remaining = None if used_pct is None else 100 - float(used_pct)
    bar = _bar(remaining)
    rs  = _reset_str(reset_when, kind)
    tail = f"  ({rs})" if rs else ""
    return f"  {_status_icon(remaining)} {label:<{label_w}} {bar}{tail}"


class ProviderResponseError(ValueError):
    """Raised when a provider returns JSON in an unexpected shape."""


def _as_dict(value, name):
    if not isinstance(value, dict):
        raise ProviderResponseError(f"{name} response was not an object")
    return value


def _as_optional_dict(value, name):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ProviderResponseError(f"{name} was not an object")
    return value


def _as_optional_list(value, name):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ProviderResponseError(f"{name} was not a list")
    return value


def _as_optional_number(value, name):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderResponseError(f"{name} was not numeric") from exc


def _http_json(url, headers, timeout=NET_TIMEOUT, retries=NET_RETRIES, backoff=NET_BACKOFF):
    req = urllib.request.Request(url, headers=headers)
    last_exc = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            should_retry = exc.code == 429 or 500 <= exc.code < 600
            try:
                exc.close()
            except Exception:
                pass
            if not should_retry or attempt == retries - 1:
                raise
            delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff * (2 ** attempt)
            # Don't block the refresh thread for minutes on a long Retry-After.
            if delay > NET_MAX_RETRY_DELAY:
                raise
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise last_exc


def _remaining(used_pct):
    """Remaining percent from a used percent, or None if unknown."""
    return None if used_pct is None else 100 - float(used_pct)


_USAGE_CACHE = {}  # provider name -> (monotonic_ts, validated_data)


def _cached_usage(name, fetch_fn, ttl=USAGE_TTL):
    """Fetch provider usage through a small TTL cache.

    Returns (data, age_seconds, served_on_error). The tray refreshes every ~60s
    but these endpoints are rate-limited, so hit the network at most once per
    `ttl`. If a refresh fails (e.g. HTTP 429) keep serving the last good payload
    instead of blanking the display; served_on_error flags that fallback.
    """
    now = time.monotonic()
    cached = _USAGE_CACHE.get(name)
    if cached is not None and (now - cached[0]) < ttl:
        return cached[1], now - cached[0], False
    try:
        data = fetch_fn()
    except Exception:
        if cached is not None:
            return cached[1], now - cached[0], True
        raise
    _USAGE_CACHE[name] = (now, data)
    return data, 0.0, False


def _fmt_age(seconds):
    """Compact age: 30s, 4m, 2h."""
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    m = s // 60
    if m < 90:
        return f"{m}m"
    return f"{m // 60}h"


def _codex_window_label(window, fallback="Limit", fallback_kind="week"):
    """Label + reset-kind for a Codex rate-limit window from its duration.

    Codex no longer guarantees primary_window is the 5h window and secondary
    the weekly one — on some plans primary_window IS the weekly window. Derive
    the label from limit_window_seconds instead of the slot position.
    """
    secs = window.get("limit_window_seconds")
    if not secs:
        return fallback, fallback_kind
    hours = secs / 3600
    if hours <= 6:
        return f"{int(round(hours))}h limit", "5h"
    days = secs / 86400
    if abs(days - 7) < 0.5:
        return "Weekly limit", "week"
    return f"{int(round(days))}d limit", "week"


def _usage_error_rows(exc, relogin_hint):
    """Menu rows for a failed usage fetch. 401 gets an explicit re-login hint."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return [(f"  ⚠ re-login required (run: {relogin_hint})", False, None)]
        return [(f"  usage unavailable (HTTP {exc.code})", False, None)]
    return [(f"  usage unavailable ({type(exc).__name__})", False, None)]


def validate_claude_usage(data):
    data = _as_dict(data, "Claude usage")
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        window = _as_optional_dict(data.get(key), key)
        _as_optional_number(window.get("utilization"), f"{key}.utilization")
    # Per-model barometers (Fable, etc.) now live in the limits[] array.
    for i, lim in enumerate(_as_optional_list(data.get("limits"), "limits")):
        lim = _as_dict(lim, f"limits[{i}]")
        _as_optional_number(lim.get("percent"), f"limits[{i}].percent")
    extra = _as_optional_dict(data.get("extra_usage"), "extra_usage")
    _as_optional_number(extra.get("utilization"), "extra_usage.utilization")
    return data


def validate_codex_usage(data):
    data = _as_dict(data, "Codex usage")
    rl = _as_optional_dict(data.get("rate_limit"), "rate_limit")
    for name in ("primary_window", "secondary_window"):
        window = _as_optional_dict(rl.get(name), f"rate_limit.{name}")
        _as_optional_number(window.get("used_percent"), f"rate_limit.{name}.used_percent")
    for i, extra in enumerate(_as_optional_list(data.get("additional_rate_limits"), "additional_rate_limits")):
        extra = _as_dict(extra, f"additional_rate_limits[{i}]")
        erl = _as_optional_dict(extra.get("rate_limit"), f"additional_rate_limits[{i}].rate_limit")
        for name in ("primary_window", "secondary_window"):
            window = _as_optional_dict(erl.get(name), f"additional_rate_limits[{i}].rate_limit.{name}")
            _as_optional_number(window.get("used_percent"), f"additional_rate_limits[{i}].rate_limit.{name}.used_percent")
    _as_optional_dict(data.get("credits"), "credits")
    return data


# ── Claude Code ──────────────────────────────────────────────────────────────

def claude_data():
    rows = []
    summary = {"5h": None, "weekly": None}
    if not shutil.which("claude"):
        return {"installed": False, "rows": [("  not installed", False, None)], "summary": summary}

    dot = Path.home() / ".claude.json"
    email, billing = "", ""
    if dot.exists():
        try:
            d = json.loads(dot.read_text())
            oa = d.get("oauthAccount", {})
            email   = oa.get("emailAddress", "")
            billing = oa.get("billingType", "")
        except Exception:
            pass

    creds = Path.home() / ".claude" / ".credentials.json"
    tok, sub_type, tier = None, "", ""
    if creds.exists():
        try:
            c = json.loads(creds.read_text())
            o = c.get("claudeAiOauth", {})
            tok      = o.get("accessToken")
            sub_type = o.get("subscriptionType", "")
            tier     = o.get("rateLimitTier", "")
        except Exception:
            pass

    plan = (sub_type or billing or "").replace("_", " ").title() or "logged in"
    account_line = email + (f" ({plan})" if plan else "")
    rows.append((_kv("Account", account_line), False, None))
    if tier:
        rows.append((_kv("Tier", tier.replace("_", " ")), False, None))

    if tok:
        try:
            u, age, stale = _cached_usage("claude", lambda: validate_claude_usage(_http_json(
                "https://api.anthropic.com/api/oauth/usage",
                {
                    "Authorization":     f"Bearer {tok}",
                    "anthropic-beta":    "oauth-2025-04-20",
                    "anthropic-version": "2023-06-01",
                    "User-Agent":        "claude-code/ai-tray",
                },
            )))
        except Exception as e:
            rows.extend(_usage_error_rows(e, "claude /login"))
            return {"installed": True, "rows": rows, "summary": summary}

        if stale:
            rows.append((f"  ⚪ usage {_fmt_age(age)} old — refresh failed", False, None))

        for label, key, kind, slot in [
            ("5h limit",     "five_hour", "5h",   "5h"),
            ("Weekly limit", "seven_day", "week", "weekly"),
        ]:
            w = u.get(key) or {}
            if w.get("utilization") is not None:
                summary[slot] = _remaining(w.get("utilization"))
                rows.append((_limit_row(label, w.get("utilization"),
                                        w.get("resets_at"), kind), False, None))

        # Per-model weekly barometers (Fable, Opus, Sonnet, …). Anthropic moved
        # these out of the dedicated seven_day_* fields into a generic limits[]
        # array keyed by scope.model.display_name, so this picks up new models
        # automatically.
        for lim in u.get("limits") or []:
            model = (lim.get("scope") or {}).get("model") or {}
            name  = model.get("display_name")
            if not name or lim.get("percent") is None:
                continue
            is_session = lim.get("group") == "session"
            label = f"{name} 5h" if is_session else f"Weekly {name}"
            rows.append((_limit_row(label, lim["percent"], lim.get("resets_at"),
                                    "5h" if is_session else "week"), False, None))

        eu = u.get("extra_usage") or {}
        if eu.get("is_enabled") and eu.get("utilization") is not None:
            rows.append((_limit_row("Extra usage", eu["utilization"], None, "week"), False, None))

    return {"installed": True, "rows": rows, "summary": summary}


# ── Codex CLI ────────────────────────────────────────────────────────────────

def codex_data():
    rows = []
    summary = {"5h": None, "weekly": None}
    auth_file = Path.home() / ".codex" / "auth.json"
    # `codex` is often installed via nvm, whose bin dir is absent from a
    # systemd user service's PATH — so shutil.which() alone falsely reports
    # "not installed". The presence of ~/.codex/auth.json is an equally valid
    # signal (and it's what the usage fetch actually reads), so accept either.
    if not shutil.which("codex") and not auth_file.exists():
        return {"installed": False, "rows": [("  not installed", False, None)], "summary": summary}

    tok = None
    if auth_file.exists():
        try:
            a = json.loads(auth_file.read_text())
            t = a.get("tokens") or {}
            tok = t.get("access_token")
        except Exception:
            pass

    if not tok:
        rows.append(("  no auth token", False, None))
        return {"installed": True, "rows": rows, "summary": summary}

    try:
        u, age, stale = _cached_usage("codex", lambda: validate_codex_usage(_http_json(
            "https://chatgpt.com/backend-api/codex/usage",
            {
                "Authorization": f"Bearer {tok}",
                "User-Agent": "codex_cli_rs/ai-tray",
                "originator": "codex_cli_rs",
            },
        )))
    except Exception as e:
        rows.extend(_usage_error_rows(e, "codex login"))
        return {"installed": True, "rows": rows, "summary": summary}

    email = u.get("email", "")
    plan  = (u.get("plan_type") or "").title()
    rows.append((_kv("Account", email + (f" ({plan})" if plan else "")), False, None))
    if stale:
        rows.append((f"  ⚪ usage {_fmt_age(age)} old — refresh failed", False, None))

    rl = u.get("rate_limit") or {}
    for slot, fallback in (("primary_window", ("5h limit", "5h")),
                           ("secondary_window", ("Weekly limit", "week"))):
        w = rl.get(slot) or {}
        if w and w.get("used_percent") is not None:
            label, kind = _codex_window_label(w, *fallback)
            summary["5h" if kind == "5h" else "weekly"] = _remaining(w.get("used_percent"))
            rows.append((_limit_row(label, w.get("used_percent"),
                                    w.get("reset_at"), kind), False, None))

    for extra in (u.get("additional_rate_limits") or []):
        name = extra.get("limit_name") or extra.get("metered_feature") or "Extra"
        erl  = extra.get("rate_limit") or {}
        epw  = erl.get("primary_window") or {}
        esw  = erl.get("secondary_window") or {}
        rows.append((f"  {name} limit:", False, None))
        if epw:
            rows.append((_limit_row("  5h", epw.get("used_percent"),
                                    epw.get("reset_at"), "5h"), False, None))
        if esw:
            rows.append((_limit_row("  Weekly", esw.get("used_percent"),
                                    esw.get("reset_at"), "week"), False, None))

    cr = u.get("credits") or {}
    if cr.get("has_credits") or cr.get("unlimited"):
        bal = cr.get("balance", "")
        rows.append((_kv("Credits", "unlimited" if cr.get("unlimited") else f"${bal}"), False, None))

    return {"installed": True, "rows": rows, "summary": summary}


def fetch_all():
    return {
        "Claude Code": claude_data(),
        "Codex CLI":   codex_data(),
    }


def worst_remaining_pct(data):
    """Lowest 'N% left' value across Claude+Codex. Used for the tray label."""
    worst = None
    for tool in ("Claude Code", "Codex CLI"):
        for text, *_ in data.get(tool, {}).get("rows", []):
            if "% left" in text:
                try:
                    pct = int(text.split("% left")[0].split()[-1])
                    worst = pct if worst is None else min(worst, pct)
                except Exception:
                    pass
    return worst
