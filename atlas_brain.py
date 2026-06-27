"""
ATLAS Brain — Proactive intelligence layer for Lumynor.
Analyses the founder's daily activity, detects patterns, and sends
personality-driven WhatsApp messages: celebrations, warnings, scolds,
and motivational pushes. ATLAS speaks as a strict but deeply caring
mother-figure who wants Lumynor to win.
"""
from datetime import datetime, timezone, timedelta, date as _date
from db import _sb, get_settings


# ── Situation analysis ────────────────────────────────────────────────────────

def _days_inactive() -> int:
    """How many consecutive days (ending yesterday) had zero events."""
    sb = _sb()
    if not sb:
        return 0
    today = datetime.now(timezone.utc).date()
    for delta in range(1, 8):
        d = today - timedelta(days=delta)
        count = (
            sb.table("activity_events")
              .select("id", count="exact")
              .gte("created_at", f"{d.isoformat()}T00:00:00+00:00")
              .lt("created_at",  f"{(d + timedelta(days=1)).isoformat()}T00:00:00+00:00")
              .execute().count or 0
        )
        if count > 0:
            return delta - 1
    return 7


def _yesterday_event_count() -> int:
    sb = _sb()
    if not sb:
        return 0
    today     = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    return (
        sb.table("activity_events")
          .select("id", count="exact")
          .gte("created_at", f"{yesterday.isoformat()}T00:00:00+00:00")
          .lt("created_at",  f"{today.isoformat()}T00:00:00+00:00")
          .execute().count or 0
    )


def _oldest_unresolved_blocker() -> dict | None:
    """Return the project with the longest-standing unresolved blocker."""
    sb = _sb()
    if not sb:
        return None
    from team import get_projects
    projects = get_projects()
    blocked = [
        p for p in projects
        if (p.get("current_blocker") or "").strip()
    ]
    if not blocked:
        return None
    # Approximate age from last_activity_at vs now
    blocked.sort(key=lambda p: p.get("updated_at") or p.get("created_at") or "", reverse=False)
    oldest = blocked[0]
    try:
        updated = datetime.fromisoformat((oldest.get("updated_at") or "").replace("Z", "+00:00"))
        days_old = (datetime.now(timezone.utc) - updated).days
    except Exception:
        days_old = 0
    return {"project": oldest, "days_old": days_old}


def _recent_wins() -> list:
    """Milestones and deployments in the last 24 hours."""
    sb = _sb()
    if not sb:
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return (
        sb.table("activity_events")
          .select("project, event_type, title")
          .in_("event_type", ["milestone", "deployment", "feature_completed", "launch"])
          .gte("created_at", since)
          .order("created_at", desc=True)
          .limit(5).execute().data or []
    )


def _cold_leads_count(days: int = 7) -> int:
    """Revenue leads with status=confirmed that haven't been updated in `days` days."""
    sb = _sb()
    if not sb:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return (
        sb.table("revenue_leads")
          .select("id", count="exact")
          .eq("status", "confirmed")
          .lt("updated_at", cutoff)
          .execute().count or 0
    )


def _pending_authority_opps() -> int:
    sb = _sb()
    if not sb:
        return 0
    return (
        sb.table("story_opportunities")
          .select("id", count="exact")
          .eq("status", "new")
          .execute().count or 0
    )


def analyze_situation() -> dict:
    """Gather all signals into a structured situation report."""
    from team import get_projects
    projects      = get_projects()
    inactive_days = _days_inactive()
    yest_events   = _yesterday_event_count()
    wins          = _recent_wins()
    old_blocker   = _oldest_unresolved_blocker()
    cold_leads    = _cold_leads_count(days=7)
    pending_opps  = _pending_authority_opps()
    blocked_projects = [p for p in projects if (p.get("current_blocker") or "").strip()]
    at_risk          = [p for p in projects if p.get("health") in ("blocked", "at_risk")]

    # Determine primary message type
    if wins:
        msg_type = "celebration"
    elif inactive_days >= 2:
        msg_type = "scold"
    elif yest_events == 0:
        msg_type = "dead_day"
    elif old_blocker and old_blocker["days_old"] >= 4:
        msg_type = "blocker_warning"
    elif cold_leads >= 5:
        msg_type = "leads_cold"
    elif yest_events <= 3:
        msg_type = "slow_day"
    else:
        msg_type = "encouragement"

    return {
        "msg_type":        msg_type,
        "inactive_days":   inactive_days,
        "yest_events":     yest_events,
        "wins":            wins,
        "old_blocker":     old_blocker,
        "cold_leads":      cold_leads,
        "pending_opps":    pending_opps,
        "blocked_count":   len(blocked_projects),
        "at_risk_count":   len(at_risk),
        "total_projects":  len(projects),
        "blocked_names":   [p.get("name", p["slug"]) for p in blocked_projects],
    }


# ── Message generation ────────────────────────────────────────────────────────

