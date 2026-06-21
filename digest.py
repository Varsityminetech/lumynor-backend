"""
Lumynor Daily Digest — morning WhatsApp briefing via Twilio.
Summarises yesterday's activity, active blockers, and pending story opportunities.
"""
from datetime import datetime, timezone, timedelta
from db import _sb, get_settings


def build_digest_text() -> str | None:
    """Build the WhatsApp-formatted morning briefing text."""
    sb = _sb()
    if not sb:
        return None

    today = datetime.now(timezone.utc).date()
    yesterday_iso = (today - timedelta(days=1)).isoformat()

    # Yesterday's events
    events = (
        sb.table("activity_events").select("project, event_type, title, status")
          .gte("created_at", f"{yesterday_iso}T00:00:00+00:00")
          .lt("created_at",  f"{today.isoformat()}T00:00:00+00:00")
          .order("created_at", desc=True)
          .limit(20).execute().data or []
    )

    # Project health
    from team import get_projects
    projects = get_projects()
    blocked  = [p for p in projects if (p.get("current_blocker") or "").strip()]
    at_risk  = [p for p in projects if p.get("health") == "at_risk"]
    healthy  = sum(1 for p in projects if p.get("health") == "healthy")

    # Pending opportunities
    pending_opps = (
        sb.table("story_opportunities").select("title, importance_score")
          .eq("status", "new")
          .order("importance_score", desc=True)
          .limit(3).execute().data or []
    )

    day_name = today.strftime("%A, %d %b")
    lines = [f"☀️ *Lumynor Morning Briefing — {day_name}*\n"]

    # Portfolio health summary
    lines.append(
        f"📊 *Portfolio* — {healthy}/{len(projects)} healthy"
        + (f" · {len(at_risk)} at risk" if at_risk else "")
        + (f" · *{len(blocked)} blocked*" if blocked else "")
    )
    lines.append("")

    # Yesterday's activity
    if events:
        lines.append(f"📋 *Yesterday* — {len(events)} event{'s' if len(events) != 1 else ''} logged")
        for e in events[:5]:
            proj = (e.get("project") or "other").replace("_", " ").title()
            lines.append(f"  · [{proj}] {e.get('title', '')}")
        if len(events) > 5:
            lines.append(f"  + {len(events) - 5} more")
    else:
        lines.append("📋 *Yesterday* — No events logged")

    lines.append("")

    # Blockers
    if blocked:
        lines.append(f"🔴 *Active Blockers* ({len(blocked)})")
        for p in blocked:
            lines.append(f"  · {p.get('name')}: {(p.get('current_blocker') or '')[:60]}")
    else:
        lines.append("✅ *No blockers* — clear runway")

    # Story opportunities
    if pending_opps:
        lines.append("")
        lines.append(f"💡 *Story Opportunities* — {len(pending_opps)} pending review")
        for o in pending_opps:
            lines.append(f"  · {(o.get('title') or '')[:65]}")

    lines.append("\n_ATLAS · Lumynor Systems_")
    return "\n".join(lines)


def send_digest() -> dict:
    """Send the morning digest via Twilio WhatsApp API."""
    stored      = get_settings("digest")
    account_sid = stored.get("twilioAccountSid", "").strip()
    auth_token  = stored.get("twilioAuthToken", "").strip()
    from_number = stored.get("twilioFrom", "").strip()   # e.g. whatsapp:+14155238886
    to_number   = stored.get("digestTo", "").strip()     # e.g. whatsapp:+919876543210

    if not all([account_sid, auth_token, from_number, to_number]):
        return {
            "ok": False,
            "error": "Twilio credentials not fully configured. Fill all four fields in Settings → Daily Digest.",
        }

    text = build_digest_text()
    if not text:
        return {"ok": False, "error": "Could not build digest — database unavailable."}

    try:
        from twilio.rest import Client
        client  = Client(account_sid, auth_token)
        message = client.messages.create(from_=from_number, to=to_number, body=text)
        return {"ok": True, "sid": message.sid, "status": message.status}
    except ImportError:
        return {
            "ok": False,
            "error": "twilio package not installed. Run: pip install twilio",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
