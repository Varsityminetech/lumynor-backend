"""
Lumynor Strategy OS — Founder Prioritization Engine
Helps Danish decide where to focus time and attention.
NOT analytics. NOT dashboards. Actionable recommendations only.
"""
import re
import json
import uuid
from datetime import datetime, timezone, timedelta
from db import _sb

_IMPORTANCE_PTS = {
    'Revenue Driver':   35,
    'Core Product':     30,
    'Growth Engine':    25,
    'Customer Facing':  20,
    'Internal Tool':    10,
    'Experiment':        5,
}
_PRIORITY_PTS = {
    'Critical': 20,
    'High':     15,
    'Medium':   10,
    'Low':       5,
}
_HEALTH_ACTIVITY_PTS = {'healthy': 15, 'at_risk': 6, 'blocked': 0, 'inactive': -10}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Focus Score ───────────────────────────────────────────────────────────────

def compute_focus_score(project: dict, authority_opps: list = None) -> int:
    score = 0

    # Strategic importance (0–35)
    score += _IMPORTANCE_PTS.get(project.get('strategic_importance', ''), 10)

    # Priority (0–20)
    score += _PRIORITY_PTS.get(project.get('priority', 'Medium'), 10)

    # Recent activity (0–20)
    last_act = project.get('last_activity_at')
    if last_act:
        try:
            ts = datetime.fromisoformat(last_act.replace('Z', '+00:00'))
            days = (datetime.now(timezone.utc) - ts).days
            if days <= 1:    score += 20
            elif days <= 3:  score += 16
            elif days <= 7:  score += 12
            elif days <= 14: score += 6
        except Exception:
            pass

    # Blocker penalty (-20)
    has_blocker = bool((project.get('current_blocker') or '').strip())
    if has_blocker:
        score = max(0, score - 20)

    # Health (−10 to +15)
    score += _HEALTH_ACTIVITY_PTS.get(project.get('health', 'healthy'), 5)

    # Authority opportunity bonus (0–10)
    if authority_opps:
        slug = project.get('slug', '')
        active = [o for o in authority_opps
                  if o.get('project_slug') == slug
                  and o.get('status') in ('new', 'reviewed', 'approved')]
        score += min(10, len(active) * 4)

    return min(100, max(0, score))


def _build_recommendation(project: dict, score: int, authority_opps: list = None) -> tuple:
    """Returns (reason, evidence[], expected_impact) from project data. No LLM."""
    slug        = project.get('slug', '')
    name        = project.get('name', slug)
    health      = project.get('health', 'healthy')
    blocker     = (project.get('current_blocker') or '').strip()
    milestone   = (project.get('next_milestone') or '').strip()
    priority    = project.get('priority', 'Medium')
    status      = project.get('status', 'Development')

    evidence = []

    # Recency
    last_act = project.get('last_activity_at')
    if last_act:
        try:
            ts = datetime.fromisoformat(last_act.replace('Z', '+00:00'))
            days = (datetime.now(timezone.utc) - ts).days
            if days == 0:    evidence.append("Active today")
            elif days == 1:  evidence.append("Active yesterday")
            elif days <= 7:  evidence.append(f"Active {days} days ago")
            elif days <= 14: evidence.append(f"Last active {days} days ago — at risk of stalling")
            else:            evidence.append(f"No activity for {days} days")
        except Exception:
            pass

    if blocker:
        evidence.append(f"Blocker: {blocker[:100]}")
    if milestone:
        evidence.append(f"Next milestone: {milestone[:100]}")
    if priority in ('Critical', 'High'):
        evidence.append(f"{priority} priority")
    if health == 'healthy':
        evidence.append("Healthy momentum")
    elif health == 'at_risk':
        evidence.append("Momentum slowing")

    if authority_opps:
        active_opps = [o for o in authority_opps
                       if o.get('project_slug') == slug
                       and o.get('status') in ('new', 'reviewed', 'approved')]
        if active_opps:
            n = len(active_opps)
            evidence.append(f"{n} story opportunit{'y' if n == 1 else 'ies'} identified")

    # Reason
    if blocker and score >= 55:
        reason = f"High-value project blocked. Unblocking '{blocker[:80]}' is the single most impactful action."
        impact = "Removing this blocker directly accelerates the most important project."
    elif blocker:
        reason = f"Blocked by '{blocker[:80]}'. Resolve this before any other work on {name}."
        impact = "Unblocking enables forward progress that's currently impossible."
    elif health == 'inactive':
        reason = f"{name} has had no activity in over 14 days. Decide: resume or defer."
        impact = "A clear decision prevents indefinite drift and wasted mental overhead."
    elif health == 'at_risk' and score >= 40:
        reason = f"Momentum on {name} is slowing. A focused session now prevents a longer restart later."
        impact = "Re-engaging now costs one hour; re-starting from zero costs a week."
    elif score >= 70:
        ms_str = f" Approaching: {milestone}." if milestone else " No blockers."
        reason = f"Highest strategic value in the portfolio right now.{ms_str} Keep pushing."
        impact = "Concentrated focus accelerates the project closest to delivering real value."
    elif score >= 50:
        ms_str = f" Next: {milestone}." if milestone else ""
        reason = f"Steady progress on {name}.{ms_str} Maintain momentum."
        impact = "Consistent attention delivers the next milestone on schedule."
    else:
        reason = f"{name} is lower priority right now. Other projects need founder attention more."
        impact = "Deprioritizing this frees cognitive bandwidth for higher-value work."

    return reason, evidence, impact


