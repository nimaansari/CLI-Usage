"""Shared, GUI-free data layer for the AI CLI tray indicator.

Works on Linux, macOS, and Windows. Used by both the GTK and pystray frontends.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BAR_WIDTH   = 10
NET_TIMEOUT = 6
NET_RETRIES = 3
NET_BACKOFF = 0.6
# A rate-limited endpoint can answer with a Retry-After of thousands of seconds.
# Sleeping that long would freeze the refresh thread, so give up and let the
# next scheduled refresh try again.
NET_MAX_RETRY_DELAY = 15
# The usage endpoints are rate-limited; the tray refreshes every minute but only
# hits the network this often per provider (see _cached_usage).
USAGE_TTL = 180

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"


def app_dir():
    """Per-user folder for the lock file, rendered icons and logs."""
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif IS_MAC:
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "cli-usage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def claude_config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _extra_bin_dirs():
    """Places CLIs get installed that a login/autostart PATH often misses."""
    h = Path.home()
    dirs = [h / ".local" / "bin", h / ".claude" / "local", h / ".npm-global" / "bin",
            h / ".bun" / "bin", h / ".volta" / "bin", h / "bin",
            Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/snap/bin")]
    if IS_WIN:
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(Path(appdata) / "npm")
        dirs.append(h / "scoop" / "shims")
    nvm = Path(os.environ.get("NVM_DIR") or h / ".nvm") / "versions" / "node"
    if nvm.is_dir():
        dirs += sorted(nvm.glob("*/bin"), reverse=True)
    return [d for d in dirs if d.is_dir()]


def find_cli(name):
    """shutil.which, plus the usual npm/nvm/brew/local bin folders."""
    found = shutil.which(name)
    if found:
        return found
    extra = os.pathsep.join(str(d) for d in _extra_bin_dirs())
    return shutil.which(name, path=extra) if extra else None


# ── single instance ──────────────────────────────────────────────────────────

_lock_handle = None


def _lock_path():
    return app_dir() / "instance.lock"


def _try_lock(fh):
    try:
        if IS_WIN:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh):
    try:
        if IS_WIN:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def acquire_single_instance():
    """Return True if this is the only running tray; keep the lock for the process lifetime."""
    global _lock_handle
    fh = open(_lock_path(), "a+")
    if not _try_lock(fh):
        fh.close()
        return False
    fh.seek(0)
    fh.truncate()
    # Windows locks byte 0 (other processes can't read it), so the PID goes after it.
    fh.write(" " + str(os.getpid()))
    fh.flush()
    _lock_handle = fh
    return True


def under_service_manager():
    """True when systemd (Linux) started us; it restarts the tray if it stops."""
    # Set only by our unit file; INVOCATION_ID leaks into terminals, so it can't be trusted.
    return os.environ.get("CLI_USAGE_SERVICE") == "1" and sys.platform.startswith("linux")


def sd_notify(msg):
    """Tell systemd we are alive (READY=1 / WATCHDOG=1). No-op outside a notify service."""
    import socket
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not under_service_manager():
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False


def claim_single_instance():
    """Get the instance lock. Under systemd, wait for an older copy to exit
    instead of exiting (which would make systemd restart us in a loop)."""
    if acquire_single_instance():
        return True
    if not under_service_manager():
        return False
    print("another copy is running; waiting for it to exit", flush=True)
    sd_notify("READY=1")
    while not acquire_single_instance():
        sd_notify("WATCHDOG=1")
        time.sleep(5)
    return True


def request_quit():
    """Called from the Quit menu item. Under systemd, stop the service so it
    is not restarted; returns after asking."""
    if under_service_manager() and shutil.which("systemctl"):
        try:
            subprocess.Popen(["systemctl", "--user", "stop", "--no-block", "cli-usage.service"])
        except OSError:
            pass


def running_instance_pid():
    """PID of a running tray, or None."""
    path = _lock_path()
    if not path.exists():
        return None
    try:
        with open(path, "r+") as fh:
            if _try_lock(fh):
                _unlock(fh)
                return None   # nobody holds it
            fh.seek(1 if IS_WIN else 0)   # byte 0 is the locked byte on Windows
            txt = fh.read().strip()
    except OSError:
        return None
    return int(txt) if txt.isdigit() else None


def stop_running_instance(timeout=5.0):
    """Terminate a running tray (used by the installer before relaunching)."""
    import signal
    pid = running_instance_pid()
    if not pid or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and running_instance_pid() == pid:
        time.sleep(0.2)
    return True


# ── formatting ───────────────────────────────────────────────────────────────


def _bar(remaining_pct):
    if remaining_pct is None:
        return ""
    r = max(0.0, min(100.0, float(remaining_pct)))
    filled = round(r / 100 * BAR_WIDTH)
    return f"{'█'*filled}{'░'*(BAR_WIDTH-filled)}  {int(round(r))}% left"


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


def _reset_str(when, kind, now=None):
    """'resets in 2h 14m' when it is close, otherwise 'resets Thu 21:00'."""
    dt = _parse_when(when)
    if not dt:
        return ""
    now = now or datetime.now().astimezone()
    secs = (dt - now).total_seconds()
    if secs <= 0:
        return "resetting now"
    if secs < 24 * 3600:
        h, m = int(secs // 3600), int(secs % 3600 // 60)
        if h == 0:
            return f"resets in {max(m, 1)}m"
        return f"resets in {h}h {m:02d}m"
    return f"resets {dt.strftime('%a')} {dt.strftime('%d %b').lstrip('0')}, {dt.strftime('%H:%M')}"


def _kv(key, val, key_w=10):
    return f"  {key:<{key_w}} {val}"


def _limit_row(label, used_pct, reset_when, kind, label_w=13):
    remaining = None if used_pct is None else 100 - float(used_pct)
    bar = _bar(remaining)
    rs  = _reset_str(reset_when, kind)
    tail = f"  ·  {rs}" if rs else ""
    return f"  {_status_icon(remaining)}  {label:<{label_w}} {bar}{tail}"


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
            if not should_retry or attempt == retries - 1:
                raise
            delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff * (2 ** attempt)
            if delay > NET_MAX_RETRY_DELAY:
                raise
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise last_exc


_USAGE_CACHE = {}  # provider -> (monotonic time, token, payload)
_BLOCKED_UNTIL = {}  # provider -> (monotonic deadline, wall-clock deadline)


class RateLimited(Exception):
    """The provider asked us to wait; ``until`` is a wall-clock timestamp."""

    def __init__(self, until):
        super().__init__("rate limited")
        self.until = until


def _retry_after_seconds(exc):
    value = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _cached_usage(name, token, fetch_fn, ttl=USAGE_TTL):
    """Fetch through a small TTL cache; on failure serve the last good payload.

    Returns (payload, age_seconds, stale). `stale` is True when a refresh failed
    and an older payload is being shown instead of blanking the menu.
    """
    now = time.monotonic()
    cached = _USAGE_CACHE.get(name)
    if cached and cached[1] == token and now - cached[0] < ttl:
        return cached[2], now - cached[0], False
    blocked = _BLOCKED_UNTIL.get(name)
    if blocked and now < blocked[0]:
        if cached and cached[1] == token:
            return cached[2], now - cached[0], True
        raise RateLimited(blocked[1])
    try:
        data = fetch_fn()
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            wait = _retry_after_seconds(exc)
            wait = 300.0 if wait is None else min(wait, 3600.0)
            _BLOCKED_UNTIL[name] = (now + wait, time.time() + wait)
            if not (cached and cached[1] == token):
                raise RateLimited(_BLOCKED_UNTIL[name][1]) from exc
        if cached and cached[1] == token:
            return cached[2], now - cached[0], True
        raise
    _BLOCKED_UNTIL.pop(name, None)
    _USAGE_CACHE[name] = (now, token, data)
    return data, 0.0, False


def _fmt_age(seconds):
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def _usage_error_rows(exc, relogin_cmd):
    """Readable menu rows for a failed usage fetch."""
    if isinstance(exc, RateLimited):
        at = datetime.fromtimestamp(exc.until).strftime("%H:%M")
        return [(f"  ⏳  rate-limited by the provider — next try at {at}", False, None)]
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return [(f"  ⚠  login expired — run `{relogin_cmd}`", False, None)]
        if exc.code == 429:
            return [("  ⏳  rate-limited by the provider — retrying soon", False, None)]
        return [(f"  usage unavailable (HTTP {exc.code})", False, None)]
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return [("  usage unavailable (offline?)", False, None)]
    return [(f"  usage unavailable ({type(exc).__name__})", False, None)]


def _stale_row(age):
    return (f"  ⚪  showing data from {_fmt_age(age)} ago — refresh failed", False, None)


def validate_claude_usage(data):
    data = _as_dict(data, "Claude usage")
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        window = _as_optional_dict(data.get(key), key)
        _as_optional_number(window.get("utilization"), f"{key}.utilization")
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

def _claude_oauth(creds):
    """Return Claude Code's ``claudeAiOauth`` login dict, or None.

    ``.credentials.json`` can exist without a Claude login: on macOS Claude Code
    keeps the login in the Keychain but may still write MCP OAuth tokens to that
    file (e.g. when run over SSH). Only trust the file if it actually holds
    ``claudeAiOauth``; otherwise fall back to the Keychain on macOS.
    """
    def parse(raw):
        try:
            return json.loads(raw).get("claudeAiOauth") or None
        except Exception:
            return None

    found = None
    if creds.exists():
        try:
            found = parse(creds.read_text())
        except Exception:
            found = None
        if found and found.get("accessToken"):
            return found
    if IS_MAC:
        try:
            p = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if p.returncode == 0:
                found = parse(p.stdout.strip()) or found
        except Exception:
            pass
    # May have plan/tier but an empty token (e.g. login via CLAUDE_CODE_OAUTH_TOKEN).
    return found


_SHELL_TOKEN = {"at": None, "value": None}


def _claude_env_token():
    """Token from CLAUDE_CODE_OAUTH_TOKEN (e.g. made with `claude setup-token`).

    A menu-bar app started by launchd/systemd does not inherit the user's shell
    environment, so if the variable isn't set here, ask the user's shell once
    (cached for 10 minutes).
    """
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok or IS_WIN:
        return tok
    now = time.monotonic()
    if _SHELL_TOKEN["at"] is not None and now - _SHELL_TOKEN["at"] < 600:
        return _SHELL_TOKEN["value"]
    shell = os.environ.get("SHELL") or ("/bin/zsh" if IS_MAC else "/bin/bash")
    value = None
    try:
        p = subprocess.run([shell, "-lc", 'printf %s "$CLAUDE_CODE_OAUTH_TOKEN"'],
                           capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
        value = p.stdout.strip() or None
    except Exception:
        pass
    _SHELL_TOKEN.update(at=now, value=value)
    return value


def claude_data():
    rows = []
    cfg = claude_config_dir()
    creds = cfg / ".credentials.json"
    if not find_cli("claude") and not creds.exists():
        return {"installed": False, "rows": [("  not installed", False, None)]}

    # With CLAUDE_CONFIG_DIR set, .claude.json lives inside it instead of $HOME.
    dot = (cfg if os.environ.get("CLAUDE_CONFIG_DIR") else Path.home()) / ".claude.json"
    email, billing = "", ""
    if dot.exists():
        try:
            d = json.loads(dot.read_text())
            oa = d.get("oauthAccount", {})
            email   = oa.get("emailAddress", "")
            billing = oa.get("billingType", "")
        except Exception:
            pass

    tok, sub_type, tier = None, "", ""
    o = _claude_oauth(creds)
    if o:
        tok      = o.get("accessToken") or None
        sub_type = o.get("subscriptionType", "")
        tier     = o.get("rateLimitTier", "")
    tok = tok or _claude_env_token()

    plan = (sub_type or billing or "").replace("_", " ").title() or "logged in"
    account_line = email + (f" ({plan})" if plan else "")
    rows.append((_kv("Account", account_line), False, None))
    if tier:
        rows.append((_kv("Tier", tier.replace("_", " ")), False, None))

    if not tok:
        rows.append(("  not logged in — run `claude` and /login", False, None))
        return {"installed": True, "rows": rows}

    try:
        u, age, stale = _cached_usage("claude", tok, lambda: validate_claude_usage(_http_json(
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
        return {"installed": True, "rows": rows}
    if stale:
        rows.append(_stale_row(age))

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

    # Newer model-scoped weekly limits (e.g. "Weekly Fable") only appear in
    # the generic limits[] array, keyed by scope.model.display_name.
    shown = {r[0] for r in rows}
    for lim in u.get("limits") or []:
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        if not model or lim.get("percent") is None:
            continue
        session = lim.get("group") == "session"
        label = f"5h {model}" if session else f"Weekly {model}"
        row = _limit_row(label, lim["percent"], lim.get("resets_at"), "5h" if session else "week")
        if not any(label in s for s in shown):
            rows.append((row, False, None))

    eu = u.get("extra_usage") or {}
    if eu.get("is_enabled") and eu.get("utilization") is not None:
        rows.append((_limit_row("Extra usage", eu["utilization"], None, "week"), False, None))

    return {"installed": True, "rows": rows}


# ── Codex CLI ────────────────────────────────────────────────────────────────

def codex_data():
    rows = []
    auth_file = codex_home() / "auth.json"
    # Accept the auth file as proof of install: autostart PATHs often miss nvm/npm bins.
    if not find_cli("codex") and not auth_file.exists():
        return {"installed": False, "rows": [("  not installed", False, None)]}

    tok = None
    if auth_file.exists():
        try:
            a = json.loads(auth_file.read_text())
            t = a.get("tokens") or {}
            tok = t.get("access_token")
        except Exception:
            pass

    if not tok:
        rows.append(("  not logged in — run `codex login`", False, None))
        return {"installed": True, "rows": rows}

    try:
        u, age, stale = _cached_usage("codex", tok, lambda: validate_codex_usage(_http_json(
            # /codex/usage now returns a Cloudflare 403; Codex CLI uses /wham/usage.
            "https://chatgpt.com/backend-api/wham/usage",
            {
                "Authorization": f"Bearer {tok}",
                "User-Agent": "codex_cli_rs/ai-tray",
                "originator": "codex_cli_rs",
            },
        )))
    except Exception as e:
        rows.extend(_usage_error_rows(e, "codex login"))
        return {"installed": True, "rows": rows}

    email = u.get("email", "")
    plan  = (u.get("plan_type") or "").title()
    rows.append((_kv("Account", email + (f" ({plan})" if plan else "")), False, None))
    if stale:
        rows.append(_stale_row(age))

    rl = u.get("rate_limit") or {}
    pw = rl.get("primary_window") or {}
    sw = rl.get("secondary_window") or {}
    if pw:
        label, kind = _codex_window(pw, "5h")
        rows.append((_limit_row(f"{label} limit", pw.get("used_percent"),
                                pw.get("reset_at"), kind), False, None))
    if sw:
        label, kind = _codex_window(sw, "week")
        rows.append((_limit_row(f"{label} limit", sw.get("used_percent"),
                                sw.get("reset_at"), kind), False, None))

    additional = u.get("additional_rate_limits") or []
    for extra in additional:
        name = extra.get("limit_name") or extra.get("metered_feature") or "Extra"
        erl  = extra.get("rate_limit") or {}
        epw  = erl.get("primary_window") or {}
        esw  = erl.get("secondary_window") or {}
        rows.append((f"  {name} limit:", False, None))
        if epw:
            label, kind = _codex_window(epw, "5h")
            rows.append((_limit_row(f"  {label}", epw.get("used_percent"),
                                    epw.get("reset_at"), kind), False, None))
        if esw:
            label, kind = _codex_window(esw, "week")
            rows.append((_limit_row(f"  {label}", esw.get("used_percent"),
                                    esw.get("reset_at"), kind), False, None))

    cr = u.get("credits") or {}
    if cr.get("has_credits") or cr.get("unlimited"):
        bal = cr.get("balance", "")
        rows.append((_kv("Credits", "unlimited" if cr.get("unlimited") else f"${bal}"), False, None))

    # Some plan tiers (e.g. "prolite") return all rate-limit fields as null.
    # Without this branch the menu would just show Account/Plan and look broken.
    # Surface anything else the API gave us so the user knows it's the plan, not the tray.
    has_any_limit = bool(pw or sw or additional or cr.get("has_credits") or cr.get("unlimited"))
    if not has_any_limit:
        spend         = u.get("spend_control") or {}
        reset_credits = u.get("rate_limit_reset_credits") or {}
        spend_cap     = spend.get("individual_limit")
        reset_avail   = reset_credits.get("available_count")
        if spend.get("reached"):
            rows.append(("  🔴 spend limit reached", False, None))
        if spend_cap:
            rows.append((_kv("Spend cap", f"${spend_cap}"), False, None))
        if reset_avail:
            rows.append((_kv("Reset credits", str(reset_avail)), False, None))

    rows.extend(_codex_local_token_rows())

    if not has_any_limit and len([r for r in rows if "showing data from" not in r[0]]) == 1:
        # Account row only — nothing else surfaced
        plan_label = plan or "unknown"
        rows.append((f"  no usage data available (plan: {plan_label})", False, None))

    return {"installed": True, "rows": rows}


def _codex_window(window, default_kind):
    """Label a Codex window by its real length (plans differ: 5h vs weekly primary)."""
    secs = window.get("limit_window_seconds")
    if not isinstance(secs, (int, float)):
        return ("5h" if default_kind == "5h" else "Weekly"), default_kind
    if secs >= 86400:
        return ("Weekly" if secs >= 6 * 86400 else f"{round(secs / 86400)}d"), "week"
    return f"{round(secs / 3600)}h", "5h"


_CODEX_FILE_CACHE = {}  # path -> ((mtime, size), {local date: tokens})


def _codex_file_tokens(path):
    """Tokens per local day for one Codex session log, streamed line by line.

    Each token_count event carries a running total for the session, so the
    usage of an event is the increase over the previous total (repeated
    events add nothing). Results are cached until the file changes.
    """
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    cached = _CODEX_FILE_CACHE.get(path)
    if cached and cached[0] == key:
        return cached[1]
    per_day = {}
    prev_total = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"token_count"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                info = ((obj.get("payload") or {}).get("info") or {})
                # Count input + output (cached input is part of input), the same
                # definition ccusage and the Codex CLI use. total_tokens is larger.
                tot = info.get("total_token_usage")
                if isinstance(tot, dict):
                    total = int(tot.get("input_tokens") or 0) + int(tot.get("output_tokens") or 0)
                    used = max(0, total - prev_total)
                    prev_total = max(prev_total, total)
                else:
                    last = info.get("last_token_usage") or {}
                    used = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
                if not used:
                    continue
                ts = _parse_when(obj.get("timestamp"))
                if ts is None:
                    continue
                day = ts.date()
                per_day[day] = per_day.get(day, 0) + used
    except OSError:
        return {}
    _CODEX_FILE_CACHE[path] = (key, per_day)
    return per_day


def _codex_local_token_rows():
    """Token totals for today and this month from local Codex session logs.

    Replaces shelling out to ccusage-codex, which re-parsed every log on each
    refresh and peaked at 2+ GB of RAM. Only files touched this month are read,
    and unchanged files come from the cache.
    """
    base = codex_home() / "sessions"
    if not base.is_dir():
        return []
    now = datetime.now().astimezone()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    today, month = now.date(), (now.year, now.month)
    today_tok = month_tok = 0
    seen = set()
    for path in base.glob("*/*/*/*.jsonl"):
        try:
            if path.stat().st_mtime < month_start:
                continue
        except OSError:
            continue
        seen.add(path)
        for day, tok in _codex_file_tokens(path).items():
            if (day.year, day.month) == month:
                month_tok += tok
                if day == today:
                    today_tok += tok
    for stale in set(_CODEX_FILE_CACHE) - seen:
        del _CODEX_FILE_CACHE[stale]
    if not month_tok:
        return []
    return [(_kv("Today", f"{_fmt_n(today_tok)} tok"), False, None),
            (_kv("Month", f"{_fmt_n(month_tok)} tok"), False, None)]


def _fmt_n(n):
    """Compact numeric format: 1234 → 1.2K, 12345678 → 12.3M."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    for unit, div in (("M", 1_000_000), ("K", 1_000)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return str(n)


