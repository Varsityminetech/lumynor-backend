"""
ATLAS Chat — mid-week intelligence queries against live Supabase data.
Answers the founder's questions using the same data Weekly Intel gathers,
without requiring a Monday report to exist first.
"""
import json
from weekly_intel import _gather_intelligence
from db import get_settings


def chat(question: str, history: list = None) -> dict:
    """Answer a founder question using live portfolio data."""
    try:
        from auto_blogger import _build_llm_cfg, _llm
    except ImportError:
        return {"answer": "LLM module not available."}

    intel = _gather_intelligence()
    if not intel:
        return {"answer": "Could not load portfolio data — check the database connection."}

    scorecard   = intel["scorecard"]
    projects    = intel["projects"]
    events_7d   = intel["events_7d"]
    opps        = intel["all_opportunities"]
    attention   = intel["attention_pct"]

    projects_ctx = "\n".join(
        f"- {p.get('name', p['slug'])} (slug={p['slug']}) | health={p.get('health')} "
        f"| priority={p.get('priority')} | status={p.get('status')} "
        f"| blocker={p.get('current_blocker') or 'none'} "
        f"| milestone={p.get('next_milestone') or 'none'}"
        for p in projects
    ) or "No projects."

    events_ctx = "\n".join(
        f"[{e.get('project')}] {e.get('event_type')} | {e.get('title')} "
        f"| status={e.get('status')} | {(e.get('created_at') or '')[:10]}"
        for e in events_7d[:60]
    ) or "No events in last 7 days."

    opps_ctx = "\n".join(
        f"- [{o.get('project_slug')}] {o.get('title')} "
        f"| score={o.get('importance_score')} | status={o.get('status')}"
        for o in opps[:10]
    ) or "No story opportunities."

    attention_ctx = "\n".join(
        f"- {slug}: {pct}%"
        for slug, pct in list(attention.items())[:6]
    ) or "No attention data this week."

    # Include last 6 messages (3 exchanges) for context
    history_ctx = ""
    if history:
        for msg in (history or [])[-6:]:
            role = "Founder" if msg.get("role") == "user" else "ATLAS"
            history_ctx += f"{role}: {msg.get('content', '')}\n"

    prompt = f"""You are ATLAS, the intelligence system for Lumynor Systems.
Answer the founder's question using the live portfolio data below. Be direct, specific, and reference actual data.
Do not invent information — if the data doesn't cover the question, say so.

WEEKLY SCORECARD:
Score: {scorecard['weekly_score']}/100 | Active: {scorecard['projects_active']} | Healthy: {scorecard['projects_healthy']} | At Risk: {scorecard['projects_at_risk']} | Blocked: {scorecard['critical_blockers']}

PROJECTS:
{projects_ctx}

LAST 7 DAYS ACTIVITY (newest first):
{events_ctx}

STORY OPPORTUNITIES:
{opps_ctx}

FOUNDER ATTENTION THIS WEEK:
{attention_ctx}
{f"{chr(10)}CONVERSATION SO FAR:{chr(10)}{history_ctx}" if history_ctx else ""}
Founder: {question}
ATLAS:"""

    stored    = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")
    llm_cfg   = _build_llm_cfg(stored, gemini_key)

    try:
        answer = _llm(prompt, llm_cfg, json_mode=False, timeout=60, max_tokens=600)
        return {"answer": answer.strip()}
    except Exception as e:
        return {"answer": f"I ran into an issue fetching the answer: {str(e)[:100]}"}