# ── Daily Focus ───────────────────────────────────────────────────────────────

def get_daily_focus(limit: int = 6) -> list:
    sb = _sb()
    if not sb:
        return []

    from team import get_projects
    projects = get_projects()

    authority_opps = (
        sb.table("story_opportunities").select("project_slug, status, importance_score")
          .not_.eq("status", "rejected").execute().data or []
    )

    scored = []
    for p in projects:
        if p.get('status') in ('Archived', 'Cancelled'):
            continue
        score = compute_focus_score(p, authority_opps)
        reason, evidence, impact = _build_recommendation(p, score, authority_opps)
        scored.append({
            "slug":            p["slug"],
            "name":            p.get("name", p["slug"]),
            "score":           score,
            "health":          p.get("health", "healthy"),
            "priority":        p.get("priority", "Medium"),
            "status":          p.get("status", "Development"),
            "current_blocker": (p.get("current_blocker") or "").strip(),
            "next_milestone":  (p.get("next_milestone") or "").strip(),
            "reason":          reason,
            "evidence":        evidence,
            "expected_impact": impact,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    for i, item in enumerate(scored):
        item["rank"] = i + 1

    return scored[:limit]


# ── Opportunity vs Risk Matrix ────────────────────────────────────────────────

def _opportunity_score(project: dict, authority_opps: list) -> int:
    score = 0
    score += _IMPORTANCE_PTS.get(project.get('strategic_importance', ''), 10)
    score += _PRIORITY_PTS.get(project.get('priority', 'Medium'), 10)
    if project.get('status') in ('Beta', 'Launch', 'Live'):
        score += 15
    if authority_opps:
        slug = project.get('slug', '')
        high = [o for o in authority_opps
                if o.get('project_slug') == slug and (o.get('importance_score') or 0) >= 50]
        score += min(20, len(high) * 7)
    return min(100, max(0, score))


def _risk_score(project: dict) -> int:
    risk = 0
    if (project.get('current_blocker') or '').strip():
        risk += 35
    risk += {'blocked': 30, 'inactive': 25, 'at_risk': 15, 'healthy': 0}.get(project.get('health', 'healthy'), 10)
    last_act = project.get('last_activity_at')
    if last_act:
        try:
            ts = datetime.fromisoformat(last_act.replace('Z', '+00:00'))
            days = (datetime.now(timezone.utc) - ts).days
            if days > 30:    risk += 20
            elif days > 14:  risk += 12
            elif days > 7:   risk += 6
        except Exception:
            pass
    return min(100, max(0, risk))


def get_matrix() -> list:
    sb = _sb()
    if not sb:
        return []

    from team import get_projects
    projects = get_projects()
    authority_opps = (
        sb.table("story_opportunities").select("project_slug, status, importance_score")
          .not_.eq("status", "rejected").execute().data or []
    )

    result = []
    for p in projects:
        if p.get('status') in ('Archived', 'Cancelled'):
            continue
        opp  = _opportunity_score(p, authority_opps)
        risk = _risk_score(p)
        quadrant = (
            'prioritize' if opp >= 50 and risk <  50 else
            'bold_bet'   if opp >= 50 and risk >= 50 else
            'maintain'   if opp <  50 and risk <  50 else
            'reconsider'
        )
        result.append({
            "slug":     p["slug"],
            "name":     p.get("name", p["slug"]),
            "opp":      opp,
            "risk":     risk,
            "quadrant": quadrant,
            "health":   p.get("health", "healthy"),
        })
    return result


# ── Attention Allocation ──────────────────────────────────────────────────────

def log_attention(project_slug: str, minutes: int) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    row = {
        "id":           str(uuid.uuid4()),
        "project_slug": project_slug,
        "minutes":      minutes,
        "logged_at":    _now(),
    }
    sb.table("attention_logs").insert(row).execute()
    return row


def get_attention_allocation(days: int = 7) -> list:
    sb = _sb()
    if not sb:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    logs = (
        sb.table("attention_logs").select("project_slug, minutes")
          .gte("logged_at", cutoff).execute().data or []
    )
    totals: dict = {}
    grand = 0
    for log in logs:
        slug = log["project_slug"]
        mins = log.get("minutes", 0)
        totals[slug] = totals.get(slug, 0) + mins
        grand += mins
    return [
        {
            "project_slug": slug,
            "minutes":      mins,
            "percentage":   round(mins / grand * 100) if grand else 0,
        }
        for slug, mins in sorted(totals.items(), key=lambda x: -x[1])
    ]


# ── Strategic Blockers ────────────────────────────────────────────────────────

def get_strategic_blockers() -> list:
    sb = _sb()
    if not sb:
        return []
    from team import get_projects
    projects = get_projects()
    blockers = []
    for p in projects:
        blocker = (p.get('current_blocker') or '').strip()
        if not blocker:
            continue
        status = p.get('status', '')
        health = p.get('health', 'healthy')
        impact = (
            "Blocking launch"     if status in ('Beta', 'Launch', 'Live') else
            "Stalling development" if health in ('blocked', 'at_risk') else
            "Limiting progress"
        )
        blockers.append({
            "project_slug": p["slug"],
            "project_name": p.get("name", p["slug"]),
            "blocker":      blocker,
            "impact":       impact,
            "priority":     p.get("priority", "Medium"),
            "health":       health,
        })
    order = {'Critical': 0, 'High': 1, 'Medium': 2, 'Low': 3}
    blockers.sort(key=lambda x: order.get(x['priority'], 4))
    return blockers


# ── Weekly Strategic Brief ────────────────────────────────────────────────────

def generate_weekly_brief() -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")

    try:
        from auto_blogger import _build_llm_cfg, _llm
        from db import get_settings
    except ImportError:
        raise RuntimeError("auto_blogger not available")

    stored     = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")

    focus    = get_daily_focus(limit=10)
    blockers = get_strategic_blockers()

    projects_text = "\n".join(
        f"{f['rank']}. {f['name']} | score={f['score']} | health={f['health']} "
        f"| priority={f['priority']} | blocker={f['current_blocker'] or 'none'} "
        f"| milestone={f['next_milestone'] or 'none'}"
        for f in focus
    ) or "No projects."

    blockers_text = "\n".join(
        f"- [{b['project_name']}] {b['blocker']} ({b['impact']})"
        for b in blockers
    ) or "No active blockers."

    prompt = f"""You are the Strategy OS for Lumynor Systems, helping founder Danish prioritize his time.
He is a solo/small-team founder building AI SaaS products.

PROJECT STATUS (by focus priority):
{projects_text}

ACTIVE BLOCKERS:
{blockers_text}

Generate a weekly strategic brief. Return ONLY this JSON object — no markdown, no explanation:
{{
  "biggest_progress": "1-2 sentences on what moved forward most this week. Be specific about the project.",
  "biggest_risks": "1-2 sentences on the most critical risks. Be specific.",
  "most_important_opportunity": "The single best opportunity right now and why (1-2 sentences).",
  "recommended_focus": "Specific actionable focus for next week. Name the project and the exact action (2-3 sentences).",
  "projects_to_ignore": "Which projects should Danish NOT spend time on next week and why. Be direct. (1-2 sentences)"
}}

Rules: Direct, specific, actionable. No generic startup advice. Every statement must reference actual project data above."""

    llm_cfg = _build_llm_cfg(stored, gemini_key)
    brief   = {}
    try:
        response = _llm(prompt, llm_cfg, json_mode=False, timeout=120, max_tokens=800)
        match    = re.search(r'\{.*\}', response, re.DOTALL)
        if match:
            brief = json.loads(match.group())
    except Exception as e:
        print(f"[strategy] weekly brief LLM error: {e}")
        brief = {"error": str(e)[:200]}

    row = {
        "id":                         str(uuid.uuid4()),
        "biggest_progress":           brief.get("biggest_progress", ""),
        "biggest_risks":              brief.get("biggest_risks", ""),
        "most_important_opportunity": brief.get("most_important_opportunity", ""),
        "recommended_focus":          brief.get("recommended_focus", ""),
        "projects_to_ignore":         brief.get("projects_to_ignore", ""),
        "raw_json":                   brief,
        "created_at":                 _now(),
    }
    try:
        sb.table("strategy_briefs").insert(row).execute()
    except Exception as e:
        print(f"[strategy] brief insert error: {e}")
    return row


def get_latest_brief() -> dict | None:
    sb = _sb()
    if not sb:
        return None
    r = (
        sb.table("strategy_briefs").select("*")
          .order("created_at", desc=True).limit(1).execute()
    )
    return r.data[0] if r.data else None
