"""
Lumynor Activity OS — event storage, retrieval, and ATLAS intelligence.
"""
import os
import json
import uuid
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from db import _sb

_PROJECT_STATUS_FILE = "/data/project_statuses.json"
_DEFAULT_PROJECT_STATUSES = {
    "agentforge":      {"status": "Launch Ready", "note": "Awaiting Payment Integration"},
    "linkforge":       {"status": "Development",  "note": ""},
    "district21":      {"status": "Development",  "note": ""},
    "lumynor_website": {"status": "Production",   "note": ""},
    "mission_control": {"status": "Planning",     "note": ""},
    "other":           {"status": "Development",  "note": ""},
}

# ── Constants ─────────────────────────────────────────────────────────────────

SOURCES     = ('github', 'claude', 'codex', 'website', 'manual', 'system')
PROJECTS    = ('agentforge', 'linkforge', 'district21', 'mission_control', 'lumynor_website', 'other')
EVENT_TYPES = ('feature_completed', 'bug_fix', 'deployment', 'decision', 'lead', 'blocker', 'review_needed', 'task_update', 'report', 'milestone')
STATUSES    = ('new', 'in_progress', 'completed', 'blocked', 'review_needed', 'failed')
PRIORITIES  = ('low', 'normal', 'important', 'critical')

GITHUB_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_start() -> str:
    n = datetime.now(timezone.utc)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _24h_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_event(data: dict) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("Database not configured")
    row = {
        "id":            data.get("id") or str(uuid.uuid4()),
        "source":        data.get("source", "manual"),
        "project":       data.get("project", "other"),
        "event_type":    data.get("event_type", "task_update"),
        "title":         data.get("title", ""),
        "summary":       data.get("summary", ""),
        "status":        data.get("status", "new"),
        "priority":      data.get("priority", "normal"),
        "actor":         data.get("actor", ""),
        "related_url":   data.get("related_url", ""),
        "metadata_json": data.get("metadata_json", {}),
        "created_at":    data.get("created_at") or _now(),
        "updated_at":    _now(),
    }
    sb.table("activity_events").insert(row).execute()
    return row


def get_events(limit: int = 200, project: str = None, status: str = None, priority: str = None) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("activity_events").select("*").order("created_at", desc=True)
    if project:
        q = q.eq("project", project)
    if status:
        q = q.eq("status", status)
    if priority:
        q = q.eq("priority", priority)
    return q.limit(limit).execute().data or []


def get_today_events() -> list:
    sb = _sb()
    if not sb:
        return []
    return (
        sb.table("activity_events").select("*")
          .gte("created_at", _today_start())
          .order("created_at", desc=True)
          .execute().data or []
    )


def get_events_by_project() -> dict:
    rows = get_events(limit=500)
    groups: dict = {}
    for r in rows:
        p = r.get("project", "other")
        groups.setdefault(p, []).append(r)
    return groups


def get_critical_events() -> list:
    sb = _sb()
    if not sb:
        return []
    crit    = sb.table("activity_events").select("*").eq("priority", "critical").order("created_at", desc=True).limit(50).execute().data or []
    blocked = sb.table("activity_events").select("*").in_("status", ["blocked", "failed"]).order("created_at", desc=True).limit(50).execute().data or []
    seen, result = set(), []
    for r in crit + blocked:
        if r["id"] not in seen:
            seen.add(r["id"])
            result.append(r)
    return sorted(result, key=lambda x: x["created_at"], reverse=True)


