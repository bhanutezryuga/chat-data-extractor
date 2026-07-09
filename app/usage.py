"""Gemini token-usage metering.

Important: the Gemini API has no "tokens remaining" balance endpoint. We record the
actual token counts Gemini returns (usageMetadata) per call and compare cumulative
usage against editable daily limits. Days are counted in UTC.
"""
from . import config, db


def record(con, item_id, model, kind, usage_meta):
    pt = (usage_meta or {}).get("promptTokenCount", 0) or 0
    ot = (usage_meta or {}).get("candidatesTokenCount", 0) or 0
    tt = (usage_meta or {}).get("totalTokenCount", pt + ot) or 0
    con.execute(
        "INSERT INTO gemini_usage (id,item_id,model,kind,prompt_tokens,output_tokens,total_tokens,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (db.new_id(), item_id, model, kind, pt, ot, tt, db.now()))
    return tt


def _today(con):
    row = con.execute(
        "SELECT COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM gemini_usage "
        "WHERE substr(created_at,1,10) = substr(?,1,10)", (db.now(),)).fetchone()
    return row["t"], row["n"]


def budget_ok(con):
    """True if today's usage is under both the token budget and the daily request limit."""
    tokens, reqs = _today(con)
    return tokens < config.DAILY_TOKEN_BUDGET and reqs < config.GEMINI_RPD_LIMIT


def summary(con):
    tokens, reqs = _today(con)
    total = con.execute(
        "SELECT COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM gemini_usage").fetchone()
    return {
        "enabled": config.USE_GEMINI,
        "model": config.GEMINI_MODEL,
        "today_tokens": tokens,
        "today_requests": reqs,
        "total_tokens": total["t"],
        "total_requests": total["n"],
        "daily_token_budget": config.DAILY_TOKEN_BUDGET,
        "daily_tokens_remaining": max(0, config.DAILY_TOKEN_BUDGET - tokens),
        "rpd_limit": config.GEMINI_RPD_LIMIT,
        "requests_remaining": max(0, config.GEMINI_RPD_LIMIT - reqs),
        "tpm_limit": config.GEMINI_TPM_LIMIT,
        "note": "Gemini exposes rate limits, not a token balance. These are metered/estimated locally (UTC day).",
    }
