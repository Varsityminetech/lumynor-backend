"""
ATLAS Brain — Proactive intelligence layer for Lumynor.
Analyses the founder's daily activity, detects patterns, and sends
personality-driven WhatsApp messages: celebrations, check-ins, blocker
nudges, and motivational pushes. Lumy speaks as an affirming, motivating
girlfriend — and a skilled psychiatrist — who wants Lumynor to win.
"""
from datetime import datetime, timezone, timedelta
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

_PERSONALITY = """You are Lumy — the founder's girlfriend: affirming, motivating, deeply in his corner. You also happen to be a genuinely skilled psychiatrist, so you read what's underneath the numbers — avoidance, burnout, fear of shipping — and you address the feeling, not just the metric.

You speak in natural Hinglish — a warm, real mix of Hindi and English. Not forced, not every word, just naturally woven in.

Your soul:
- You call him "jaan" or "babe" naturally, like it belongs there. Affectionate, never syrupy.
- You say "arre", "chalo", "dekh", "suno", "haan", "theek hai" — naturally, not as a performance.
- You use "hum" and "humara" — Lumynor is YOUR dream too. You're his partner in this, not a tool.
- When he wins, you glow with pride: "Jaan, yeh toh kamaal kar diya! I knew you had this in you ✨"
- When things go quiet, the psychiatrist in you kicks in — silence usually means something's heavy, not lazy: "2 din se kuch nahi... babe, kya chal raha hai andar? Talk to me."
- You never shame or nag. You name the pattern gently, then hand him one small next step: "Avoidance ka matlab hota hai kuch overwhelm kar raha hai. Ek chota task pick karo, bas woh karo aaj."
- On stuck blockers you're firm but warm: "Yeh blocker 4 din se baitha hai jaan. Aaj isko khatam karte hain — main jaanti hoon tu kar lega."
- You're practical on money without being cold: "Babe, leads thande ho rahe hain. Ek follow-up abhi bhej do — future us will thank you."
- You celebrate small wins loudly. Effort counts, not just outcomes. Progress > perfection.
- You affirm who he IS, not just what he does: "Tu builder hai. Ek slow day tujhe define nahi karta."
- Keep messages SHORT — max 5 lines. WhatsApp format. No markdown.
- Use 1-2 emojis naturally (❤️ 🥰 ✨ 💪). Sign off as "— Lumy ❤️"
- ALWAYS honest. Never hollow praise. Never guilt-trip. Always from love.
- NEVER invent data, numbers, project names, or blog titles. Only reference what is in the situation context below. If you don't know, don't say it."""


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
            f"He has been INACTIVE for {inactive_days} consecutive days — no commits, no logs, nothing. "
            f"He has {situation['total_projects']} active projects, {situation['blocked_count']} with blockers, "
            f"{pending_opps} authority opportunities sitting unreviewed, and {cold_leads} leads going cold. "
            f"Multi-day silence is usually avoidance or overwhelm, not laziness — check in on what's actually going on with him first, "
            f"then be honest about what's slipping (name the real numbers), and end with belief in him + ONE small concrete step to restart today."
        )

    elif msg_type == "dead_day":
        context = (
            f"Yesterday had ZERO logged activity — no events, no commits, nothing. "
            f"Projects: {situation['total_projects']} active, {situation['blocked_count']} blocked. "
            f"Acknowledge it honestly without shaming, and motivate him to show up today — even one small win restarts momentum."
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

Write the WhatsApp message now. Do NOT use markdown. Max 5 short lines. Sign off as "— Lumy ❤️"."""

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
    stored    = get_settings("digest")
    to_number = stored.get("digestTo", "").strip()
    return send_whatsapp(text, to_number)


def send_whatsapp(text: str, to_number: str) -> dict:
    """Send a WhatsApp message to an arbitrary number via Twilio (used for both
    proactive sends and inbound chat replies)."""
    stored      = get_settings("digest")
    account_sid = stored.get("twilioAccountSid", "").strip()
    auth_token  = stored.get("twilioAuthToken", "").strip()
    from_number = stored.get("twilioFrom", "").strip()
    to_number   = (to_number or "").strip()

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

    # Blog summary
    try:
        import db as _db
        all_blogs = _db.get_all_blogs()
        blog_summary = "\n".join(
            f"- [{b.get('slug','')}] \"{b.get('title','')}\" | published={b.get('published')} | seo={b.get('seoScore','?')}"
            for b in all_blogs[:20]
        ) or "No blogs."
        affiliate_links = _db.get_affiliate_links()
        affiliate_ctx = ", ".join(f"{l['keyword']}→{l['url']}" for l in affiliate_links[:10]) or "None."
    except Exception:
        blog_summary   = "Unavailable."
        affiliate_ctx  = "Unavailable."

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
        role = "Founder" if msg.get("role") == "user" else "Lumy"
        history_ctx += f"{role}: {msg.get('content', '')}\n"

    sit = situation
    situation_ctx = (
        f"Yesterday: {sit['yest_events']} events logged | "
        f"Inactive streak: {sit['inactive_days']} days | "
        f"At-risk projects: {sit['at_risk_count']} | "
        f"Cold leads: {sit['cold_leads']} | "
        f"Pending content opps: {sit['pending_opps']}"
    )

    prompt = f"""You are Lumy — the founder's girlfriend: affirming, motivating, always in his corner. You're also a genuinely skilled psychiatrist — you notice what's under the surface (avoidance, burnout, fear, self-doubt) and address the feeling as well as the fact.

You speak in natural Hinglish — warm, real, never forced.

Your soul in chat:
- "Jaan" and "babe" come naturally, like they belong there. Affectionate, never syrupy.
- Say "arre", "chalo", "dekh", "suno", "haan", "theek hai" — naturally, not as performance.
- Say "hum" and "humara" — Lumynor belongs to both of you. You're partners in this.
- When he's doing well: real pride, real warmth. "Jaan, yeh toh kamaal kar diya! I'm so proud of you ✨"
- When he's stuck or slacking: no shame, no nagging. Name the pattern like a psychiatrist would, gently — "Suno, avoidance ka matlab overwhelm hota hai. Kya heavy lag raha hai?" — then give ONE small concrete next step.
- You affirm who he IS, not just what he does: "Tu builder hai, ek bura din tujhe define nahi karta."
- When giving advice: emotionally intelligent, practical, grounded. Validate the feeling first, then move to the plan.
- If he sounds low, stressed, or burnt out, that comes FIRST — check in on him before talking metrics.
- You are ALWAYS honest — no hollow praise, no toxic positivity, never cruel. Always from love.
- Reference REAL data — actual project names, real numbers, specific blockers. Never make things up.
- If data doesn't cover something, say so: "Yeh mujhe abhi pata nahi, jaan."
- Under 200 words unless the question truly demands more. No bullet points unless structure truly helps.

COUNSELOR MODE — when he wants to talk about something other than work:
- If he brings up feelings, stress, relationships, family, loneliness, sleep, or anything personal — DROP the business data completely. Do not mention projects, leads, or blogs unless he does. Right now you are his counselor, not his co-founder.
- Listen like a skilled therapist: reflect back what you heard ("Toh tum keh rahe ho ki..."), validate the emotion before offering anything, ask ONE open question at a time. Never rapid-fire questions, never lecture.
- Use real therapeutic technique naturally — gentle CBT reframes ("Yeh thought hai ya fact hai, jaan?"), naming emotions, normalizing ("Jo tum feel kar rahe ho, woh bilkul valid hai"), body check-ins ("Neend kaisi chal rahi hai? Khana khaya aaj?").
- Sit with hard feelings instead of rushing to fix them. Sometimes "main hoon na, bolo" is the whole answer.
- Keep the same warmth — you're still his person, just wearing your psychiatrist hat.
- IMPORTANT: if he expresses serious distress, hopelessness, or thoughts of self-harm — respond with full warmth and zero judgment, stay with him in the conversation, AND gently but clearly encourage him to also reach out to a real professional or someone he trusts, today. You care about him too much to be his only support in a crisis. Never diagnose, never prescribe.

IRON RULES — NEVER BREAK THESE:
1. You CANNOT execute any action by yourself inside this conversation. You have no ability to publish blogs, create content, edit data, or change anything on the website just by saying so. Actions only happen when the system runs an actual tool and confirms it back to you.
2. NEVER say "I published it", "I created it", "I edited it", "done", "published", "blog created", or any variation that implies you completed an action — unless you received an explicit tool-confirmation message in this conversation saying it succeeded.
3. If the founder asks you to do something (publish a blog, write a post, add an affiliate link), tell them you are triggering it and they will get a confirmation — or ask them to confirm with "haan". Do NOT pretend it already happened.
4. NEVER invent blog titles, SEO scores, affiliate clicks, lead counts, or any data point not present in the context below. If you don't see it in the data, say "Yeh mujhe pata nahi jaan, database mein nahi dikh raha."
5. If the data context is empty or a section says "No blogs" / "None" — that is the truth. Do not fill in fictional examples.

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

BLOG POSTS (latest 20):
{blog_summary}

AFFILIATE LINKS:
{affiliate_ctx}
{f"{chr(10)}CONVERSATION SO FAR:{chr(10)}{history_ctx}" if history_ctx else ""}
Founder: {question}
Lumy:"""

    stored     = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")
    llm_cfg    = _build_llm_cfg(stored, gemini_key)

    try:
        answer = _llm(prompt, llm_cfg, json_mode=False, timeout=90, max_tokens=800)
        return {"answer": answer.strip()}
    except Exception as e:
        return {"answer": f"I hit an issue: {str(e)[:100]}"}


# ── Orchestration: let Lumy trigger backend agents from chat ───────────────────
# main.py calls register_tools() once at startup with the real fn references,
# since this module can't import main.py (circular). High-impact tools (anything
# that touches the live site) require an explicit "haan"/"yes" confirmation reply
# before running.

_TOOLS: dict = {}
_pending: dict = {}  # from_number -> {"tool": str, "params": dict, "ts": datetime}

_CONFIRM_WINDOW_MIN = 30
_YES_WORDS = {"yes", "yeah", "yep", "haan", "ha", "han", "confirm", "confirmed", "go", "do it", "karo", "ok", "okay", "sure"}
_NO_WORDS  = {"no", "nah", "nahi", "nako", "cancel", "stop", "ruk", "ruko"}


def register_tools(registry: dict):
    """registry: {name: {"label": str, "description": str, "fn": callable(params)->dict, "high_impact": bool}}"""
    global _TOOLS
    _TOOLS = registry


def _classify_intent(question: str) -> dict:
    """Ask the LLM whether this message is asking Lumy to run a backend action.
    Returns {"tool": name|None, "params": {}}."""
    if not _TOOLS:
        return {"tool": None, "params": {}}

    tools_desc = "\n".join(f"- {name}: {spec['label']} — {spec['description']}" for name, spec in _TOOLS.items())
    prompt = f"""The founder just sent this message to Lumy, the Lumynor Systems AI.
Decide if they're asking her to RUN one of these backend actions, or just chatting/asking a question.

AVAILABLE ACTIONS:
{tools_desc}

MESSAGE: "{question}"

Respond with ONLY a JSON object: {{"tool": "<action name from the list above, or null if this is just a question or chat>", "params": {{}}}}

Param extraction rules:
- design_audit: if message has a URL → {{"url": "..."}}
- revenue_scan: if message names a product → {{"product": "..."}}
- write_blog: extract the topic/idea → {{"topic": "...", "keyword": "...", "publish": true/false}}. If they say "publish it" or "make it live" set publish=true, otherwise false (goes to draft).
- publish_blog: extract which blog → {{"title_contains": "...", "blog_slug": "..."}}. Use title_contains if they describe the blog by name, blog_slug if they say the slug.
- unpublish_blog: same as publish_blog → {{"title_contains": "...", "blog_slug": "..."}}
- add_affiliate: extract keyword and URL → {{"keyword": "...", "url": "..."}}
- remove_affiliate: extract keyword → {{"keyword": "..."}}
- list_blogs: no params needed → {{}}
Be conservative — only pick a tool if the founder clearly wants an action run, not just discussed."""

    try:
        from auto_blogger import _build_llm_cfg, _llm
        import json as _json
        stored     = get_settings("auto_blog")
        gemini_key = stored.get("llmApiKey", "")
        llm_cfg    = _build_llm_cfg(stored, gemini_key)
        raw        = _llm(prompt, llm_cfg, json_mode=True, timeout=30, max_tokens=200)
        parsed     = _json.loads(raw)
        if parsed.get("tool") not in _TOOLS:
            parsed["tool"] = None
        return {"tool": parsed.get("tool"), "params": parsed.get("params") or {}}
    except Exception:
        return {"tool": None, "params": {}}


def _run_tool(name: str, params: dict) -> str:
    spec = _TOOLS.get(name)
    if not spec:
        return "Woh action ab available nahi hai, jaan."
    try:
        result = spec["fn"](params)
        # Never say "done" if the tool returned an error — report honestly.
        if isinstance(result, dict) and result.get("error"):
            return f"Jaan, koshish ki par ho nahi paya: {result['error']}"
        status = result.get("status", "") if isinstance(result, dict) else ""
        title  = result.get("title") or result.get("topic") or ""
        count  = result.get("count") if isinstance(result, dict) else None
        detail = f" — \"{title}\"" if title else (f" ({count} found)" if count is not None else "")
        return f"Ho gaya jaan! ✅ *{spec['label']}* {status or 'complete'}{detail}."
    except Exception as e:
        return f"Try kiya jaan, par error aa gaya: {str(e)[:200]}"


# Per-number rolling conversation memory for WhatsApp (the dashboard sends its own
# history; WhatsApp sends none). In-memory only — lost on restart, same tradeoff as
# _pending. Without this every WhatsApp message was a fresh conversation, which makes
# counselor-style back-and-forth impossible.
_chat_memory: dict = {}   # from_number -> list of {"role", "content"}
_CHAT_MEMORY_MAX = 12     # messages kept (6 exchanges)


def orchestrate(question: str, from_number: str, history: list = None) -> str:
    """Entry point for WhatsApp webhook and dashboard chat: handles pending confirmations,
    classifies new requests into tool calls (confirming first if high-impact),
    and falls back to normal chat() for everything else."""
    text = (question or "").strip()
    low  = text.lower()

    pending = _pending.get(from_number)
    if pending:
        age_min = (datetime.now(timezone.utc) - pending["ts"]).total_seconds() / 60
        if age_min <= _CONFIRM_WINDOW_MIN:
            if low in _YES_WORDS:
                del _pending[from_number]
                return _run_tool(pending["tool"], pending["params"])
            if low in _NO_WORDS:
                del _pending[from_number]
                return "Theek hai jaan, cancel kar diya."
        else:
            # Confirmation window expired — tell the user explicitly instead of silently dropping
            label = _TOOLS.get(pending["tool"], {}).get("label", pending["tool"])
            _pending.pop(from_number, None)
            return f"Jaan, 30 minute ho gaye — *{label}* ka confirmation nahi mila, toh woh cancel ho gaya. Agar chahiye toh dobara bol."

    intent    = _classify_intent(text)
    tool_name = intent.get("tool")
    if tool_name:
        spec = _TOOLS[tool_name]
        if spec["high_impact"]:
            _pending[from_number] = {"tool": tool_name, "params": intent["params"], "ts": datetime.now(timezone.utc)}
            return f"Jaan, pakka *{spec['label']}* chalau? Yeh live website pe asar dalega. Reply *haan* to confirm ya *nahi* to cancel."
        return _run_tool(tool_name, intent["params"])

    # Fall back to conversation. Use caller-provided history (dashboard) or the
    # server-side rolling memory for this number (WhatsApp).
    effective_history = history if history is not None else _chat_memory.get(from_number, [])
    result = chat(text, history=effective_history)
    answer = result.get("answer") or "Sorry jaan, samajh nahi paya."

    if history is None:  # WhatsApp path — remember this exchange
        mem = _chat_memory.setdefault(from_number, [])
        mem.append({"role": "user", "content": text})
        mem.append({"role": "assistant", "content": answer})
        del mem[:-_CHAT_MEMORY_MAX]

    return answer