# ── Gemini CLI ───────────────────────────────────────────────────────────────

def gemini_data():
    rows = []
    if not find_cli("gemini"):
        return {"installed": False, "rows": [("  not installed", False, None)]}

    # Gemini has no public usage endpoint, so we surface the richest local state we can:
    # account email, auth type, and OAuth token expiry. Beats a bare "usage unavailable".
    env_key  = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    auth_kind = None
    email     = None
    expiry    = None

    if env_key:
        auth_kind = "API key (env)"
    else:
        gem_dir   = Path.home() / ".gemini"
        oauth     = gem_dir / "oauth_creds.json"
        accounts  = gem_dir / "google_accounts.json"
        if oauth.exists():
            auth_kind = "OAuth (gemini-cli)"
            try:
                d  = json.loads(oauth.read_text())
                ms = d.get("expiry_date")
                if ms:
                    expiry = datetime.fromtimestamp(ms / 1000).astimezone()
            except Exception:
                pass
            if accounts.exists():
                try:
                    email = json.loads(accounts.read_text()).get("active")
                except Exception:
                    pass
        else:
            for p in [
                Path.home() / ".config" / "gemini" / "credentials.json",
                Path.home() / ".config" / "gcloud" / "application_default_credentials.json",
                Path.home() / "AppData" / "Roaming" / "gcloud" / "application_default_credentials.json",
                Path.home() / "Library" / "Application Support" / "gcloud" / "application_default_credentials.json",
            ]:
                if p.exists():
                    auth_kind = p.name
                    break

    if not auth_kind:
        rows.append(("  no credentials found", False, None))
        return {"installed": True, "rows": rows}

    if email:
        rows.append((_kv("Account", email), False, None))
    rows.append((_kv("Auth", auth_kind), False, None))
    if expiry:
        now = datetime.now().astimezone()
        if expiry < now:
            rows.append((_kv("Token", "expired (auto-refreshes on next call)"), False, None))
        else:
            rows.append((_kv("Token", f"expires {expiry.strftime('%H:%M')} on {expiry.strftime('%d %b').lstrip('0')}"), False, None))
    # No public usage endpoint for the OAuth-personal flow Google ships with the
    # Gemini CLI; the only way to see quotas is the API-key flow with a Google
    # Cloud project. Tell the user that explicitly so the section doesn't look broken.
    if auth_kind != "API key (env)":
        rows.append(("  no quota % — Google doesn't expose it for OAuth-personal", False, None))
    rows.extend(_gemini_local_token_rows())
    return {"installed": True, "rows": rows}


