"""
Lumynor Weekly Intelligence Engine — ATLAS Phase 2
Pattern recognition covering 7-day periods.
NOT an activity log. Identifies what is happening to Lumynor.
"""
import re
import json
import uuid
from datetime import datetime, timezone, timedelta, date
from db import _sb


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _week_bounds() -> tuple[str, str]:
    """Monday–Sunday of the current/most recent complete week (previous 7 days)."""
    today = datetime.now(timezone.utc).date()
    # Always cover the last 7 days regardless of day-of-week
    week_end   = today - timedelta(days=1)
    week_start = week_end - timedelta(days=6)
    return week_start.isoformat(), week_end.isoformat()


# ── Scorecard (deterministic, no LLM) ────────────────────────────────────────

def _compute_scorecard(projects: list, events_7d: list, opportunities_7d: list) -> dict:
    healthy  = sum(1 for p in projects if p.get('health') == 'healthy')
    at_risk  = sum(1 for p in projects if p.get('health') == 'at_risk')
    blocked  = sum(1 for p in projects
                   if p.get('health') == 'blocked'
                   or (p.get('current_blocker') or '').strip())
    active   = sum(1 for p in projects if p.get('health') in ('healthy', 'at_risk'))
    new_opps = len(opportunities_7d)

    # Milestone-flavored events
    milestones = sum(
        1 for e in events_7d
        if any(k in (e.get('event_type', '') + ' ' + e.get('title', '')).lower()
               for k in ('milestone', 'release', 'launch', 'shipped', 'deployed'))
    )

    score = 50
    score += healthy  * 8
    score -= at_risk  * 4
    score -= blocked  * 15
    score += min(20, milestones * 8)
    score += min(15, new_opps  * 3)
    return {
        "projects_active":     active,
        "projects_healthy":    healthy,
        "projects_at_risk":    at_risk,
        "critical_blockers":   blocked,
        "completed_milestones":milestones,
        "new_opportunities":   new_opps,
        "weekly_score":        min(100, max(0, score)),
    }


# ── Intelligence Gathering ────────────────────────────────────────────────────

def _gather_intelligence() -> dict | None:
    sb = _sb()
    if not sb:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    from team import get_projects
    projects = get_projects()

    events_7d = (
        sb.table("activity_events").select("*")
          .gte("created_at", cutoff)
          .order("created_at", desc=True)
          .limit(200).execute().data or []
    )

    opportunities_7d = (
        sb.table("story_opportunities").select("*")
          .gte("created_at", cutoff)
          .not_.eq("status", "rejected")
          .order("importance_score", desc=True)
          .limit(20).execute().data or []
    )

    all_opportunities = (
        sb.table("story_opportunities").select("*")
          .not_.eq("status", "rejected")
          .order("importance_score", desc=True)
          .limit(10).execute().data or []
    )

    # Attention allocation
    attention_logs = (
        sb.table("attention_logs").select("project_slug, minutes")
          .gte("logged_at", cutoff).execute().data or []
    )
    totals: dict = {}
    grand = 0
    for log in attention_logs:
        slug = log["project_slug"]
        mins = log.get("minutes", 0)
        totals[slug] = totals.get(slug, 0) + mins
        grand += mins
    attention_pct = {
        slug: round(mins / grand * 100)
        for slug, mins in sorted(totals.items(), key=lambda x: -x[1])
    } if grand else {}

    # Events per project this week
    event_counts: dict = {}
    for e in events_7d:
        proj = e.get("project", "other")
        if proj != "other":
            event_counts[proj] = event_counts.get(proj, 0) + 1

    return {
        "projects":          projects,
        "events_7d":         events_7d,
        "event_counts":      event_counts,
        "opportunities_7d":  opportunities_7d,
        "all_opportunities": all_opportunities,
        "attention_pct":     attention_pct,
        "scorecard":         _compute_scorecard(projects, events_7d, opportunities_7d),
    }


# ── Report Generation ─────────────────────────────────────────────────────────

