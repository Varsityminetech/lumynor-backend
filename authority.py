"""
Lumynor Authority OS — Story Opportunity Engine
Identifies publishable story opportunities from engineering activity.
Does NOT generate content. Only identifies what is worth talking about.
"""
import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from db import _sb

OPPORTUNITY_TYPES = [
    'Product Progress',
    'Technical Insight',
    'Founder Lesson',
    'Milestone',
    'Launch Update',
    'Build In Public',
    'Case Study Opportunity',
]

OPPORTUNITY_STATUSES = ['new', 'reviewed', 'approved', 'rejected', 'published']


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Read ───────────────────────────────────────────────────────────────────────

def get_opportunities(status: str = None, project_slug: str = None, limit: int = 200) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("story_opportunities").select("*").order("importance_score", desc=True)
    if status:
        q = q.eq("status", status)
    if project_slug:
        q = q.eq("project_slug", project_slug)
    return q.limit(limit).execute().data or []


def update_opportunity(opp_id: str, **kwargs) -> dict | None:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    allowed = {'status', 'title', 'summary', 'why_it_matters', 'suggested_angle', 'importance_score'}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return None
    updates['updated_at'] = _now()
    sb.table("story_opportunities").update(updates).eq("id", opp_id).execute()
    r = sb.table("story_opportunities").select("*").eq("id", opp_id).limit(1).execute()
    return r.data[0] if r.data else None


def delete_opportunity(opp_id: str) -> None:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    sb.table("story_opportunities").delete().eq("id", opp_id).execute()


# ── Scan Engine ───────────────────────────────────────────────────────────────

def _get_used_event_ids() -> set:
    """Collect event IDs already referenced in non-rejected opportunities."""
    sb = _sb()
    if not sb:
        return set()
    rows = (
        sb.table("story_opportunities").select("source_event_ids")
          .not_.eq("status", "rejected")
          .execute().data or []
    )
    ids: set = set()
    for r in rows:
        for eid in (r.get("source_event_ids") or []):
            ids.add(eid)
    return ids