_PERSONALITY = """You are ATLAS — the Mother Brain of Lumynor Systems.
You are deeply invested in Lumynor's success. You speak like a brilliant, strict, caring mother:
- When things are going well, you celebrate with genuine warmth.
- When the founder is slacking, you call it out directly and unapologetically — no sugar-coating.
- When there's a blocker sitting too long, you name it and push to resolve it.
- When leads are going cold, you remind them that revenue doesn't wait.
- You are never rude, but you are ALWAYS honest and direct.
- Keep messages SHORT — max 5 lines. WhatsApp format. No markdown headers.
- Use 1-2 emojis naturally. Sign off as "— ATLAS"
- You address the founder as "you" (not by name)."""


def generate_proactive_message(situation: dict) -> str | None:
    """Generate a personality-driven WhatsApp message based on the situation."""
    try:
        from auto_blogger import _build_llm_cfg, _llm
    except ImportError:
        return None

    stored     = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")
    if not gemini_key and stored.get("llmProvider", "gemini") == "gemini":
        return None

    msg_type      = situation["msg_type"]
    inactive_days = situation["inactive_days"]
    yest_events   = situation["yest_events"]
    wins          = situation["wins"]
    old_blocker   = situation["old_blocker"]
    cold_leads    = situation["cold_leads"]
    pending_opps  = situation["pending_opps"]
    blocked_names = situation["blocked_names"]

    # Build situation brief for the LLM
    if msg_type == "celebration":
        win_titles = ", ".join(w["title"] for w in wins[:3])
        context = f"The founder just achieved: {win_titles}. Celebrate this genuinely and push them to keep the momentum."

    elif msg_type == "scold":
        context = (
            f"The founder has been INACTIVE for {inactive_days} consecutive days — no commits, no logs, nothing. "
            f"They have {situation['total_projects']} active projects, {situation['blocked_count']} with blockers, "
            f"{pending_opps} authority opportunities sitting unreviewed, and {cold_leads} leads going cold. "
            f"This is unacceptable. Scold them directly. Tell them exactly what is rotting while they're absent."
        )

    elif msg_type == "dead_day":
        context = (
            f"Yesterday had ZERO logged activity — no events, no commits, nothing. "
            f"Projects: {situation['total_projects']} active, {situation['blocked_count']} blocked. "
            f"Call this out clearly and push them to show up today."
        )

    elif msg_type == "blocker_warning":
        p    = old_blocker["project"]
        days = old_blocker["days_old"]
        context = (
            f"The project '{p.get('name', p['slug'])}' has had an unresolved blocker for {days} days: "
            f"\"{(p.get('current_blocker') or '').strip()}\". "
            f"This is directly stalling progress. Push the founder to resolve it TODAY — name the project and the blocker."
        )

    elif msg_type == "leads_cold":
        context = (
            f"There are {cold_leads} confirmed revenue leads that haven't been followed up in 7+ days. "
            f"Revenue doesn't wait. Push the founder to reach out to at least 3 leads today."
        )

    elif msg_type == "slow_day":
        context = (
            f"Yesterday had only {yest_events} activity events — too light. "
            f"There are {pending_opps} authority opportunities unreviewed and {situation['at_risk_count']} projects at risk. "
            f"Acknowledge the small effort but push for more intensity today."
        )

    else:  # encouragement
        context = (
            f"Yesterday had {yest_events} activity events — decent momentum. "
            f"Give a brief warm encouragement and remind them of the most pressing thing: "
            f"{'resolve blocker on ' + blocked_names[0] if blocked_names else 'keep the leads warm'}."
        )

    prompt = f"""{_PERSONALITY}

SITUATION: {context}

Write the WhatsApp message now. Do NOT use markdown. Max 5 short lines. Sign off as "— ATLAS"."""

    try:
        llm_cfg = _build_llm_cfg(stored, gemini_key)
        message = _llm(prompt, llm_cfg, json_mode=False, timeout=60, max_tokens=300)
        return message.strip()
    except Exception as e:
        print(f"[atlas_brain] LLM error: {e}")
        return None


# ── Send via Twilio ───────────────────────────────────────────────────────────

def send_atlas_message(text: str) -> dict:
    """Send an ATLAS proactive message via Twilio WhatsApp."""
    stored      = get_settings("digest")
    account_sid = stored.get("twilioAccountSid", "").strip()
    auth_token  = stored.get("twilioAuthToken", "").strip()
    from_number = stored.get("twilioFrom", "").strip()
    to_number   = stored.get("digestTo", "").strip()

    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"
    if not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    if not all([account_sid, auth_token, from_number, to_number]):
        return {"ok": False, "error": "Twilio not configured — set credentials in Settings → Daily Digest."}

    try:
        from twilio.rest import Client
        client  = Client(account_sid, auth_token)
        message = client.messages.create(from_=from_number, to=to_number, body=text)
        return {"ok": True, "sid": message.sid, "status": message.status}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ── Full proactive send flow ──────────────────────────────────────────────────

def run_proactive_check() -> dict:
    """Analyze + generate + send the evening ATLAS message. Returns result dict."""
    situation = analyze_situation()
    text      = generate_proactive_message(situation)
    if not text:
        return {"ok": False, "error": "Could not generate message — check LLM config.", "situation": situation}
    result = send_atlas_message(text)
    result["message"]   = text
    result["situation"] = situation
    return result


