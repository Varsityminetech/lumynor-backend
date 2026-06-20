"""
Lumynor Activity OS — event storage, retrieval, and ATLAS intelligence.
"""
import os
import uuid
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from db import _sb

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
    name = (repo_name or "").lower()
    if "backend" in name:
        return "other"        # lumynor-backend → maps as infra/other
    if "website" in name or "lumynor" in name:
        return "lumynor_website"
    if "agentforge" in name:
        return "agentforge"
    if "linkforge" in name:
        return "linkforge"
    if "district" in name:
        return "district21"
    if "mission" in name:
        return "mission_control"
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

    prompt = f"""You are ATLAS, executive intelligence system for Lumynor Systems.

Analyze these {len(events)} activity events from the last 24 hours and write a concise executive briefing for the founder (Danish).

EVENTS:
{context}

Write a structured briefing with EXACTLY these 5 sections. Use bullet points. Be direct — executive tone, zero fluff.

## What Happened Today
(2-4 highest-signal points only)

## Completed
(what was finished)

## Blocked
(what is stuck; write "Nothing blocked." if none)

## Needs Your Review
(decisions or items requiring Danish's direct attention; write "None." if clear)

## Recommended Focus Tomorrow
(1-3 prioritized actions for Danish)"""

    try:
        ollama_key = os.getenv("OLLAMA_API_KEY", "")
        if ollama_key:
            from auto_blogger import _ollama_generate
            ollama_host = os.getenv("OLLAMA_HOST", "https://ollama.com")
            writing_models = [m.strip() for m in (os.getenv("OLLAMA_WRITING_MODELS", "") or "").split(",") if m.strip()] \
                or ["cogito-2.1:671b", "qwen3-coder:480b", "nemotron-3-super", "gpt-oss:120b"]
            cfg = {
                "provider": "ollama_cloud",
                "writing_models": writing_models,
                "ollama_key": ollama_key,
                "ollama_host": ollama_host,
            }
            last_err = None
            briefing = None
            for model in writing_models:
                try:
                    briefing = _ollama_generate(prompt, cfg, json_mode=False, timeout=120, max_tokens=1024, model=model)
                    break
                except Exception as e:
                    last_err = e
            if briefing is None:
                raise last_err or RuntimeError("No Ollama model responded")
        else:
            gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
            import requests as _req, json as _json
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            resp = _req.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
            resp.raise_for_status()
            briefing = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        briefing = f"ATLAS unavailable: {e}"

    return {"generated_at": _now(), "event_count": len(events), "briefing": briefing}
