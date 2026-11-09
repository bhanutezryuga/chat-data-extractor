"""Gemini usage metering + the rate-limit view on the dashboard.

Important: the Gemini API has no "tokens remaining" balance endpoint and no way to read a key's
live rate limits (no headers, no quota endpoint — only AI Studio shows them, behind a Google
login). So everything here is assembled locally:
  - usage: the actual token counts Gemini returns (usageMetadata), recorded per call;
  - limits: the editable values in config, replaced per model by the real number whenever a
    429 reply from Google names the quota that was hit (note_429);
  - reset: Google resets the per-day quota at midnight Pacific time, so "today" is counted on
    that boundary (quota_day), not on the UTC date;
  - requests: every attempt per model (record_call), failed ones included — Google counts a
    503/429 against the quota too.
availability() turns that into "can this model be used right now?", which is what lets
gemini.py move down config.GEMINI_MODELS when a model is out of limits or overloaded.
"""
import json
import math
from datetime import datetime, timedelta, timezone

from . import config, db

_ISO = "%Y-%m-%dT%H:%M:%SZ"
_DB = "%Y-%m-%d %H:%M:%S"          # db.now()'s format (UTC)
_META_KEY = "gemini_quota"


def record(con, item_id, model, kind, usage_meta):
    pt = (usage_meta or {}).get("promptTokenCount", 0) or 0
    ot = (usage_meta or {}).get("candidatesTokenCount", 0) or 0
    tt = (usage_meta or {}).get("totalTokenCount", pt + ot) or 0
    con.execute(
        "INSERT INTO gemini_usage (id,item_id,model,kind,prompt_tokens,output_tokens,total_tokens,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (db.new_id(), item_id, model, kind, pt, ot, tt, db.now()))
    return tt


def _utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def record_call(model, outcome, now=None):
    """Count one request sent to `model` (outcome: ok | 429 | 503 | error). Never raises."""
    try:
        con = db.connect()
        con.execute("INSERT INTO gemini_calls (id, model, outcome, created_at) VALUES (?,?,?,?)",
                    (db.new_id(), model, outcome, (now or _utcnow()).strftime(_DB)))
        con.commit()
        con.close()
    except Exception as e:      # metering must never break an extraction
        print(f"  [usage] could not count the call: {e}")


def _nth_sunday(year, month, n):
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _pacific_offset(at):
    """US Pacific's UTC offset at a UTC instant: -7h in daylight time (second Sunday of March
    02:00 PST -> first Sunday of November 02:00 PDT), else -8h. Worked out by hand because
    Windows Python ships no timezone database."""
    dst_start = _nth_sunday(at.year, 3, 2) + timedelta(hours=10)
    dst_end = _nth_sunday(at.year, 11, 1) + timedelta(hours=9)
    return timedelta(hours=-7 if dst_start <= at < dst_end else -8)


def _pacific_midnight(day):
    """The UTC instant of 00:00 Pacific on a (naive) Pacific calendar day."""
    at = day.replace(tzinfo=timezone.utc) - _pacific_offset(day.replace(tzinfo=timezone.utc))
    return day.replace(tzinfo=timezone.utc) - _pacific_offset(at)


def quota_day(now=None):
    """(start, next_reset) of Google's current per-day quota window, as UTC datetimes."""
    now = now or _utcnow()
    local = (now + _pacific_offset(now)).replace(tzinfo=None)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return _pacific_midnight(day), _pacific_midnight(day + timedelta(days=1))


def _tokens_since(con, start, model=None):
    q = "SELECT COALESCE(SUM(total_tokens),0) t FROM gemini_usage WHERE created_at >= ?"
    args = [start.strftime(_DB)]
    if model:
        q += " AND model = ?"
        args.append(model)
    return con.execute(q, args).fetchone()["t"]


def _calls_since(con, start, model):
    return con.execute("SELECT COUNT(*) n FROM gemini_calls WHERE created_at >= ? AND model = ?",
                       (start.strftime(_DB), model)).fetchone()["n"]


# ---- limits Google has confirmed (learned from 429 replies) ----------------------------------

def _load(con):
    row = con.execute("SELECT value FROM meta WHERE key=?", (_META_KEY,)).fetchone()
    try:
        data = json.loads(row["value"]) if row else {}
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _quota_kind(violation):
    """Which of our three limits a QuotaFailure violation is about: rpm / tpm / rpd, else None."""
    text = f"{violation.get('quotaId', '')} {violation.get('quotaMetric', '')}".lower()
    tokens = "token" in text
    if "perday" in text:
        return None if tokens else "rpd"
    if "perminute" in text:
        return "tpm" if tokens else "rpm"
    return None


def _seconds(delay):
    """A google.protobuf.Duration string ("37s", "12.5s") as whole seconds, else None."""
    try:
        return int(math.ceil(float(str(delay).rstrip("s"))))
    except ValueError:
        return None


def note_429(model, body, now=None):
    """Remember what a 429 reply from Gemini says: the real value of each quota it names
    (per model), how long Google asks us to wait, and until when the model is out
    (`blocked_until`: the daily reset for a per-day quota, else the retry time / one minute).
    Called from gemini._generate; a bare 429 with no details still counts as "rate-limited at
    <time>". Never raises."""
    try:
        now = now or _utcnow()
        try:
            err = (json.loads(body.decode("utf-8", "replace")) or {}).get("error") or {}
        except (AttributeError, ValueError):
            err = {}
        if not isinstance(err, dict):
            err = {}
        limits, delay = {}, None
        for d in err.get("details") or []:
            kind = str(d.get("@type", ""))
            if kind.endswith("QuotaFailure"):
                for v in d.get("violations") or []:
                    k = _quota_kind(v)
                    if k and str(v.get("quotaValue", "")).isdigit():
                        limits[k] = {"value": int(v["quotaValue"]), "seen_at": now.strftime(_ISO)}
            elif kind.endswith("RetryInfo"):
                delay = _seconds(d.get("retryDelay"))
        if "rpd" in limits:
            until = _daily_until(now, delay)
        else:
            until = now + timedelta(seconds=delay if delay is not None else 60)
        last = {"at": now.strftime(_ISO), "kinds": sorted(limits), "retry_delay_s": delay,
                "retry_at": (now + timedelta(seconds=delay)).strftime(_ISO) if delay is not None else None,
                "blocked_until": until.strftime(_ISO),
                "message": str(err.get("message") or "")[:200]}
        con = db.connect()
        data = _load(con)
        entry = data.setdefault("models", {}).setdefault(model, {})
        entry.setdefault("limits", {}).update(limits)
        entry["last_429"] = last
        con.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_META_KEY, json.dumps(data)))
        con.commit()
        con.close()
    except Exception as e:      # metering must never break an extraction
        print(f"  [usage] could not record the 429 details: {e}")