def scan_opportunities(days: int = 60) -> dict:
    """Use the LLM to identify story opportunities from recent activity events.
    Returns a dict: {created, count, debug}."""
    sb = _sb()
    if not sb:
        return {"created": [], "count": 0, "debug": "DB not configured"}

    try:
        from auto_blogger import _build_llm_cfg, _llm
        from db import get_settings
    except ImportError:
        raise RuntimeError("auto_blogger not available")

    # Load stored LLM settings (same source as auto_blogger pipeline)
    stored = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")

    # Events from last `days` days — include ALL projects for broader coverage
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    events = (
        sb.table("activity_events").select("*")
          .gte("created_at", cutoff)
          .order("created_at", desc=True)
          .limit(150).execute().data or []
    )
    # Exclude the generic 'other' bucket but keep everything else
    events = [e for e in events if e.get("project") and e["project"] != "other"]

    print(f"[authority] found {len(events)} events in last {days} days across projects: {list(set(e.get('project','?') for e in events))}")
    if not events:
        return {"created": [], "count": 0, "debug": f"No activity events found in last {days} days"}

    # Project context
    projects = (
        sb.table("projects").select(
            "slug, name, status, priority, strategic_importance, current_blocker, next_milestone"
        ).execute().data or []
    )

    # Only process events not already captured in an existing opportunity
    used_ids   = _get_used_event_ids()
    new_events = [e for e in events if e["id"] not in used_ids]
    if not new_events:
        return {"created": [], "count": 0, "debug": f"All {len(events)} events already captured in existing opportunities"}

    events_text = "\n".join(
        f"[{e['project']}] {e.get('event_type','')} | {e.get('title','')} | "
        f"status={e.get('status','')} | {(e.get('created_at') or '')[:10]}"
        for e in new_events[:80]
    )

    projects_text = "\n".join(
        f"{p['slug']} | {p.get('name','')} | {p.get('status','')} | "
        f"{p.get('strategic_importance','')} | milestone: {p.get('next_milestone','')}"
        for p in projects
    ) or "No projects configured yet."

    prompt = f"""You are the Authority OS for Lumynor Systems, a company building AI-powered SaaS products.

Your job: identify which engineering activities have potential as public stories for a founder building in public.
You do NOT write content. You ONLY identify opportunities.

OPPORTUNITY TYPES:
- Product Progress: meaningful feature or product advancement
- Technical Insight: interesting technical problem solved or approach taken
- Founder Lesson: challenge, mistake, or decision with educational value
- Milestone: significant achievement, launch, or completion
- Launch Update: product going live or a major release
- Build In Public: interesting development journey moment worth sharing
- Case Study Opportunity: problem/solution that teaches something to others

PROJECTS CONTEXT:
{projects_text}

RECENT ENGINEERING ACTIVITY (last 14 days):
{events_text}

Identify 3-7 story opportunities. Return a JSON array only — no markdown, no explanation:

[
  {{
    "project_slug": "agentforge",
    "title": "Short compelling title (max 80 chars)",
    "summary": "1-2 sentence description of what happened technically",
    "opportunity_type": "one of the 7 types above",
    "why_it_matters": "why a public audience would find this interesting (1 sentence)",
    "suggested_angle": "the specific hook or perspective to tell this story (1 sentence)",
    "importance_score": 75,
    "source_event_titles": ["exact event title from the list above"]
  }}
]

IMPORTANCE SCORING GUIDE:
- 10-20: minor fix or routine commit
- 25-45: meaningful progress, interesting decision, useful learning
- 50-69: major feature completion, notable technical challenge solved
- 70-85: product launch prep, milestone completed, significant release
- 86-100: company-defining moment, launch, major public milestone

Include all opportunities with importance_score >= 15. Even routine commits can have story value for a founder building in public.
Return ONLY the JSON array."""

    try:
        llm_cfg  = _build_llm_cfg(stored, gemini_key)
        response = _llm(prompt, llm_cfg, json_mode=False, timeout=180, max_tokens=2500)

        print(f"[authority] LLM raw response (first 300): {response[:300]}")
        match = re.search(r'\[.*\]', response, re.DOTALL)
        if not match:
            print(f"[authority] no JSON array in LLM response")
            return {"created": [], "count": 0, "debug": "LLM response had no JSON array"}

        opportunities_data = json.loads(match.group())
    except Exception as e:
        print(f"[authority] LLM error: {e}")
        return {"created": [], "count": 0, "debug": f"LLM error: {str(e)[:200]}"}

    created = []
    for opp in opportunities_data:
        if not opp.get("title") or not opp.get("project_slug"):
            continue
        score = int(opp.get("importance_score") or 0)
        if score < 15:
            continue

        # Match source event IDs by title
        src_titles = set(opp.get("source_event_titles") or [])
        source_ids = [e["id"] for e in new_events if e["title"] in src_titles]

        row = {
            "id":               str(uuid.uuid4()),
            "project_slug":     opp["project_slug"],
            "title":            opp["title"][:200],
            "summary":          opp.get("summary", "")[:500],
            "opportunity_type": opp.get("opportunity_type", "Build In Public"),
            "source_event_ids": source_ids,
            "importance_score": min(100, max(1, score)),
            "why_it_matters":   opp.get("why_it_matters", "")[:300],
            "suggested_angle":  opp.get("suggested_angle", "")[:300],
            "status":           "new",
            "created_at":       _now(),
            "updated_at":       _now(),
        }
        try:
            sb.table("story_opportunities").insert(row).execute()
            created.append(row)
        except Exception as e:
            print(f"[authority] insert error: {e}")

    debug = f"LLM returned {len(opportunities_data)} candidates; {len(created)} saved (scored >= 15)"
    print(f"[authority] scan complete: {debug}")
    return {"created": created, "count": len(created), "debug": debug}


# ── Weekly summary for ATLAS ──────────────────────────────────────────────────

def get_weekly_summary() -> list:
    sb = _sb()
    if not sb:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return (
        sb.table("story_opportunities").select("*")
          .gte("created_at", cutoff)
          .in_("status", ["new", "reviewed", "approved"])
          .order("importance_score", desc=True)
          .limit(5).execute().data or []
    )
