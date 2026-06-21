"""
Lumynor Activity OS — Team Collaboration + Project Management Layer
Project ownership, membership, health engine, and project-scoped chat.
"""
import uuid
from datetime import datetime, timezone
from db import _sb

TEAM_MEMBERS = ['Danish', 'Mustafa']

# Fields only humans may set — GitHub never overwrites these once a human touches them
HUMAN_OWNED_FIELDS = {
    'name', 'owner_name', 'status', 'description', 'strategic_notes',
    'priority', 'strategic_importance', 'current_blocker', 'next_milestone',
    'target_launch_date', 'notes', 'note',
}

# Fields GitHub may update freely (never human-locked)
GITHUB_FIELDS = {
    'primary_repo', 'last_activity_at', 'last_commit_at',
    'last_pr_at', 'last_deploy_at', 'recent_activity_count',
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Health Engine ─────────────────────────────────────────────────────────────

def compute_health(project: dict) -> str:
    blocker = (project.get('current_blocker') or '').strip()
    last_act = project.get('last_activity_at')
    days_since = 999
    if last_act:
        try:
            ts = datetime.fromisoformat(last_act.replace('Z', '+00:00'))
            days_since = (datetime.now(timezone.utc) - ts).days
        except Exception:
            pass
    if blocker:
        return 'blocked'
    if days_since >= 14:
        return 'inactive'
    if days_since >= 7:
        return 'at_risk'
    return 'healthy'


# ── Projects ──────────────────────────────────────────────────────────────────

def get_projects() -> list:
    sb = _sb()
    if not sb:
        return []
    projects = sb.table("projects").select("*").order("name").execute().data or []
    for p in projects:
        p["members"] = sb.table("project_members").select("*").eq("project_slug", p["slug"]).execute().data or []
        p["health"] = compute_health(p)
    return projects


def get_portfolio() -> list:
    return get_projects()


def get_project(slug: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    r = sb.table("projects").select("*").eq("slug", slug).limit(1).execute()
    if not r.data:
        return None
    p = r.data[0]
    p["members"] = sb.table("project_members").select("*").eq("project_slug", slug).execute().data or []
    p["health"] = compute_health(p)
    return p


def _ensure_project(sb, slug: str, primary_repo: str = None):
    if sb.table("projects").select("id").eq("slug", slug).limit(1).execute().data:
        return  # already exists
    name = slug.replace("_", " ").replace("-", " ").title()
    full_row = {
        "id":                   str(uuid.uuid4()),
        "slug":                 slug,
        "name":                 name,
        "owner_name":           "",
        "status":               "Development",
        "note":                 "",
        "description":          "",
        "strategic_notes":      "",
        "priority":             "Medium",
        "strategic_importance": "Internal Tool",
        "current_blocker":      "",
        "next_milestone":       "",
        "target_launch_date":   None,
        "notes":                "",
        "human_locked_fields":  [],
        "primary_repo":         primary_repo or slug,
        "auto_created":         True,
        "recent_activity_count": 0,
        "created_at":           _now(),
        "updated_at":           _now(),
    }
    try:
        sb.table("projects").insert(full_row).execute()
    except Exception:
        # Phase 4 columns may not exist yet — fall back to the original schema
        minimal = {k: full_row[k] for k in
                   ("id", "slug", "name", "owner_name", "status", "note", "created_at", "updated_at")}
        try:
            sb.table("projects").insert(minimal).execute()
        except Exception:
            pass  # already exists or DB unavailable


def update_project(slug: str, human_edit: bool = False, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    _ensure_project(sb, slug)

    allowed = HUMAN_OWNED_FIELDS | GITHUB_FIELDS
    updates = {k: v for k, v in kwargs.items() if k in allowed}

    if human_edit and updates:
        project = get_project(slug) or {}
        locked = set(project.get('human_locked_fields') or [])
        locked.update(k for k in updates if k in HUMAN_OWNED_FIELDS)
        updates['human_locked_fields'] = list(locked)

    if updates:
        updates['updated_at'] = _now()
        sb.table('projects').update(updates).eq('slug', slug).execute()
    return get_project(slug)


def update_github_fields(slug: str, **kwargs) -> None:
    """Update only GitHub-managed fields; never overwrite human-locked fields."""
    sb = _sb()
    if not sb:
        return
    _ensure_project(sb, slug, primary_repo=kwargs.get('primary_repo'))
    project = get_project(slug)
    if not project:
        return
    locked = set(project.get('human_locked_fields') or [])
    updates = {
        k: v for k, v in kwargs.items()
        if v is not None and k in GITHUB_FIELDS and k not in locked
    }
    if 'last_activity_at' in kwargs:
        updates['recent_activity_count'] = (project.get('recent_activity_count') or 0) + 1
    if updates:
        updates['updated_at'] = _now()
        try:
            sb.table('projects').update(updates).eq('slug', slug).execute()
        except Exception as e:
            # Phase 4 columns may not exist yet — fall back to only the base fields
            print(f"[team] update_github_fields full update failed ({e}), trying base fields only")
            base_github = {'last_activity_at', 'recent_activity_count', 'updated_at'}
            safe = {k: v for k, v in updates.items() if k in base_github}
            if safe:
                try:
                    sb.table('projects').update(safe).eq('slug', slug).execute()
                except Exception as e2:
                    print(f"[team] update_github_fields base-field fallback also failed: {e2}")


def delete_project(slug: str) -> None:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    sb.table("project_members").delete().eq("project_slug", slug).execute()
    sb.table("project_messages").delete().eq("project_slug", slug).execute()
    sb.table("projects").delete().eq("slug", slug).execute()


def sync_projects_from_events() -> list[str]:
    """Create project rows for any event slugs not yet in the projects table."""
    sb = _sb()
    if not sb:
        return []
    events = sb.table("activity_events").select("project").execute().data or []
    slugs = {e["project"] for e in events if e.get("project") and e["project"] != "other"}
    existing = {r["slug"] for r in (sb.table("projects").select("slug").execute().data or [])}
    created = []
    for slug in slugs - existing:
        _ensure_project(sb, slug)
        created.append(slug)
    return created


# ── Members ───────────────────────────────────────────────────────────────────

def add_member(project_slug: str, user_name: str, role: str = "member") -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    _ensure_project(sb, project_slug)
    existing = sb.table("project_members").select("id").eq("project_slug", project_slug).eq("user_name", user_name).execute().data
    if existing:
        sb.table("project_members").update({"role": role}).eq("project_slug", project_slug).eq("user_name", user_name).execute()
    else:
        sb.table("project_members").insert({
            "id":           str(uuid.uuid4()),
            "project_slug": project_slug,
            "user_name":    user_name,
            "role":         role,
            "created_at":   _now(),
        }).execute()
    return get_project(project_slug)


def remove_member(project_slug: str, user_name: str) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    sb.table("project_members").delete().eq("project_slug", project_slug).eq("user_name", user_name).execute()
    return get_project(project_slug)


# ── Chat ──────────────────────────────────────────────────────────────────────

def get_messages(project_slug: str, reader_name: str = None) -> list:
    sb = _sb()
    if not sb:
        return []
    msgs = (
        sb.table("project_messages").select("*")
          .eq("project_slug", project_slug)
          .order("created_at")
          .limit(200).execute().data or []
    )
    if reader_name:
        for msg in msgs:
            rb = msg.get("read_by_json") or []
            if reader_name not in rb:
                rb.append(reader_name)
                sb.table("project_messages").update({"read_by_json": rb}).eq("id", msg["id"]).execute()
    return msgs


def send_message(project_slug: str, sender_name: str, message: str) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    row = {
        "id":           str(uuid.uuid4()),
        "project_slug": project_slug,
        "sender_name":  sender_name,
        "message":      message.strip(),
        "read_by_json": [sender_name],
        "created_at":   _now(),
    }
    sb.table("project_messages").insert(row).execute()
    return row


def get_unread_counts(reader_name: str) -> dict:
    sb = _sb()
    if not sb:
        return {}
    msgs = sb.table("project_messages").select("project_slug, read_by_json").execute().data or []
    counts: dict = {}
    for msg in msgs:
        slug = msg["project_slug"]
        rb   = msg.get("read_by_json") or []
        if reader_name not in rb:
            counts[slug] = counts.get(slug, 0) + 1
    return counts