_RESET_HINT_MIN = 3600   # a per-day refusal's wait this long (s) or longer is Google's own reset time


def _daily_until(at, delay):
    """When a model refused for its per-day quota is usable again. Google documents the reset as
    midnight Pacific, but a refusal seen live pointed at midnight UTC instead — so a long wait in
    the reply is taken as the real reset time. A short one ("retry in 30s", which Google also
    sends for per-day quotas) says nothing about the reset, so the documented rule applies."""
    if delay is not None and delay >= _RESET_HINT_MIN:
        return at + timedelta(seconds=delay)
    return quota_day(at)[1]


def _daily_refusal(last_429):
    """(refused_at, usable_again_at) of the last per-day 429, else (None, None). Worked out from
    the stored reply rather than its `blocked_until`, so older records follow the same rule."""
    at = _parse((last_429 or {}).get("at"))
    if not at or "rpd" not in ((last_429 or {}).get("kinds") or []):
        return None, None
    return at, _daily_until(at, last_429.get("retry_delay_s"))


def _day_start(last_429, now):
    """Where this model's per-day count starts: Google's quota day, or the reset time Google itself
    gave in a per-day refusal when that is more recent."""
    start = quota_day(now)[0]
    _, again = _daily_refusal(last_429)
    return again if again and start < again <= now else start


_overloaded = {}     # model -> UTC time until which it is left alone after a 503 (in-memory)


def note_503(model, now=None):
    """A 503 = that model is overloaded on Google's side. Leave it alone for a while."""
    _overloaded[model] = (now or _utcnow()) + timedelta(seconds=config.GEMINI_OVERLOAD_COOLDOWN)


def _limits(con, model):
    """({rpm,tpm,rpd: {value, source[, seen_at]}}, last_429) for a model. Source is "google" once
    a 429 confirmed the number, "settings" when it comes from .env (GEMINI_LIMITS, or the three
    GEMINI_*_LIMIT values for the main model), else "unknown" (value None)."""
    known = ((_load(con).get("models") or {}).get(model) or {})
    conf = config.GEMINI_LIMITS.get(model)
    if conf is None and model == config.GEMINI_MODEL:
        conf = {"rpm": config.GEMINI_RPM_LIMIT, "tpm": config.GEMINI_TPM_LIMIT, "rpd": config.GEMINI_RPD_LIMIT}
    out = {}
    for kind in ("rpm", "tpm", "rpd"):
        seen = (known.get("limits") or {}).get(kind)
        if seen:
            out[kind] = {"value": seen["value"], "source": "google", "seen_at": seen.get("seen_at")}
        elif conf:
            out[kind] = {"value": conf[kind], "source": "settings"}
        else:
            out[kind] = {"value": None, "source": "unknown"}
    return out, known.get("last_429")