def update_event_status(event_id: str, status: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    sb.table("activity_events").update({"status": status, "updated_at": _now()}).eq("id", event_id).execute()
    res = sb.table("activity_events").select("*").eq("id", event_id).limit(1).execute()
    return res.data[0] if res.data else None


# ── GitHub webhook ─────────────────────────────────────────────────────────────

def verify_github_signature(body: bytes, signature: str) -> bool:
    if not GITHUB_SECRET:
        return True  # dev mode: no secret configured
    expected = "sha256=" + hmac.new(GITHUB_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _detect_project(repo_name: str) -> str:
    name = (repo_name or "").lower().replace("-", "").replace("_", "")
    if "agentforge" in name:
        return "agentforge"
    if "linkforge" in name:
        return "linkforge"
    if "district21" in name or "district" in name:
        return "district21"
    if "missioncontrol" in name or "mission" in name:
        return "mission_control"
    if "backend" in name:
        return "other"
    if "website" in name or "lumynor" in name:
        return "lumynor_website"
    return "other"


def parse_github_event(gh_event: str, payload: dict) -> dict | None:
    """Convert a raw GitHub webhook payload into a standardised activity event."""
    repo     = (payload.get("repository") or {}).get("name", "unknown")
    project  = _detect_project(repo)
    actor    = (payload.get("sender") or {}).get("login", "github")
    repo_url = (payload.get("repository") or {}).get("html_url", "")

    if gh_event == "push":
        commits = payload.get("commits", [])
        if not commits:
            return None
        ref  = payload.get("ref", "").replace("refs/heads/", "")
        msgs = [c.get("message", "").split("\n")[0] for c in commits[:3]]
        return {
            "source": "github", "project": project, "event_type": "task_update",
            "title":  f"[{repo}] {len(commits)} commit(s) pushed to {ref}",
            "summary": " · ".join(msgs),
            "status": "completed", "priority": "normal", "actor": actor,
            "related_url": payload.get("compare", repo_url),
            "metadata_json": {"ref": ref, "commit_count": len(commits)},
        }

    if gh_event == "pull_request":
        pr     = payload.get("pull_request", {})
        action = payload.get("action", "")
        if action not in ("opened", "closed", "ready_for_review"):
            return None
        merged     = pr.get("merged", False)
        event_type = "feature_completed" if merged else ("review_needed" if action == "ready_for_review" else "task_update")
        status     = "completed" if merged else ("review_needed" if action == "ready_for_review" else "in_progress")
        return {
            "source": "github", "project": project, "event_type": event_type,
            "title":  f"[{repo}] PR {'merged' if merged else action}: {pr.get('title', '')}",
            "summary": pr.get("body", "")[:300] or f"PR #{pr.get('number')} by {actor}",
            "status": status, "priority": "important" if merged else "normal", "actor": actor,
            "related_url": pr.get("html_url", ""),
            "metadata_json": {"pr_number": pr.get("number"), "merged": merged},
        }

    if gh_event == "release":
        if payload.get("action") != "published":
            return None
        rel = payload.get("release", {})
        return {
            "source": "github", "project": project, "event_type": "milestone",
            "title":  f"[{repo}] Release: {rel.get('name') or rel.get('tag_name', '')}",
            "summary": (rel.get("body") or "")[:300],
            "status": "completed", "priority": "important", "actor": actor,
            "related_url": rel.get("html_url", ""),
            "metadata_json": {"tag": rel.get("tag_name")},
        }

    if gh_event == "deployment_status":
        ds    = payload.get("deployment_status", {})
        state = ds.get("state", "")
        status_map   = {"success": "completed", "failure": "failed", "error": "failed", "pending": "in_progress"}
        priority_map = {"failure": "important", "error": "critical"}
        return {
            "source": "github", "project": project, "event_type": "deployment",
            "title":  f"[{repo}] Deployment {state}",
            "summary": ds.get("description") or f"Env: {(payload.get('deployment') or {}).get('environment', 'production')}",
            "status": status_map.get(state, "in_progress"),
            "priority": priority_map.get(state, "normal"), "actor": actor,
            "related_url": ds.get("target_url") or repo_url,
            "metadata_json": {"state": state, "environment": (payload.get("deployment") or {}).get("environment")},
        }

    return None


# ── Decision Registry ─────────────────────────────────────────────────────────

def get_decisions(limit: int = 100) -> list:
    sb = _sb()
    if not sb:
        return []
    return (
        sb.table("activity_events").select("*")
          .eq("event_type", "decision")
          .order("created_at", desc=True)
          .limit(limit).execute().data or []
    )


# ── Project Status Layer ───────────────────────────────────────────────────────

def get_project_statuses() -> dict:
    try:
        with open(_PROJECT_STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return _DEFAULT_PROJECT_STATUSES.copy()


def update_project_status(project: str, status: str, note: str = "") -> dict:
    statuses = get_project_statuses()
    statuses[project] = {"status": status, "note": note}
    try:
        os.makedirs(os.path.dirname(_PROJECT_STATUS_FILE), exist_ok=True)
        with open(_PROJECT_STATUS_FILE, "w") as f:
            json.dump(statuses, f)
    except Exception as e:
        print(f"[activity] Could not persist project statuses: {e}")
    return statuses


# ── Project Momentum Engine ────────────────────────────────────────────────────

def get_project_momentum() -> dict:
    """Score each project High / Medium / Low / Blocked from the last 7 days of events."""
    all_events = get_events(limit=500)
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()

    result = {}
    for project in PROJECTS:
        proj  = [e for e in all_events if e.get("project") == project]
        if not proj:
            continue
        recent   = [e for e in proj if (e.get("created_at") or "") >= week_ago]
        blockers = [e for e in recent if e.get("status") in ("blocked", "failed")]
        completed = [e for e in recent if e.get("status") == "completed"]
        milestones = [e for e in recent if e.get("event_type") in ("milestone", "feature_completed")]

        last_iso = proj[0].get("created_at", "")
        try:
            last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            days_since = max(0, (now - last_dt).days)
        except Exception:
            days_since = 999

        vol = len(recent)

        if blockers and vol == 0:
            score = "blocked"
            reason = f"{len(blockers)} unresolved blocker(s), no new activity."
        elif days_since > 7:
            score = "low"
            reason = f"No activity in {days_since} day(s)."
        elif vol >= 5 and (completed or milestones):
            score = "high"
            reason = f"{vol} events this week, {len(completed)} completed, {len(milestones)} milestone(s)."
        elif vol >= 2:
            score = "medium"
            reason = f"{vol} events this week."
        elif blockers:
            score = "blocked"
            reason = f"{len(blockers)} active blocker(s)."
        else:
            score = "low"
            reason = f"Light activity ({vol} event(s) this week)."

        result[project] = {
            "score":           score,
            "reason":          reason,
            "recent_count":    vol,
            "completed":       len(completed),
            "blockers":        len(blockers),
            "days_since_update": days_since,
        }
    return result


# ── Executive Snapshot ─────────────────────────────────────────────────────────

def get_executive_snapshot() -> dict:
    """One-glance company status: active projects, blockers, critical items, big win today."""
    today_events = get_today_events()
    all_events   = get_events(limit=200)
    momentum     = get_project_momentum()

    projects_active  = sum(1 for v in momentum.values() if v["score"] != "blocked")
    projects_blocked = sum(1 for v in momentum.values() if v["score"] == "blocked")
    critical_issues  = sum(1 for e in all_events[:100] if e.get("priority") == "critical")
    review_items     = sum(1 for e in all_events[:100] if e.get("status") == "review_needed")

    major_achievement = None
    for e in today_events:
        if e.get("event_type") in ("milestone", "feature_completed") or e.get("status") == "completed":
            major_achievement = e.get("title", "")
            break

    decisions_today = [e for e in today_events if e.get("event_type") == "decision"]

    return {
        "projects_active":   projects_active,
        "projects_blocked":  projects_blocked,
        "critical_issues":   critical_issues,
        "review_items":      review_items,
        "events_today":      len(today_events),
        "decisions_today":   len(decisions_today),
        "major_achievement": major_achievement,
        "momentum":          momentum,
    }


# ── ATLAS Daily Intelligence ──────────────────────────────────────────────────

def generate_atlas_summary() -> dict:
    """Generate an executive daily briefing from the last 24 h of events."""
    sb = _sb()
    events = (
        sb.table("activity_events").select("*")
          .gte("created_at", _24h_ago())
          .order("created_at", desc=True)
          .limit(200).execute().data or []
    ) if sb else []

    if not events:
        return {
            "generated_at": _now(),
            "event_count":  0,
            "briefing": "No activity recorded in the last 24 hours.",
        }

    lines = [
        f"[{e['priority'].upper()}][{e['source']}][{e['project']}][{e['event_type']}] "
        f"{e['title']} — {e.get('summary') or 'no details'} (status: {e['status']})"
        for e in events
    ]
    context = "\n".join(lines)

    momentum     = get_project_momentum()
    mom_lines    = "\n".join(
        f"  {p.replace('_',' ').title()}: {v['score'].upper()} — {v['reason']}"
        for p, v in momentum.items()
    )

    prompt = f"""You are ATLAS, executive intelligence system for Lumynor Systems.

Analyze these {len(events)} activity events from the last 24 hours and write a concise executive briefing for the founder (Danish).

EVENTS:
{context}

PROJECT MOMENTUM (calculated):
{mom_lines}

Write a structured briefing with EXACTLY these 6 sections. Use bullet points. Be direct — executive tone, zero fluff.

## Major Achievements
(What was completed or shipped — only meaningful wins. Write "None today." if nothing significant.)

## Key Decisions
(Strategic or technical decisions made. Write "None recorded." if none.)

## Blockers
(What is stuck and needs action — who owns it, what is needed. Write "Nothing blocked." if none.)

## Momentum Changes
(Which projects accelerated, stalled, or shifted direction based on the data above.)

## Needs Your Review
(Items requiring Danish's direct attention or approval. Write "None." if clear.)

## Recommended Focus Tomorrow
(1-3 prioritized actions for Danish — most impactful first.)"""

    try:
        from auto_blogger import _build_llm_cfg, _llm
        llm_cfg  = _build_llm_cfg({}, "")
        briefing = _llm(prompt, llm_cfg, json_mode=False, timeout=120, max_tokens=1024)
    except Exception as e:
        briefing = f"ATLAS unavailable: {e}"

    return {"generated_at": _now(), "event_count": len(events), "briefing": briefing}