def _gemini_local_token_rows():
    """Token totals reconstructed from local Gemini CLI session JSONLs.

    Gemini CLI logs per-response `tokens` blocks to two layouts under ~/.gemini/tmp/*/chats/:
      - flat:   session-*.jsonl
      - nested: <session-uuid>/<message-uuid>.jsonl   (subagent / newer sessions)
    Both must be scanned. Timestamps inside are UTC (Z-suffixed), so we convert to local
    time before bucketing into Today / This Month, otherwise late-evening usage hides in
    tomorrow's UTC date.
    """
    rows = []
    base = Path.home() / ".gemini" / "tmp"
    if not base.exists():
        return rows

    now_local = datetime.now().astimezone()
    today_local = now_local.date()
    month_local = (today_local.year, today_local.month)

    last_ts = None
    today_in = today_out = today_total = 0
    month_in = month_out = month_total = 0
    all_in = all_out = all_total = 0

    files = list(base.glob("*/chats/session-*.jsonl")) + list(base.glob("*/chats/*/*.jsonl"))
    for f in files:
        try:
            with f.open() as fh:
                for line in fh:
                    if '"type":"gemini"' not in line or '"tokens"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tok = obj.get("tokens") or {}
                    inp = int(tok.get("input")  or 0)
                    out = int(tok.get("output") or 0)
                    tot = int(tok.get("total")  or (inp + out))
                    all_in += inp; all_out += out; all_total += tot

                    ts_local = _parse_when(obj.get("timestamp"))
                    if ts_local is None:
                        continue
                    if last_ts is None or ts_local > last_ts:
                        last_ts = ts_local
                    if (ts_local.year, ts_local.month) == month_local:
                        month_in += inp; month_out += out; month_total += tot
                        if ts_local.date() == today_local:
                            today_in += inp; today_out += out; today_total += tot
        except OSError:
            continue

    if all_total == 0:
        return rows
    rows.append((_kv("Today",    f"{_fmt_n(today_total)} tok  (in {_fmt_n(today_in)} / out {_fmt_n(today_out)})"), False, None))
    rows.append((_kv("Month",    f"{_fmt_n(month_total)} tok  (in {_fmt_n(month_in)} / out {_fmt_n(month_out)})"), False, None))
    rows.append((_kv("All-time", f"{_fmt_n(all_total)} tok  (in {_fmt_n(all_in)} / out {_fmt_n(all_out)})"), False, None))
    if last_ts:
        rows.append((_kv("Last use", f"{last_ts.strftime('%H:%M')} on {last_ts.strftime('%d %b').lstrip('0')}"), False, None))
    return rows