def _parse(iso):
    try:
        return datetime.strptime(iso, _ISO).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def availability(model, now=None, con=None):
    """None if `model` can be used right now, else {"reason", "until"}:
    "daily" (out until Google's reset), "overloaded" (cooling down after a 503) or "minute"
    (at a per-minute limit). Google's own 429 is the final word; the app's count of attempts
    only predicts it, to avoid sending a request that is bound to fail."""
    now = now or _utcnow()
    own = con is None
    con = con or db.connect()
    try:
        limits, last_429 = _limits(con, model)
        reset = quota_day(now)[1]
        _, again = _daily_refusal(last_429)
        if again and again > now:
            return {"reason": "daily", "until": again}
        until = None if again else _parse((last_429 or {}).get("blocked_until"))
        said_no = until if until and until > now else None
        rpd = limits["rpd"]["value"]
        if rpd is not None and _calls_since(con, _day_start(last_429, now), model) >= rpd:
            return {"reason": "daily", "until": reset}
        cool = _overloaded.get(model)
        if cool and cool > now:
            return {"reason": "overloaded", "until": cool}
        if said_no:
            return {"reason": "minute", "until": said_no}
        minute = now - timedelta(seconds=60)
        rpm, tpm = limits["rpm"]["value"], limits["tpm"]["value"]
        if ((rpm is not None and _calls_since(con, minute, model) >= rpm)
                or (tpm is not None and _tokens_since(con, minute, model) >= tpm)):
            return {"reason": "minute", "until": now + timedelta(seconds=60)}
        return None
    finally:
        if own:
            con.close()


def budget_ok(con):
    """True if today's tokens are under our own budget and at least one model in the list is
    not out for the day (a per-minute limit or an overload is temporary, so it doesn't count)."""
    now = _utcnow()
    if _tokens_since(con, quota_day(now)[0]) >= config.DAILY_TOKEN_BUDGET:
        return False
    return any((availability(m, now, con) or {}).get("reason") != "daily" for m in config.GEMINI_MODELS)


def _model_view(con, model, now, start):
    limits, last_429 = _limits(con, model)
    start = _day_start(last_429, now)
    block = availability(model, now, con)
    minute = now - timedelta(seconds=60)
    return {"model": model,
            "today_requests": _calls_since(con, start, model),
            "today_tokens": _tokens_since(con, start, model),
            "minute_requests": _calls_since(con, minute, model),
            "minute_tokens": _tokens_since(con, minute, model),
            "limits": limits,
            "status": block["reason"] if block else "ok",
            "blocked_until": block["until"].strftime(_ISO) if block else None,
            "last_429": last_429}


def summary(con, now=None):
    now = now or _utcnow()
    start, reset = quota_day(now)
    models = [_model_view(con, m, now, start) for m in config.GEMINI_MODELS]
    main = models[0]
    tokens = _tokens_since(con, start)
    total = con.execute(
        "SELECT COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM gemini_usage").fetchone()
    rpd = main["limits"]["rpd"]["value"]
    return {
        "enabled": config.USE_GEMINI,
        "model": config.GEMINI_MODEL,
        "models": models,                      # in preference order; the top-level counts are the main model's
        "active_model": next((m["model"] for m in models if m["status"] == "ok"), None),
        "today_tokens": tokens,                # all models — our own DAILY_TOKEN_BUDGET spans them
        "today_requests": main["today_requests"],
        "minute_tokens": main["minute_tokens"],
        "minute_requests": main["minute_requests"],
        "total_tokens": total["t"],
        "total_requests": total["n"],
        "daily_token_budget": config.DAILY_TOKEN_BUDGET,
        "daily_tokens_remaining": max(0, config.DAILY_TOKEN_BUDGET - tokens),
        "limits": main["limits"],
        "rpd_limit": rpd,
        "requests_remaining": max(0, rpd - main["today_requests"]) if rpd is not None else None,
        "tpm_limit": main["limits"]["tpm"]["value"],
        "reset_at": reset.strftime(_ISO),
        "reset_in_seconds": int((reset - now).total_seconds()),
        "last_429": main["last_429"],
        "note": ("Google exposes no live quota. Requests (failed ones included) and tokens are "
                 "counted locally over Google's quota day (midnight Pacific); a limit is 'google' "
                 "once a 429 reply confirmed it, 'settings' if it comes from .env, else 'unknown'."),
    }