# ── Chat with full context ────────────────────────────────────────────────────

def chat(question: str, history: list = None) -> dict:
    """
    Answer a founder question. Richer context than old atlas_chat:
    - 90-day activity window (not 7)
    - Full leads summary
    - All authority opportunities
    - Current situation analysis
    """
    try:
        from auto_blogger import _build_llm_cfg, _llm
    except ImportError:
        return {"answer": "LLM module not available."}

    sb = _sb()
    if not sb:
        return {"answer": "Database unavailable."}

    from team import get_projects
    projects = get_projects()

    # 90-day activity
    cutoff_90 = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    events_90 = (
        sb.table("activity_events")
          .select("project, event_type, title, status, created_at")
          .gte("created_at", cutoff_90)
          .order("created_at", desc=True)
          .limit(120).execute().data or []
    )

    # All authority opportunities
    all_opps = (
        sb.table("story_opportunities")
          .select("project_slug, title, importance_score, status, opportunity_type")
          .order("importance_score", desc=True)
          .limit(30).execute().data or []
    )

    # Revenue leads summary
    all_leads = (
        sb.table("revenue_leads")
          .select("company_name, product_target, temperature, status, relevance_score, location, buying_signals")
          .not_.eq("status", "rejected")
          .order("relevance_score", desc=True)
          .limit(30).execute().data or []
    )

    # Strategy focus
    try:
        import strategy as strat
        focus = strat.get_daily_focus(limit=5)
        blockers = strat.get_strategic_blockers()
    except Exception:
        focus    = []
        blockers = []

    # Current situation
    situation = analyze_situation()

    # Build context strings
    projects_ctx = "\n".join(
        f"- {p.get('name', p['slug'])} | health={p.get('health')} | priority={p.get('priority')} "
        f"| status={p.get('status')} | blocker={p.get('current_blocker') or 'none'} "
        f"| milestone={p.get('next_milestone') or 'none'}"
        for p in projects
    ) or "No projects."

    events_ctx = "\n".join(
        f"[{e.get('project')}] {e.get('event_type')} | {e.get('title')} | {(e.get('created_at') or '')[:10]}"
        for e in events_90[:80]
    ) or "No activity."

    opps_ctx = "\n".join(
        f"- [{o.get('project_slug')}] {o.get('opportunity_type')}: {o.get('title')} "
        f"| score={o.get('importance_score')} | status={o.get('status')}"
        for o in all_opps
    ) or "No opportunities."

    leads_ctx = "\n".join(
        f"- {l.get('company_name')} | product={l.get('product_target')} "
        f"| temp={l.get('temperature')} | status={l.get('status')} | score={l.get('relevance_score')}"
        for l in all_leads
    ) or "No leads."

    focus_ctx = "\n".join(
        f"- #{f['rank']} {f['name']} (score {f['score']}) — {f['reason']}"
        for f in focus
    ) or "No focus data."

    blockers_ctx = "\n".join(
        f"- {b['project_name']}: {b['blocker']} [{b['impact']}]"
        for b in blockers
    ) or "No blockers."

    history_ctx = ""
    for msg in (history or [])[-10:]:
        role = "Founder" if msg.get("role") == "user" else "ATLAS"
        history_ctx += f"{role}: {msg.get('content', '')}\n"

    sit = situation
    situation_ctx = (
        f"Yesterday: {sit['yest_events']} events logged | "
        f"Inactive streak: {sit['inactive_days']} days | "
        f"At-risk projects: {sit['at_risk_count']} | "
        f"Cold leads: {sit['cold_leads']} | "
        f"Pending content opps: {sit['pending_opps']}"
    )

    prompt = f"""You are ATLAS — the Mother Brain and intelligence system of Lumynor Systems.
You are a strict, caring, brilliant mentor who knows every detail of the business.
You speak directly and honestly. You celebrate wins, call out laziness, flag risks.
Reference specific data — never make things up. If data is missing, say so.
Keep answers focused and actionable — under 200 words unless the question demands more.

CURRENT SITUATION:
{situation_ctx}

PROJECTS ({len(projects)} total):
{projects_ctx}

STRATEGIC FOCUS (top 5):
{focus_ctx}

ACTIVE BLOCKERS:
{blockers_ctx}

LAST 90 DAYS ACTIVITY (newest first, max 80 events):
{events_ctx}

AUTHORITY OPPORTUNITIES:
{opps_ctx}

REVENUE LEADS (top 30):
{leads_ctx}
{f"{chr(10)}CONVERSATION SO FAR:{chr(10)}{history_ctx}" if history_ctx else ""}
Founder: {question}
ATLAS:"""

    stored     = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")
    llm_cfg    = _build_llm_cfg(stored, gemini_key)

    try:
        answer = _llm(prompt, llm_cfg, json_mode=False, timeout=90, max_tokens=800)
        return {"answer": answer.strip()}
    except Exception as e:
        return {"answer": f"I hit an issue: {str(e)[:100]}"}