def generate_weekly_report() -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")

    try:
        from auto_blogger import _build_llm_cfg, _llm
        from db import get_settings
    except ImportError:
        raise RuntimeError("auto_blogger not available")

    intel = _gather_intelligence()
    if not intel:
        raise RuntimeError("Could not gather intelligence")

    stored     = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")

    projects        = intel["projects"]
    event_counts    = intel["event_counts"]
    scorecard       = intel["scorecard"]
    attention_pct   = intel["attention_pct"]
    all_opps        = intel["all_opportunities"]
    week_start, week_end = _week_bounds()

    # ── Build LLM context ─────────────────────────────────────────────────────
    projects_ctx = "\n".join(
        f"- {p.get('name', p['slug'])} (slug={p['slug']}) | health={p.get('health','?')} "
        f"| priority={p.get('priority','?')} | status={p.get('status','?')} "
        f"| blocker={p.get('current_blocker') or 'none'} "
        f"| milestone={p.get('next_milestone') or 'none'} "
        f"| events_this_week={event_counts.get(p['slug'], 0)}"
        for p in projects
    ) or "No projects."

    opps_ctx = "\n".join(
        f"- [{o.get('project_slug')}] {o.get('title')} | score={o.get('importance_score')} | type={o.get('opportunity_type')} | status={o.get('status')}"
        for o in all_opps[:8]
    ) or "No story opportunities identified yet."

    attention_ctx = "\n".join(
        f"- {slug}: {pct}%"
        for slug, pct in list(attention_pct.items())[:6]
    ) or "No attention data logged this week."

    prompt = f"""You are ATLAS, the intelligence system for Lumynor Systems.
Founder: Danish. Week: {week_start} to {week_end}.
Weekly scorecard: Active={scorecard['projects_active']}, Healthy={scorecard['projects_healthy']}, At Risk={scorecard['projects_at_risk']}, Blocked={scorecard['critical_blockers']}, New Opportunities={scorecard['new_opportunities']}, Score={scorecard['weekly_score']}/100

PORTFOLIO:
{projects_ctx}

STORY OPPORTUNITIES (Authority OS):
{opps_ctx}

FOUNDER ATTENTION THIS WEEK:
{attention_ctx}

Generate a weekly intelligence report. Return ONLY this JSON — no markdown, no explanation:
{{
  "executive_summary": {{
    "overall_momentum": "High|Medium|Low",
    "summary_text": "2-3 sentences on what happened to Lumynor this week. Pattern-level, not event list.",
    "major_achievement": "Single most important thing that happened (1 sentence or 'No major achievements this week')"
  }},
  "product_health": [
    {{
      "project_slug": "slug",
      "project_name": "Name",
      "health": "healthy|at_risk|blocked|inactive",
      "momentum": "High|Medium|Low|Stalled",
      "momentum_change": "increased|stable|decreased|stalled",
      "major_activity": "What happened this project this week (1 sentence, be specific)",
      "blocker": "Current blocker text or empty string"
    }}
  ],
  "momentum_trends": [
    {{
      "project_slug": "slug",
      "project_name": "Name",
      "trend": "increased|stable|decreased|stalled",
      "reason": "Evidence-based reason (1 sentence)"
    }}
  ],
  "strategic_blockers": [
    {{
      "blocker_name": "Short descriptive name",
      "affected_projects": ["slug1"],
      "estimated_impact": "Business impact (1 sentence)",
      "suggested_resolution": "Specific next action (1 sentence)"
    }}
  ],
  "opportunity_analysis": {{
    "summary": "Pattern-level insight on story/authority opportunities (1-2 sentences)",
    "top_opportunity_titles": ["title1", "title2"],
    "projects_generating_value": ["slug1"]
  }},
  "recommendations": {{
    "focus_more": [{{"project": "Project Name", "reason": "Specific reason (1 sentence)"}}],
    "maintain": [{{"project": "Project Name", "reason": "Specific reason (1 sentence)"}}],
    "ignore": [{{"project": "Project Name", "reason": "Specific reason (1 sentence)"}}]
  }}
}}

Rules:
- product_health MUST include ALL {len(projects)} projects
- momentum_change: use events_this_week — >3 = increased, 0 = stalled, else stable
- recommendations.ignore is MANDATORY — name at least one project and explain why
- No generic advice. Every sentence must reference actual project data
- Be direct and honest — if a project had a bad week, say so"""

    llm_cfg  = _build_llm_cfg(stored, gemini_key)
    llm_data: dict = {}
    try:
        response = _llm(prompt, llm_cfg, json_mode=False, timeout=180, max_tokens=2500)
        print(f"[weekly_intel] LLM response length: {len(response)}")
        match = re.search(r'\{.*\}', response, re.DOTALL)
        if match:
            llm_data = json.loads(match.group())
        else:
            print("[weekly_intel] no JSON object in response")
            llm_data = {"error": "LLM response had no JSON object"}
    except Exception as e:
        print(f"[weekly_intel] LLM error: {e}")
        llm_data = {"error": str(e)[:200]}

    # Merge deterministic data
    llm_data["scorecard"]         = scorecard
    llm_data["attention_analysis"] = {"percentages": attention_pct}

    row = {
        "id":          str(uuid.uuid4()),
        "week_start":  week_start,
        "week_end":    week_end,
        "weekly_score":scorecard["weekly_score"],
        "raw_json":    llm_data,
        "created_at":  _now(),
    }
    try:
        sb.table("weekly_reports").insert(row).execute()
    except Exception as e:
        print(f"[weekly_intel] insert error: {e}")

    return row


# ── Read ──────────────────────────────────────────────────────────────────────

def get_latest_report() -> dict | None:
    sb = _sb()
    if not sb:
        return None
    r = (
        sb.table("weekly_reports").select("*")
          .order("created_at", desc=True).limit(1).execute()
    )
    return r.data[0] if r.data else None


def get_all_reports(limit: int = 12) -> list:
    sb = _sb()
    if not sb:
        return []
    return (
        sb.table("weekly_reports")
          .select("id, week_start, week_end, weekly_score, created_at")
          .order("created_at", desc=True).limit(limit).execute().data or []
    )


def should_auto_generate() -> bool:
    """True if today is Monday and no report exists for this week."""
    if datetime.now(timezone.utc).weekday() != 0:  # 0 = Monday
        return False
    week_start, _ = _week_bounds()
    latest = get_latest_report()
    if not latest:
        return True
    return latest.get("week_start") != week_start
