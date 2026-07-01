"""
Supabase database layer.
All reads/writes to blogs, leads, comments, and settings go through here.
Falls back gracefully (returns empty data) when Supabase is not configured
so the app still boots in local dev without env vars.
"""
import os
import uuid
from datetime import datetime

SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

_client = None

def _sb():
    """Return the Supabase client, initialising it on first call."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            return None
        try:
            from supabase import create_client
            _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        except Exception as e:
            print(f"[db] Supabase init failed: {e}")
            return None
    return _client


def is_connected() -> bool:
    return _sb() is not None


# ── Blog helpers ───────────────────────────────────────────────────────────────

def _row_to_blog(row: dict) -> dict:
    """Merge the indexed columns back into the full data blob."""
    data = row.get("data") or {}
    merged = {**data}
    # Always trust the top-level columns for indexed fields
    merged["id"]            = row.get("id", data.get("id", ""))
    merged["slug"]          = row.get("slug", data.get("slug", ""))
    merged["title"]         = row.get("title", data.get("title", ""))
    merged["published"]     = row.get("published", data.get("published", False))
    merged["category"]      = row.get("category", data.get("category", ""))
    merged["is_auto_posted"]= row.get("is_auto_posted", data.get("is_auto_posted", False))
    merged["seoScore"]      = row.get("seo_score", data.get("seoScore"))
    merged["views"]         = row.get("views", data.get("views", 0))
    merged["created_at"]    = row.get("created_at", data.get("created_at", ""))
    return merged


def _blog_to_row(blog: dict) -> dict:
    """Build a Supabase row from a blog dict."""
    return {
        "id":            blog["id"],
        "slug":          blog.get("slug", ""),
        "title":         blog.get("title", ""),
        "published":     blog.get("published", False),
        "category":      blog.get("category", ""),
        "is_auto_posted": blog.get("is_auto_posted", False),
        "seo_score":     blog.get("seoScore"),
        "views":         blog.get("views", 0),
        "data":          blog,
    }


# ── Blogs ──────────────────────────────────────────────────────────────────────

def get_all_blogs() -> list:
    sb = _sb()
    if not sb:
        return []
    res = sb.table("blogs").select("*").order("created_at", desc=True).execute()
    return [_row_to_blog(r) for r in (res.data or [])]


def get_published_blogs() -> list:
    sb = _sb()
    if not sb:
        return []
    res = (sb.table("blogs").select("*")
             .eq("published", True)
             .order("created_at", desc=True)
             .execute())
    return [_row_to_blog(r) for r in (res.data or [])]


def get_blog(slug_or_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    res = sb.table("blogs").select("*").eq("slug", slug_or_id).limit(1).execute()
    if res.data:
        return _row_to_blog(res.data[0])
    res = sb.table("blogs").select("*").eq("id", slug_or_id).limit(1).execute()
    if res.data:
        return _row_to_blog(res.data[0])
    return None


def insert_blog(blog: dict) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("Supabase not configured")
    sb.table("blogs").insert(_blog_to_row(blog)).execute()
    return blog


def update_blog(blog_id: str, blog: dict) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("Supabase not configured")
    row = _blog_to_row(blog)
    row["updated_at"] = datetime.utcnow().isoformat()
    sb.table("blogs").update(row).eq("id", blog_id).execute()
    return blog


def patch_blog(blog_id: str, fields: dict) -> dict | None:
    """Partial update — merges fields into existing blog."""
    sb = _sb()
    if not sb:
        return None
    res = sb.table("blogs").select("*").eq("id", blog_id).limit(1).execute()
    if not res.data:
        return None
    current = _row_to_blog(res.data[0])
    for k, v in fields.items():
        if k not in ("id", "created_at"):
            current[k] = v
    current["updated_at"] = datetime.utcnow().isoformat()
    return update_blog(blog_id, current)


def delete_blog(blog_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("blogs").delete().eq("id", blog_id).execute()
    # Cascade deletes comments via FK
    return True


def increment_blog_views(slug_or_id: str) -> None:
    sb = _sb()
    if not sb:
        return
    res = sb.table("blogs").select("id, views, data").eq("slug", slug_or_id).limit(1).execute()
    if not res.data:
        res = sb.table("blogs").select("id, views, data").eq("id", slug_or_id).limit(1).execute()
    if not res.data:
        return
    row = res.data[0]
    new_views = (row.get("views") or 0) + 1
    data = dict(row.get("data") or {})
    data["views"] = new_views
    sb.table("blogs").update({"views": new_views, "data": data}).eq("id", row["id"]).execute()


def get_review_queue() -> list:
    """Auto-posted drafts with seoScore >= 50, sorted best-first."""
    sb = _sb()
    if not sb:
        return []
    res = (sb.table("blogs").select("*")
             .eq("published", False)
             .eq("is_auto_posted", True)
             .gte("seo_score", 50)
             .order("seo_score", desc=True)
             .execute())
    return [_row_to_blog(r) for r in (res.data or [])]


# ── Leads ──────────────────────────────────────────────────────────────────────

def get_all_leads() -> list:
    sb = _sb()
    if not sb:
        return []
    res = sb.table("leads").select("*").order("created_at", desc=True).execute()
    return [r.get("data") or r for r in (res.data or [])]


def lead_exists(email: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    res = sb.table("leads").select("id").eq("email", email).limit(1).execute()
    return bool(res.data)


def insert_lead(lead: dict) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("Supabase not configured")
    row = {
        "id":     lead.get("id", str(uuid.uuid4())),
        "email":  lead.get("email", ""),
        "source": lead.get("source", "website"),
        "data":   lead,
    }
    sb.table("leads").insert(row).execute()
    return lead


# ── Comments ──────────────────────────────────────────────────────────────────

def get_comments(blog_slug: str) -> list:
    sb = _sb()
    if not sb:
        return []
    res = (sb.table("comments").select("*")
             .eq("blog_slug", blog_slug)
             .order("created_at")
             .execute())
    return res.data or []


def insert_comment(blog_id: str, blog_slug: str, comment: dict) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("Supabase not configured")
    row = {
        "id":        str(uuid.uuid4()),
        "blog_id":   blog_id,
        "blog_slug": blog_slug,
        "author":    comment.get("author", ""),
        "email":     comment.get("email", ""),
        "content":   comment.get("content", ""),
    }
    sb.table("comments").insert(row).execute()
    return {**row, "timestamp": datetime.utcnow().isoformat()}


def delete_blog_comments(blog_id: str) -> None:
    sb = _sb()
    if not sb:
        return
    sb.table("comments").delete().eq("blog_id", blog_id).execute()


# ── Affiliate Links ───────────────────────────────────────────────────────────

def get_affiliate_links() -> list:
    sb = _sb()
    if not sb:
        return []
    res = sb.table("affiliate_links").select("*").order("created_at", desc=False).execute()
    return res.data or []


def create_affiliate_link(keyword: str, url: str) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("Supabase not configured")
    row = {"id": str(uuid.uuid4()), "keyword": keyword.strip(), "url": url.strip(), "is_active": True}
    sb.table("affiliate_links").insert(row).execute()
    return row


def update_affiliate_link(link_id: str, fields: dict) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    allowed = {k: v for k, v in fields.items() if k in ("keyword", "url", "is_active")}
    if not allowed:
        return None
    sb.table("affiliate_links").update(allowed).eq("id", link_id).execute()
    res = sb.table("affiliate_links").select("*").eq("id", link_id).limit(1).execute()
    return res.data[0] if res.data else None


def delete_affiliate_link(link_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("affiliate_links").delete().eq("id", link_id).execute()
    return True


def log_affiliate_click(affiliate_id: str, blog_id: str, blog_slug: str) -> None:
    sb = _sb()
    if not sb:
        return
    sb.table("affiliate_clicks").insert({
        "id": str(uuid.uuid4()),
        "affiliate_id": affiliate_id,
        "blog_id": blog_id or "",
        "blog_slug": blog_slug or "",
    }).execute()


def get_affiliate_stats() -> list:
    """Returns each affiliate link with its total click count and per-blog breakdown."""
    sb = _sb()
    if not sb:
        return []
    links = sb.table("affiliate_links").select("*").order("created_at").execute().data or []
    clicks = sb.table("affiliate_clicks").select("affiliate_id, blog_id, blog_slug").execute().data or []

    from collections import defaultdict
    totals: dict = defaultdict(int)
    per_blog: dict = defaultdict(lambda: defaultdict(int))
    for c in clicks:
        aid = c["affiliate_id"]
        totals[aid] += 1
        slug = c.get("blog_slug") or c.get("blog_id") or "unknown"
        per_blog[aid][slug] += 1

    result = []
    for link in links:
        lid = link["id"]
        result.append({
            **link,
            "total_clicks": totals.get(lid, 0),
            "clicks_by_blog": dict(per_blog.get(lid, {})),
        })
    return result


# ── User Profiles ─────────────────────────────────────────────────────────────

def get_user_profile(user_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    res = sb.table("user_profiles").select("*").eq("id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def upsert_user_profile(profile: dict) -> dict:
    sb = _sb()
    if not sb:
        raise RuntimeError("Supabase not configured")
    sb.table("user_profiles").upsert(profile).execute()
    return profile


def get_all_user_profiles() -> list:
    sb = _sb()
    if not sb:
        return []
    res = sb.table("user_profiles").select("*").order("created_at", desc=True).execute()
    return res.data or []


def get_pending_admins() -> list:
    sb = _sb()
    if not sb:
        return []
    res = (sb.table("user_profiles").select("*")
             .eq("role", "pending_admin")
             .order("created_at", desc=True)
             .execute())
    return res.data or []


def set_user_role(user_id: str, role: str) -> None:
    sb = _sb()
    if not sb:
        return
    sb.table("user_profiles").update({"role": role}).eq("id", user_id).execute()


def toggle_saved_blog(user_id: str, blog_slug: str) -> list:
    profile = get_user_profile(user_id) or {"id": user_id, "saved_blogs": [], "favorite_blogs": []}
    saved = list(profile.get("saved_blogs") or [])
    if blog_slug in saved:
        saved.remove(blog_slug)
    else:
        saved.append(blog_slug)
    upsert_user_profile({**profile, "saved_blogs": saved})
    return saved


def toggle_favorite_blog(user_id: str, blog_slug: str) -> list:
    profile = get_user_profile(user_id) or {"id": user_id, "saved_blogs": [], "favorite_blogs": []}
    favs = list(profile.get("favorite_blogs") or [])
    if blog_slug in favs:
        favs.remove(blog_slug)
    else:
        favs.append(blog_slug)
    upsert_user_profile({**profile, "favorite_blogs": favs})
    return favs


def track_reading(user_id: str, blog_slug: str) -> None:
    profile = get_user_profile(user_id)
    if not profile:
        return
    history = list(profile.get("reading_history") or [])
    if blog_slug in history:
        history.remove(blog_slug)
    history.insert(0, blog_slug)
    upsert_user_profile({**profile, "reading_history": history[:20]})


def update_profile_fields(user_id: str, fields: dict) -> dict:
    """Update display_name, bio, avatar_color, avatar_url — safe subset only."""
    profile = get_user_profile(user_id)
    if not profile:
        raise RuntimeError("Profile not found")
    allowed = {"display_name", "bio", "avatar_color", "avatar_url"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    merged = {**profile, **updates}
    return upsert_user_profile(merged)


# ── Settings ──────────────────────────────────────────────────────────────────

def get_settings(key: str = "auto_blog") -> dict:
    sb = _sb()
    if not sb:
        return {}
    res = sb.table("settings").select("value").eq("key", key).limit(1).execute()
    return (res.data[0].get("value") or {}) if res.data else {}


def save_settings(value: dict, key: str = "auto_blog") -> None:
    sb = _sb()
    if not sb:
        return
    sb.table("settings").upsert({
        "key":        key,
        "value":      value,
        "updated_at": datetime.utcnow().isoformat(),
    }).execute()
