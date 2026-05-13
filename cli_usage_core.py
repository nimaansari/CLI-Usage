"""Shared, GUI-free data layer for the AI CLI tray indicator.

Works on Linux, macOS, and Windows. Used by both the GTK and pystray frontends.
"""

import json
import os
import shutil
import urllib.request
from datetime import datetime
from pathlib import Path

BAR_WIDTH   = 12
NET_TIMEOUT = 6


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


def _http_json(url, headers, timeout=NET_TIMEOUT):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── Claude Code ──────────────────────────────────────────────────────────────

def claude_data():
    rows = []
    if not shutil.which("claude"):
        return {"installed": False, "rows": [("  not installed", False, None)]}

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
            u = _http_json(
                "https://api.anthropic.com/api/oauth/usage",
                {
                    "Authorization":     f"Bearer {tok}",
                    "anthropic-beta":    "oauth-2025-04-20",
                    "anthropic-version": "2023-06-01",
                    "User-Agent":        "claude-code/ai-tray",
                },
            )
        except Exception as e:
            rows.append((f"  usage unavailable ({type(e).__name__})", False, None))
            return {"installed": True, "rows": rows}

        for label, key, kind in [
            ("5h limit",      "five_hour",        "5h"),
            ("Weekly limit",  "seven_day",        "week"),
            ("Weekly Opus",   "seven_day_opus",   "week"),
            ("Weekly Sonnet", "seven_day_sonnet", "week"),
        ]:
            w = u.get(key) or {}
            if w.get("utilization") is not None:
                rows.append((_limit_row(label, w.get("utilization"),
                                        w.get("resets_at"), kind), False, None))

        eu = u.get("extra_usage") or {}
        if eu.get("is_enabled") and eu.get("utilization") is not None:
            rows.append((_limit_row("Extra usage", eu["utilization"], None, "week"), False, None))

    return {"installed": True, "rows": rows}


# ── Codex CLI ────────────────────────────────────────────────────────────────

def codex_data():
    rows = []
    if not shutil.which("codex"):
        return {"installed": False, "rows": [("  not installed", False, None)]}

    auth_file = Path.home() / ".codex" / "auth.json"
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
        return {"installed": True, "rows": rows}

    try:
        u = _http_json(
            "https://chatgpt.com/backend-api/codex/usage",
            {"Authorization": f"Bearer {tok}", "User-Agent": "codex_cli_rs/ai-tray"},
        )
    except Exception as e:
        rows.append((f"  usage unavailable ({type(e).__name__})", False, None))
        return {"installed": True, "rows": rows}

    email = u.get("email", "")
    plan  = (u.get("plan_type") or "").title()
    rows.append((_kv("Account", email + (f" ({plan})" if plan else "")), False, None))

    rl = u.get("rate_limit") or {}
    pw = rl.get("primary_window") or {}
    sw = rl.get("secondary_window") or {}
    if pw:
        rows.append((_limit_row("5h limit", pw.get("used_percent"),
                                pw.get("reset_at"), "5h"), False, None))
    if sw:
        rows.append((_limit_row("Weekly limit", sw.get("used_percent"),
                                sw.get("reset_at"), "week"), False, None))

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

    return {"installed": True, "rows": rows}


# ── Gemini CLI ───────────────────────────────────────────────────────────────

def gemini_data():
    rows = []
    if not shutil.which("gemini"):
        return {"installed": False, "rows": [("  not installed", False, None)]}

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    src = None
    if key:
        src = "API key (env)"
    else:
        for p in [
            Path.home() / ".gemini" / "oauth_creds.json",
            Path.home() / ".config" / "gemini" / "credentials.json",
            Path.home() / ".config" / "gcloud" / "application_default_credentials.json",
            Path.home() / "AppData" / "Roaming" / "gcloud" / "application_default_credentials.json",
            Path.home() / "Library" / "Application Support" / "gcloud" / "application_default_credentials.json",
        ]:
            if p.exists():
                src = p.name
                break

    if not src:
        rows.append(("  no credentials found", False, None))
        return {"installed": True, "rows": rows}

    rows.append((_kv("Auth", src), False, None))
    rows.append(("  usage unavailable (no public endpoint)", False, None))
    return {"installed": True, "rows": rows}


def fetch_all():
    return {
        "Claude Code": claude_data(),
        "Codex CLI":   codex_data(),
        "Gemini CLI":  gemini_data(),
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