# ── terminal launch ──────────────────────────────────────────────────────────

LINUX_TERMINALS = (
    # (binary, args that run a command)
    ("x-terminal-emulator", ["-e"]),
    ("ptyxis",          ["--"]),
    ("gnome-terminal",  ["--"]),
    ("kgx",             ["--"]),
    ("konsole",         ["-e"]),
    ("xfce4-terminal",  ["-x"]),
    ("tilix",           ["-e"]),
    ("alacritty",       ["-e"]),
    ("kitty",           []),
    ("wezterm",         ["start", "--"]),
    ("foot",            []),
    ("xterm",           ["-e"]),
)


def _terminal_argv_linux(cmd):
    shell_cmd = ["bash", "-lc", f"{cmd}; exec bash"]
    preferred = os.environ.get("TERMINAL")
    if preferred and shutil.which(preferred):
        return [preferred, "-e"] + shell_cmd
    for term, args in LINUX_TERMINALS:
        if shutil.which(term):
            return [term] + args + shell_cmd
    return None


def open_terminal(cmd):
    """Open a new terminal window running `cmd` and leave it open afterwards."""
    if IS_MAC:
        escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript",
                          "-e", f'tell application "Terminal" to do script "{escaped}"',
                          "-e", 'tell application "Terminal" to activate'])
        return True
    if IS_WIN:
        # The tray runs under pythonw.exe, which has no console, so the shell
        # needs a brand-new one. -NoExit keeps the window open like cmd /k.
        shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        try:
            if shutil.which("wt"):
                subprocess.Popen(["wt", "nt", shell, "-NoExit", "-Command", cmd])
            else:
                subprocess.Popen([shell, "-NoExit", "-Command", cmd],
                                 creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError:
            subprocess.Popen(["cmd", "/c", "start", "", "cmd", "/k", cmd],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    argv = _terminal_argv_linux(cmd)
    if not argv:
        print("open_terminal: no terminal emulator found", flush=True)
        return False
    subprocess.Popen(argv, start_new_session=True)
    return True


def fetch_all():
    return {
        "Claude Code": claude_data(),
        "Codex CLI":   codex_data(),
        "Gemini CLI":  gemini_data(),
    }


def _row_pct(text):
    if "% left" not in text:
        return None
    try:
        return int(text.split("% left")[0].split()[-1])
    except (ValueError, IndexError):
        return None


def provider_summary(info):
    """Short headline for a provider, e.g. '  —  81% left' or '  —  not installed'."""
    if not info:
        return ""
    if not info.get("installed"):
        return "  —  not installed"
    pcts = [p for p in (_row_pct(t) for t, *_ in info.get("rows", [])) if p is not None]
    if pcts:
        return f"  —  {min(pcts)}% left"
    for text, *_ in info.get("rows", []):
        t = text.strip()
        if t.startswith("⚠") or "not logged in" in t or "unavailable" in t:
            return "  —  needs attention"
    return ""


def worst_remaining_pct(data):
    """Lowest 'N% left' value across Claude+Codex. Used for the tray label."""
    worst = None
    for tool in ("Claude Code", "Codex CLI"):
        for text, *_ in data.get(tool, {}).get("rows", []):
            pct = _row_pct(text)
            if pct is not None:
                worst = pct if worst is None else min(worst, pct)
    return worst
