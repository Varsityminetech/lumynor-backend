"""
Lumynor Activity OS — Team Collaboration Layer
Project ownership, membership, and project-scoped chat.
"""
import uuid
from datetime import datetime, timezone
from db import _sb

TEAM_MEMBERS = ['Danish', 'Mustafa']


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Projects ──────────────────────────────────────────────────────────────────

def get_projects() -> list:
    sb = _sb()
    if not sb:
        return []
    projects = sb.table("projects").select("*").order("name").execute().data or []
    for p in projects:
        p["members"] = sb.table("project_members").select("*").eq("project_slug", p["slug"]).execute().data or []
    return projects


def get_project(slug: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    r = sb.table("projects").select("*").eq("slug", slug).limit(1).execute()
    if not r.data:
        return None
    p = r.data[0]
    p["members"] = sb.table("project_members").select("*").eq("project_slug", slug).execute().data or []
    return p


def _ensure_project(sb, slug: str):
    if not sb.table("projects").select("id").eq("slug", slug).limit(1).execute().data:
        sb.table("projects").insert({
            "id": str(uuid.uuid4()), "slug": slug, "name": slug,
            "owner_name": "", "status": "Development", "note": "",
            "created_at": _now(), "updated_at": _now(),
        }).execute()


def update_project(slug: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("DB not configured")
    _ensure_project(sb, slug)
    updates = {k: v for k, v in kwargs.items() if v is not None}
    updates["updated_at"] = _now()
    sb.table("projects").update(updates).eq("slug", slug).execute()
    return get_project(slug)


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
            "id": str(uuid.uuid4()),
            "project_slug": project_slug,
            "user_name": user_name,
            "role": role,
            "created_at": _now(),
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
