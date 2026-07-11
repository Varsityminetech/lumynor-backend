import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from auth import (
    seed_users, seed_audit, authenticate_user, create_access_token,
    decode_token, append_audit_log, get_audit_logs, update_user_credentials
)
from fastapi.responses import FileResponse, Response, JSONResponse, RedirectResponse
from agent_graph import company_app, set_broadcast_callback, set_raw_broadcast_callback
from exporter import markdown_to_docx, markdown_to_pptx
import db
import indexer
import activity as act
import team as tm
import authority as auth
import strategy as strat
import weekly_intel as wi

app = FastAPI(title="Lumynor Systems Engine")

# NOTE: the /export/docx and /export/pptx routes are registered further down, after
# _require_admin is defined — they expose internal pipeline output and are admin-only.
# (Depends(_require_admin) is evaluated at decoration time, so the route must be
# declared after that function exists, not here at the top of the module.)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # Auth is Bearer-token in the Authorization header, never cookies — so credentialed
    # CORS is not needed. "*" origins WITH allow_credentials=True is also an invalid combo
    # browsers reject. Credentials off keeps the wildcard valid and correct.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

seed_users()
seed_audit()

# ── RATE LIMITING ─────────────────────────────────────────────────────────────
# In-memory sliding-window limiter. Deliberately dependency-free (no slowapi) so
# nothing new has to resolve on the Railway build path. Single-instance only —
# counters are per-process, which is fine here but would need Redis if this ever
# scales to multiple replicas.
#
# The abuse surface is the PUBLIC, unauthenticated endpoints: lead/comment spam,
# login brute-force, and the WhatsApp webhook (which triggers paid LLM calls on
# every inbound message and has no Twilio signature check yet).

from collections import defaultdict, deque
import threading

_rl_lock = threading.Lock()
_rl_hits: dict = defaultdict(deque)     # "scope:ip" -> deque[timestamps]


def _client_ip(request: Request) -> str:
    """Real client IP. Railway sits behind a proxy, so request.client.host is the
    proxy — the true origin is the first entry in X-Forwarded-For."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(limit: int, window_seconds: int, scope: str):
    """FastAPI dependency: allow `limit` requests per `window_seconds` per client IP."""
    def _dep(request: Request):
        key = f"{scope}:{_client_ip(request)}"
        now = time.time()
        cutoff = now - window_seconds
        with _rl_lock:
            hits = _rl_hits[key]
            while hits and hits[0] < cutoff:      # drop timestamps outside the window
                hits.popleft()
            if len(hits) >= limit:
                retry = int(hits[0] + window_seconds - now) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many requests. Try again in {retry}s.",
                    headers={"Retry-After": str(retry)},
                )
            hits.append(now)
            # Opportunistic prune so the key dict can't grow unbounded from
            # one-off IPs that never come back.
            if len(_rl_hits) > 5000:
                for k in [k for k, v in _rl_hits.items() if not v or v[-1] < cutoff]:
                    _rl_hits.pop(k, None)
    return _dep


# ── MULTI-USER CONNECTION MANAGER ─────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.connections: dict = {}          # email -> {ws, user}
        self.agent_messages: list = []       # Shared agent chat history
        self.human_chat: list = []           # Human-to-human team chat
        self.pipeline_state: dict = {}       # Latest LangGraph state
        self.dm_histories: dict = {}         # user_email::agent_name -> list
        self.pipeline_running: bool = False
        self.pending_approval: dict = None   # Current approval gate info
        self.config: dict = None
        self.thread_id: str = None

    async def connect(self, websocket: WebSocket, user: dict):
        self.connections[user["sub"]] = {"ws": websocket, "user": user}
        # Send full current state to the newly connected user
        if self.pipeline_state:
            await websocket.send_json({"type": "state_update", "state": self.pipeline_state})
        if self.human_chat:
            await websocket.send_json({"type": "human_chat_history", "messages": self.human_chat})
        if self.pending_approval:
            await websocket.send_json({"type": "approval_required", **self.pending_approval})
        # Announce join to everyone
        join_msg = {
            "type": "human_chat",
            "sender": "System",
            "role": "system",
            "message": f"🟢 {user['name']} ({user['role']}) joined the session.",
            "timestamp": datetime.utcnow().isoformat()
        }
        await self.broadcast(join_msg)

    async def disconnect(self, user: dict):
        self.connections.pop(user["sub"], None)
        leave_msg = {
            "type": "human_chat",
            "sender": "System",
            "role": "system",
            "message": f"🔴 {user['name']} left the session.",
            "timestamp": datetime.utcnow().isoformat()
        }
        await self.broadcast(leave_msg)

    async def broadcast(self, message: dict):
        dead = []
        for email, conn in self.connections.items():
            try:
                await conn["ws"].send_json(message)
            except Exception:
                dead.append(email)
        for email in dead:
            self.connections.pop(email, None)

    async def broadcast_agent_message(self, msg: dict):
        self.agent_messages.append(msg)
        current = self.pipeline_state or {}
        current["chat_history"] = self.agent_messages
        self.pipeline_state = current
        await self.broadcast({"type": "agent_msg", "message": msg})

    async def broadcast_to_others(self, sender_email: str, message: dict):
        """Broadcast to all connections EXCEPT the sender."""
        dead = []
        for email, conn in self.connections.items():
            if email != sender_email:
                try:
                    await conn["ws"].send_json(message)
                except Exception:
                    dead.append(email)
        for email in dead:
            self.connections.pop(email, None)

    async def broadcast_human_chat(self, msg: dict, sender_email: str = None):
        self.human_chat.append(msg)
        payload = {"type": "human_chat", **msg}
        if sender_email:
            await self.broadcast_to_others(sender_email, payload)
        else:
            await self.broadcast(payload)

    def get_online_users(self):
        return [{"name": c["user"]["name"], "role": c["user"]["role"]}
                for c in self.connections.values()]

manager = ConnectionManager()

# Register broadcast callbacks so agents can stream messages + status events live
set_broadcast_callback(manager.broadcast_agent_message)
set_raw_broadcast_callback(manager.broadcast)

# ── AUTH ENDPOINTS ─────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/auth/login")
def login(req: LoginRequest, _rl=Depends(rate_limit(10, 900, "login"))):
    user = authenticate_user(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": user["email"], "name": user["name"], "role": user["role"]})
    return {"access_token": token, "token_type": "bearer",
            "user": {"email": user["email"], "name": user["name"], "role": user["role"]}}

# NOTE: /audit/logs is registered further down, after _require_admin is defined.
# Depends() resolves at decoration time, so the route cannot be declared here —
# doing so raises NameError at import and the app fails to boot.

from fastapi import Depends, Header, File, UploadFile, Request

class CredentialUpdateRequest(BaseModel):
    newEmail: str
    newPassword: str = None

@app.put("/auth/admin/credentials")
def update_credentials(req: CredentialUpdateRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid token")
    token = authorization.split(" ")[1]
    user = decode_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    success = update_user_credentials(user["sub"], req.newEmail, req.newPassword)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update credentials")
    
    # Optionally return a new token since the email (sub) changed
    new_token = create_access_token({"sub": req.newEmail, "name": user["name"], "role": user["role"]})
    return {"status": "success", "access_token": new_token, "user": {"email": req.newEmail, "name": user["name"], "role": user["role"]}}

# ── SUPABASE AUTH HELPERS ─────────────────────────────────────────────────────

OWNER_EMAIL = "varsityminetech@gmail.com"

def _get_supabase_user(authorization: str = Header(None)) -> dict:
    """Validate Supabase JWT from Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = authorization.split(" ", 1)[1]
    try:
        sb = db._sb()
        if not sb:
            raise HTTPException(status_code=500, detail="Database not configured")
        resp = sb.auth.get_user(token)
        if not resp.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return {"id": resp.user.id, "email": resp.user.email}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth error: {e}")


def _require_admin(auth_user: dict = Depends(_get_supabase_user)) -> dict:
    """Allow owner always; otherwise check role in user_profiles."""
    if auth_user["email"] == OWNER_EMAIL:
        return {**auth_user, "role": "admin"}
    profile = db.get_user_profile(auth_user["id"])
    if not profile or profile.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return {**auth_user, "role": "admin"}


# ── EXPORT ENDPOINTS (admin-only) ─────────────────────────────────────────────
# These dump the internal pipeline's current document. Previously public — anyone
# could fetch the in-progress internal report. Declared here (not at the top of the
# module) because Depends(_require_admin) needs that function to already exist.

@app.get("/audit/logs")
def audit_logs(_admin: dict = Depends(_require_admin)):
    return get_audit_logs()


@app.get("/export/docx")
async def export_docx(_admin: dict = Depends(_require_admin)):
    content = manager.pipeline_state.get("current_document", "No content available.")
    path = os.path.join(os.path.dirname(__file__), "export.docx")
    markdown_to_docx(content, path)
    return FileResponse(path, filename="Lumynor_Report.docx")

@app.get("/export/pptx")
async def export_pptx(_admin: dict = Depends(_require_admin)):
    content = manager.pipeline_state.get("current_document", "No content available.")
    path = os.path.join(os.path.dirname(__file__), "export.pptx")
    markdown_to_pptx(content, path)
    return FileResponse(path, filename="Lumynor_Presentation.pptx")


# ── PUBLIC SITE SETTINGS ───────────────────────────────────────────────────────
# WhatsApp number, chatbot persona, contact email — every visitor's browser needs
# to read these to render the live values. Previously these lived ONLY in the
# founder's own browser localStorage (SettingsContext.jsx), which never syncs to
# any other visitor — so a real visitor always saw the hardcoded placeholder
# defaults, no matter what the founder set in the admin panel.

_SITE_SETTINGS_DEFAULTS = {
    "whatsappNumber": "919999999999",
    "whatsappText":   "Hi Lumynor, I want to discuss a project inquiry.",
    "contactEmail":   "hello@lumynor.com",
    "chatbotName":    "Maya",
    "chatbotWelcome": "Hi, I'm Maya from Lumynor. Tell me what you want to build.",
    "foundingTeam":   "Mustafa and Danish",
}

@app.get("/api/site-settings")
def get_site_settings():
    """Public — no auth. Every visitor fetches this on page load."""
    stored = db.get_settings("site_content")
    return {**_SITE_SETTINGS_DEFAULTS, **stored}

@app.post("/api/site-settings")
def save_site_settings(body: dict, _admin: dict = Depends(_require_admin)):
    updates = {k: v for k, v in body.items() if k in _SITE_SETTINGS_DEFAULTS}
    existing = db.get_settings("site_content")
    merged = {**existing, **updates}
    db.save_settings(merged, "site_content")
    return {**_SITE_SETTINGS_DEFAULTS, **merged}


# ── USER / ROLE ENDPOINTS ─────────────────────────────────────────────────────

@app.post("/api/auth/sync-profile")
def sync_profile(auth_user: dict = Depends(_get_supabase_user)):
    """Create or refresh the user_profiles row after login/signup."""
    existing = db.get_user_profile(auth_user["id"])
    if existing:
        return existing
    # Owner gets admin role immediately, everyone else starts as 'user'
    role = "admin" if auth_user["email"] == OWNER_EMAIL else "user"
    profile = {
        "id":             auth_user["id"],
        "email":          auth_user["email"],
        "display_name":   auth_user["email"].split("@")[0],
        "role":           role,
        "saved_blogs":    [],
        "favorite_blogs": [],
        "bio":            "",
    }
    return db.upsert_user_profile(profile)


@app.get("/api/users/me")
def get_my_profile(auth_user: dict = Depends(_get_supabase_user)):
    profile = db.get_user_profile(auth_user["id"])
    if not profile:
        # Auto-create on first access
        role = "admin" if auth_user["email"] == OWNER_EMAIL else "user"
        profile = {
            "id":             auth_user["id"],
            "email":          auth_user["email"],
            "display_name":   auth_user["email"].split("@")[0],
            "role":           role,
            "saved_blogs":    [],
            "favorite_blogs": [],
            "bio":            "",
        }
        db.upsert_user_profile(profile)
    elif profile.get("email") == OWNER_EMAIL and profile.get("role") != "admin":
        db.set_user_role(profile["id"], "admin")
        profile["role"] = "admin"
    return profile


@app.post("/api/users/request-admin")
def request_admin(auth_user: dict = Depends(_get_supabase_user)):
    profile = db.get_user_profile(auth_user["id"])
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found. Please reload.")
    if profile.get("role") == "admin":
        return {"status": "already_admin"}
    db.set_user_role(auth_user["id"], "pending_admin")
    return {"status": "requested"}


@app.get("/api/users/pending-admins")
def pending_admins(_admin: dict = Depends(_require_admin)):
    return db.get_pending_admins()


@app.patch("/api/users/{user_id}/role")
def update_role(user_id: str, body: dict, _admin: dict = Depends(_require_admin)):
    role = body.get("role")
    if role not in ("user", "admin", "pending_admin"):
        raise HTTPException(status_code=400, detail="Invalid role")
    db.set_user_role(user_id, role)
    return {"status": "updated", "role": role}


@app.patch("/api/users/me")
def update_my_profile(body: dict, auth_user: dict = Depends(_get_supabase_user)):
    try:
        return db.update_profile_fields(auth_user["id"], body)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/users/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    auth_user: dict = Depends(_get_supabase_user),
):
    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, GIF, WEBP allowed")
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 5 MB")

    ext  = (file.filename or "jpg").rsplit(".", 1)[-1].lower()
    path = f"{auth_user['id']}/avatar.{ext}"
    sb   = db._sb()
    if not sb:
        raise HTTPException(status_code=500, detail="Database not configured")

    try:
        sb.storage.create_bucket("avatars", options={"public": True})
    except Exception:
        pass  # bucket already exists

    try:
        sb.storage.from_("avatars").upload(
            path, content,
            file_options={"content-type": file.content_type, "upsert": "true"},
        )
    except Exception:
        try:
            sb.storage.from_("avatars").remove([path])
            sb.storage.from_("avatars").upload(
                path, content,
                file_options={"content-type": file.content_type},
            )
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Upload failed: {e2}")

    url = sb.storage.from_("avatars").get_public_url(path)
    try:
        db.update_profile_fields(auth_user["id"], {"avatar_url": url})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile update failed: {e}")
    return {"avatar_url": url}


@app.post("/api/users/saved-blogs/{slug}")
def toggle_saved(slug: str, auth_user: dict = Depends(_get_supabase_user)):
    return {"saved_blogs": db.toggle_saved_blog(auth_user["id"], slug)}


@app.post("/api/users/favorite-blogs/{slug}")
def toggle_favorite(slug: str, auth_user: dict = Depends(_get_supabase_user)):
    return {"favorite_blogs": db.toggle_favorite_blog(auth_user["id"], slug)}


@app.post("/api/users/reading-history/{slug}")
def track_read(slug: str, auth_user: dict = Depends(_get_supabase_user)):
    db.track_reading(auth_user["id"], slug)
    return {"status": "tracked"}


@app.get("/api/users/community")
def community_members(_auth_user: dict = Depends(_get_supabase_user)):
    """Return public profiles of all users for the community tab."""
    profiles = db.get_all_user_profiles()
    return [
        {
            "id":           p["id"],
            "display_name": p.get("display_name", ""),
            "role":         p.get("role", "user"),
            "bio":          p.get("bio", ""),
            "avatar_color": p.get("avatar_color", "blue"),
            "avatar_url":   p.get("avatar_url", ""),
        }
        for p in profiles
    ]


# ── ACTIVITY OS ───────────────────────────────────────────────────────────────

@app.post("/api/activity")
def create_activity(body: dict, _admin: dict = Depends(_require_admin)):
    try:
        return act.create_event(body)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/activity")
def get_activity(
    project: str = None,
    status: str = None,
    priority: str = None,
    limit: int = 200,
    _admin: dict = Depends(_require_admin),
):
    return act.get_events(limit=limit, project=project, status=status, priority=priority)


@app.get("/api/activity/today")
def get_activity_today(_admin: dict = Depends(_require_admin)):
    return act.get_today_events()


@app.get("/api/activity/projects")
def get_activity_projects(_admin: dict = Depends(_require_admin)):
    return act.get_events_by_project()


@app.get("/api/activity/critical")
def get_activity_critical(_admin: dict = Depends(_require_admin)):
    return act.get_critical_events()


@app.get("/api/activity/summary/daily")
def get_activity_summary(_admin: dict = Depends(_require_admin)):
    return act.generate_atlas_summary()


@app.patch("/api/activity/{event_id}/status")
def update_activity_status(event_id: str, body: dict, _admin: dict = Depends(_require_admin)):
    status = body.get("status")
    if status not in act.STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use: {act.STATUSES}")
    result = act.update_event_status(event_id, status)
    if not result:
        raise HTTPException(status_code=404, detail="Event not found")
    return result


@app.get("/api/activity/snapshot")
def get_activity_snapshot(_admin: dict = Depends(_require_admin)):
    return act.get_executive_snapshot()


@app.get("/api/activity/momentum")
def get_activity_momentum(_admin: dict = Depends(_require_admin)):
    return act.get_project_momentum()


@app.get("/api/activity/decisions")
def get_activity_decisions(_admin: dict = Depends(_require_admin)):
    return act.get_decisions()


@app.get("/api/activity/project-status")
def get_project_status(_admin: dict = Depends(_require_admin)):
    return act.get_project_statuses()


@app.patch("/api/activity/project-status/{project}")
def update_project_status(project: str, body: dict, _admin: dict = Depends(_require_admin)):
    if project not in act.PROJECTS:
        raise HTTPException(status_code=400, detail=f"Unknown project. Use: {act.PROJECTS}")
    valid_statuses = ("Planning", "Development", "Testing", "Launch Ready", "Production", "Paused")
    status = body.get("status", "")
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use: {valid_statuses}")
    return act.update_project_status(project, status, body.get("note", ""))


# ── Projects API (founder-facing, human override aware) ───────────────────────

@app.get("/api/projects")
def list_projects(_admin: dict = Depends(_require_admin)):
    return tm.get_portfolio()


@app.get("/api/projects/portfolio")
def get_portfolio(_admin: dict = Depends(_require_admin)):
    return tm.get_portfolio()


@app.post("/api/projects/sync")
def sync_projects(_admin: dict = Depends(_require_admin)):
    """Create project rows for any event slugs not yet in the projects table."""
    created = tm.sync_projects_from_events()
    return {"created": created, "count": len(created)}


# ── Authority OS ───────────────────────────────────────────────────────────────

@app.get("/api/authority/opportunities")
def list_opportunities(
    status: str = None,
    project_slug: str = None,
    _admin: dict = Depends(_require_admin),
):
    return auth.get_opportunities(status=status, project_slug=project_slug)


@app.post("/api/authority/scan")
def scan_opportunities(_admin: dict = Depends(_require_admin)):
    result = auth.scan_opportunities()
    return result


@app.patch("/api/authority/opportunities/{opp_id}")
def update_opportunity(opp_id: str, body: dict, _admin: dict = Depends(_require_admin)):
    allowed = {"status", "title", "summary", "why_it_matters", "suggested_angle", "importance_score"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail=f"No valid fields. Allowed: {sorted(allowed)}")
    result = auth.update_opportunity(opp_id, **updates)
    if not result:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return result


@app.delete("/api/authority/opportunities/{opp_id}")
def delete_opportunity(opp_id: str, _admin: dict = Depends(_require_admin)):
    auth.delete_opportunity(opp_id)
    return {"deleted": opp_id}


@app.get("/api/authority/weekly")
def weekly_authority(_admin: dict = Depends(_require_admin)):
    return auth.get_weekly_summary()


# ── Strategy OS ───────────────────────────────────────────────────────────────

@app.get("/api/strategy/focus")
def strategy_focus(_admin: dict = Depends(_require_admin)):
    return strat.get_daily_focus()


@app.get("/api/strategy/matrix")
def strategy_matrix(_admin: dict = Depends(_require_admin)):
    return strat.get_matrix()


@app.get("/api/strategy/blockers")
def strategy_blockers(_admin: dict = Depends(_require_admin)):
    return strat.get_strategic_blockers()


@app.post("/api/strategy/attention")
def log_attention(body: dict, _admin: dict = Depends(_require_admin)):
    project_slug = body.get("project_slug", "")
    try:
        minutes = int(body.get("minutes", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="minutes must be a number")
    if not project_slug or minutes <= 0:
        raise HTTPException(status_code=400, detail="project_slug and minutes required")
    return strat.log_attention(project_slug, minutes)


@app.get("/api/strategy/attention")
def get_attention(days: int = 7, _admin: dict = Depends(_require_admin)):
    return strat.get_attention_allocation(days=days)


@app.post("/api/strategy/weekly-brief")
def generate_brief(_admin: dict = Depends(_require_admin)):
    return strat.generate_weekly_brief()


@app.get("/api/strategy/weekly-brief")
def get_brief(_admin: dict = Depends(_require_admin)):
    brief = strat.get_latest_brief()
    if not brief:
        raise HTTPException(status_code=404, detail="No brief generated yet")
    return brief


# ── Weekly Intelligence ───────────────────────────────────────────────────────

@app.get("/api/weekly/latest")
def weekly_latest(_admin: dict = Depends(_require_admin)):
    report = wi.get_latest_report()
    if not report:
        raise HTTPException(status_code=404, detail="No report generated yet")
    return report


@app.get("/api/weekly/history")
def weekly_history(_admin: dict = Depends(_require_admin)):
    return wi.get_all_reports()


@app.post("/api/weekly/generate")
def weekly_generate(_admin: dict = Depends(_require_admin)):
    return wi.generate_weekly_report()


# ── ATLAS Brain ───────────────────────────────────────────────────────────────
import atlas_brain as ab

@app.post("/api/atlas/chat")
def atlas_chat(body: dict, _admin: dict = Depends(_require_admin)):
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")
    history  = body.get("history") or []
    # Route through orchestrate so Mother can act on blog/affiliate commands from the dashboard too.
    # Use a stable "dashboard" identifier for the pending-confirmation state.
    answer = ab.orchestrate(question, from_number="dashboard", history=history)
    return {"answer": answer}

@app.get("/api/atlas/situation")
def atlas_situation(_admin: dict = Depends(_require_admin)):
    return ab.analyze_situation()

@app.get("/api/atlas/proactive/preview")
def atlas_proactive_preview(_admin: dict = Depends(_require_admin)):
    situation = ab.analyze_situation()
    text      = ab.generate_proactive_message(situation)
    return {"message": text, "situation": situation}

@app.post("/api/atlas/proactive/send")
def atlas_proactive_send(_admin: dict = Depends(_require_admin)):
    return ab.run_proactive_check()

@app.get("/api/lumy/notes")
def lumy_notes(limit: int = 20, _admin: dict = Depends(_require_admin)):
    """Lumy's private session notes — her longitudinal wellbeing journal."""
    return db.get_lumy_notes(limit=limit)

@app.get("/api/lumy/history")
def lumy_history(limit: int = 50, _admin: dict = Depends(_require_admin)):
    """Persistent Lumy conversation history across WhatsApp + dashboard."""
    return db.get_lumy_history(limit=limit)

@app.get("/api/lumy/reminders")
def lumy_reminders(_admin: dict = Depends(_require_admin)):
    """Pending reminders, soonest first."""
    return db.get_pending_lumy_reminders()

@app.get("/api/lumy/whatsapp-status")
def lumy_whatsapp_status(_admin: dict = Depends(_require_admin)):
    """Diagnostic: last inbound message time + last WhatsApp send failure, if any."""
    return db.get_settings("whatsapp_session")

@app.get("/api/atlas/settings")
def atlas_settings_get(_admin: dict = Depends(_require_admin)):
    stored = db.get_settings("atlas")
    return {
        "proactive_enabled":    stored.get("proactive_enabled", True),
        "proactive_hour_utc":   stored.get("proactive_hour_utc", 14),
        "proactive_minute":     stored.get("proactive_minute", 30),
        "last_proactive_at":    stored.get("last_proactive_at", ""),
        "last_proactive_ok":    stored.get("last_proactive_ok"),
        "last_proactive_error": stored.get("last_proactive_error", ""),
    }

@app.post("/api/atlas/settings")
def atlas_settings_save(body: dict, _admin: dict = Depends(_require_admin)):
    allowed = {"proactive_enabled", "proactive_hour_utc", "proactive_minute"}
    updates = {k: v for k, v in body.items() if k in allowed}
    existing = db.get_settings("atlas")
    db.save_settings({**existing, **updates}, "atlas")
    return {**existing, **updates}


# ── Design Audit Agent ────────────────────────────────────────────────────────
import design_audit as da

@app.post("/api/design-audit/run")
def design_audit_run(body: dict, _admin: dict = Depends(_require_admin)):
    url   = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    pages         = body.get("pages") or [url]
    notes         = body.get("notes") or ""
    auditor_notes = body.get("auditor_notes") or ""
    report = da.run_audit(url, pages, notes, auditor_notes)
    if report.get("error"):
        raise HTTPException(status_code=500, detail=report["error"])
    return da.save_audit(report)

@app.get("/api/design-audit/history")
def design_audit_history(limit: int = 20, _admin: dict = Depends(_require_admin)):
    return da.get_audits(limit)

@app.get("/api/design-audit/{audit_id}")
def design_audit_get(audit_id: str, _admin: dict = Depends(_require_admin)):
    report = da.get_audit(audit_id)
    if not report:
        raise HTTPException(status_code=404, detail="Audit not found")
    return report

@app.delete("/api/design-audit/{audit_id}")
def design_audit_delete(audit_id: str, _admin: dict = Depends(_require_admin)):
    da.delete_audit(audit_id)
    return {"ok": True}


# ── Daily Digest ──────────────────────────────────────────────────────────────

@app.get("/api/digest/preview")
def digest_preview(_admin: dict = Depends(_require_admin)):
    from digest import build_digest_text
    text = build_digest_text()
    if not text:
        raise HTTPException(status_code=500, detail="Could not build digest")
    return {"text": text}


@app.post("/api/digest/send")
def digest_send(_admin: dict = Depends(_require_admin)):
    from digest import send_digest
    return send_digest()


@app.get("/api/system/digest-settings")
def get_digest_settings(_admin: dict = Depends(_require_admin)):
    stored = db.get_settings("digest")
    return {
        "twilioAccountSid": stored.get("twilioAccountSid", ""),
        "twilioAuthToken":  stored.get("twilioAuthToken", ""),
        "twilioFrom":       stored.get("twilioFrom", "whatsapp:+14155238886"),
        "digestTo":         stored.get("digestTo", ""),
        "send_hour_utc":    stored.get("send_hour_utc", 2),
        "send_minute":      stored.get("send_minute", 30),
        "last_digest_at":    stored.get("last_digest_at", ""),
        "last_digest_ok":    stored.get("last_digest_ok"),
        "last_digest_error": stored.get("last_digest_error", ""),
    }


@app.post("/api/system/digest-settings")
def save_digest_settings(body: dict, _admin: dict = Depends(_require_admin)):
    allowed = {"twilioAccountSid", "twilioAuthToken", "twilioFrom", "digestTo", "send_hour_utc", "send_minute"}
    updates = {k: v for k, v in body.items() if k in allowed}
    existing = db.get_settings("digest")
    db.save_settings({**existing, **updates}, "digest")
    return {**existing, **updates}


@app.get("/api/projects/{slug}")
def get_project_detail(slug: str, _admin: dict = Depends(_require_admin)):
    p = tm.get_project(slug)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


@app.delete("/api/projects/{slug}")
def delete_project(slug: str, _admin: dict = Depends(_require_admin)):
    p = tm.get_project(slug)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    tm.delete_project(slug)
    return {"deleted": slug}


@app.patch("/api/projects/{slug}")
def update_project_detail(slug: str, body: dict, _admin: dict = Depends(_require_admin)):
    allowed = {
        'name', 'description', 'strategic_notes', 'owner_name', 'status',
        'priority', 'strategic_importance', 'current_blocker', 'next_milestone',
        'target_launch_date', 'notes', 'note',
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    return tm.update_project(slug, human_edit=True, **updates)


# ── Team / collaboration endpoints ────────────────────────────────────────────

@app.get("/api/team/projects")
def team_list_projects(_admin: dict = Depends(_require_admin)):
    return tm.get_projects()


@app.get("/api/team/projects/{slug}")
def team_get_project(slug: str, _admin: dict = Depends(_require_admin)):
    p = tm.get_project(slug)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


@app.patch("/api/team/projects/{slug}")
def team_update_project(slug: str, body: dict, _admin: dict = Depends(_require_admin)):
    return tm.update_project(slug, human_edit=True, **{k: v for k, v in body.items() if k in ("owner_name", "status", "note", "name")})


@app.post("/api/team/projects/{slug}/members")
def team_add_member(slug: str, body: dict, _admin: dict = Depends(_require_admin)):
    if body.get("user_name") not in tm.TEAM_MEMBERS:
        raise HTTPException(status_code=400, detail=f"Unknown member. Use: {tm.TEAM_MEMBERS}")
    return tm.add_member(slug, body["user_name"], body.get("role", "member"))


@app.delete("/api/team/projects/{slug}/members/{user_name}")
def team_remove_member(slug: str, user_name: str, _admin: dict = Depends(_require_admin)):
    return tm.remove_member(slug, user_name)


@app.get("/api/team/projects/{slug}/messages")
def team_get_messages(slug: str, reader_name: str = None, _admin: dict = Depends(_require_admin)):
    return tm.get_messages(slug, reader_name)


@app.post("/api/team/projects/{slug}/messages")
def team_send_message(slug: str, body: dict, _admin: dict = Depends(_require_admin)):
    if not body.get("message", "").strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    return tm.send_message(slug, body.get("sender_name", "Unknown"), body["message"])


@app.get("/api/team/unread")
def team_unread(reader_name: str, _admin: dict = Depends(_require_admin)):
    return tm.get_unread_counts(reader_name)


@app.post("/api/webhooks/github", include_in_schema=False)
async def github_webhook(request: Request):
    body = await request.body()
    sig  = request.headers.get("X-Hub-Signature-256", "")
    if not act.verify_github_signature(body, sig):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    import json as _json
    try:
        payload = _json.loads(body)
    except Exception:
        return {"status": "ignored", "reason": "invalid json"}
    gh_event = request.headers.get("X-GitHub-Event", "")
    parsed = act.parse_github_event(gh_event, payload)
    if parsed:
        try:
            act.create_event(parsed)
            project_slug = parsed.get('project')
            repo = (parsed.get('metadata_json') or {}).get('repo')
            if project_slug and project_slug != 'other':
                now_ts = datetime.now(timezone.utc).isoformat()
                gh_update: dict = {'last_activity_at': now_ts, 'primary_repo': repo or project_slug}
                if gh_event == 'push':
                    gh_update['last_commit_at'] = now_ts
                elif gh_event == 'pull_request':
                    gh_update['last_pr_at'] = now_ts
                elif gh_event == 'deployment_status':
                    gh_update['last_deploy_at'] = now_ts
                tm.update_github_fields(project_slug, **gh_update)
        except Exception as e:
            print(f"[activity] GitHub webhook storage error: {e}")
    return {"status": "ok"}


@app.post("/api/webhooks/whatsapp", include_in_schema=False)
async def whatsapp_webhook(request: Request, _rl=Depends(rate_limit(20, 60, "wa_hook"))):
    """Twilio calls this on every inbound WhatsApp message. Replies async (outside
    Twilio's webhook timeout) by sending a follow-up message via the REST API."""
    form = await request.form()
    body = (form.get("Body") or "").strip()
    from_number = (form.get("From") or "").strip()  # already "whatsapp:+91..."

    if body and from_number:
        asyncio.create_task(_handle_whatsapp_message(body, from_number))

    return Response(content="<Response></Response>", media_type="application/xml")


async def _handle_whatsapp_message(body: str, from_number: str):
    # Every inbound message resets Twilio's 24h free-form-reply session window —
    # record it so the keepalive daemon can warn before it closes.
    try:
        sess = db.get_settings("whatsapp_session")
        db.save_settings({**sess, "last_inbound_at": datetime.utcnow().isoformat()}, "whatsapp_session")
    except Exception as e:
        print(f"[whatsapp_session] could not record inbound timestamp: {e}")

    try:
        answer = ab.orchestrate(body, from_number)
    except Exception as e:
        answer = f"Hit an error answering that: {str(e)[:200]}"
    result = ab.send_whatsapp(answer, from_number)
    if not result.get("ok"):
        # Previously discarded entirely — a failed reply (bad credentials, closed
        # session window, wrong number format) left zero trace anywhere. She'd
        # process the message fine (visible via the dashboard, since memory is
        # shared cross-channel) but the actual WhatsApp reply would silently vanish.
        print(f"[whatsapp] send FAILED to {from_number}: {result.get('error')}")
        try:
            sess = db.get_settings("whatsapp_session")
            db.save_settings({**sess,
                "last_send_error":    str(result.get("error"))[:300],
                "last_send_error_at": datetime.utcnow().isoformat(),
            }, "whatsapp_session")
        except Exception:
            pass


# ── PIPELINE RUNNER ────────────────────────────────────────────────────────────

async def run_pipeline(start_dept: str = "R&D"):
    if manager.pipeline_running:
        return
    manager.pipeline_running = True
    manager.thread_id = str(uuid.uuid4())
    manager.config = {"configurable": {"thread_id": manager.thread_id}}

    # Map department name to node name
    node_map = {
        "R&D": "rd_department",
        "Design": "design_department",
        "Engineering": "engineering_department",
        "Security & Legal": "security_department",
        "Marketing": "marketing_department",
        "Content Creator": "content_department"
    }
    start_node = node_map.get(start_dept, "rd_department")

    initial_state = {"current_department": start_dept, "chat_history": [],
                     "current_document": "", "pending_approval": False}

    await manager.broadcast({"type": "pipeline_started",
                             "message": "🚀 Lumynor AI Pipeline is starting..."})

    try:
        async for event in company_app.astream(initial_state, manager.config, stream_mode="values"):
            if event.get("chat_history") or event.get("current_document"):
                manager.pipeline_state = event
                await manager.broadcast({"type": "state_update", "state": event})
                await asyncio.sleep(2)

        # First approval gate
        await trigger_approval_gate()
    except Exception as e:
        print(f"🔥 PIPELINE EXCEPTION: {e}")
        await manager.broadcast({"type": "human_chat", "sender": "System", "role": "system", "message": f"🔥 Error: {e}", "timestamp": datetime.utcnow().isoformat()})


async def trigger_approval_gate():
    state = company_app.get_state(manager.config)
    if state.next:
        dept = state.values.get("current_department", "Current")
        gate_info = {
            "message": f"The **{dept}** Department has finished. Awaiting CEO approval to proceed.",
            "department": dept
        }
        manager.pending_approval = gate_info
        await manager.broadcast({"type": "approval_required", **gate_info})

# ── WEBSOCKET ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Auth handshake
    try:
        token = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        user = decode_token(token)
        if not user:
            await websocket.send_json({"type": "auth_error", "message": "Invalid token."})
            await websocket.close(); return
    except Exception:
        await websocket.close(); return

    await websocket.send_json({"type": "auth_success",
                               "user": {"name": user["name"], "role": user["role"], "email": user["sub"]}})

    await manager.connect(websocket, user)

    try:
        while True:
            data = await websocket.receive_text()

            # ── Inter-department messaging ──
            if data.startswith("INTER_DEPT:"):
                # Format: INTER_DEPT:FROM_DEPT:TO_DEPT:message
                parts = data[len("INTER_DEPT:"):].split(":", 2)
                if len(parts) == 3:
                    from_dept, to_dept, content = parts
                    msg = {
                        "type": "inter_dept",
                        "from_dept": from_dept,
                        "to_dept": to_dept,
                        "sender": user["name"],
                        "role": user["role"],
                        "message": content,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    await manager.broadcast(msg)

            # ── CEO/Observer input into agent discussion ──
            elif data.startswith("DIRECTIVE:"):
                parts = data[len("DIRECTIVE:"):].split(":", 1)
                target_dept = "R&D"
                content = data[len("DIRECTIVE:"):]
                
                if len(parts) == 2 and parts[0] in ["R&D", "Design", "Engineering", "Security & Legal", "Marketing", "Content Creator"]:
                    target_dept, content = parts
                
                label = "👑 CEO Directive" if user["role"] == "ceo" else f"💬 {user['name']} (Observer)"
                msg = {"department": target_dept, "sender": label, "message": content}
                await manager.broadcast_agent_message(msg)

                # Start pipeline on first directive
                if not manager.pipeline_running:
                    asyncio.create_task(run_pipeline(start_dept=target_dept))

                if manager.pending_approval:
                    await manager.broadcast_agent_message({"department": "System", "sender": "Director", "message": f"Agents are revising based on the feedback..."})
                    try:
                        from agent_graph import run_department_node
                        dept_name = manager.pipeline_state.get("current_department", "R&D")
                        
                        # Trigger LLM revision organically
                        revised_data = await run_department_node(manager.pipeline_state, dept_name)
                        
                        # Merge the new agent chat
                        updated_chat = manager.pipeline_state.get("chat_history", []) + revised_data["chat_history"]
                        manager.pipeline_state["chat_history"] = updated_chat
                        manager.pipeline_state["current_document"] = revised_data["current_document"]
                        
                        # Broadcast the newly revised state immediately
                        await manager.broadcast({"type": "state_update", "state": manager.pipeline_state})
                        
                        # Update the LangGraph checkpoint memory
                        company_app.update_state(manager.config, {"chat_history": updated_chat, "current_document": revised_data["current_document"]})
                        
                        await manager.broadcast_agent_message({"department": "System", "sender": "Director", "message": f"Revision complete. Still awaiting CEO approval for **{dept_name}**."})
                    except Exception as e:
                        print(f"🔥 REVISION EXCEPTION: {e}")
                        await manager.broadcast_agent_message({"department": "System", "sender": "Director", "message": f"Error during revision: {e}"})

            # ── Direct Message to specific agent ──
            elif data.startswith("DM:"):
                # Format: DM:AgentName:message
                parts = data[len("DM:"):].split(":", 1)
                if len(parts) == 2:
                    agent_name, content = parts
                    from agent_graph import run_agent_dm
                    
                    user_email = user["sub"]
                    dm_key = f"{user_email}::{agent_name}"
                    if dm_key not in manager.dm_histories:
                        manager.dm_histories[dm_key] = []
                    
                    # Store user message
                    manager.dm_histories[dm_key].append({"sender": "You", "message": content})
                    
                    # Run agent DM response in background
                    async def handle_dm():
                        try:
                            # Pass history for context
                            history = manager.dm_histories[dm_key]
                            response = await run_agent_dm(agent_name, content, user["name"], history)
                            
                            # Store agent response
                            manager.dm_histories[dm_key].append({"sender": agent_name, "message": response})
                            # Cap history
                            manager.dm_histories[dm_key] = manager.dm_histories[dm_key][-10:]
                            
                            await manager.broadcast({"type": "agent_dm", "agent": agent_name, "message": response, "timestamp": datetime.utcnow().isoformat()})
                        except Exception as e:
                            print(f"🔥 DM EXCEPTION: {e}")
                    
                    asyncio.create_task(handle_dm())

            # ── Human team chat ──
            elif data.startswith("TEAM_CHAT:"):
                content = data[len("TEAM_CHAT:"):]
                msg = {
                    "sender": user["name"],
                    "role": user["role"],
                    "email": user["sub"],
                    "message": content,
                    "timestamp": datetime.utcnow().isoformat()
                }
                # Exclude sender — they add it optimistically on the frontend
                await manager.broadcast_human_chat(msg, sender_email=user["sub"])

            # ── CEO Approval ──
            elif data.startswith("APPROVE:") and user["role"] == "ceo":
                dept = data[len("APPROVE:"):]
                manager.pending_approval = None
                append_audit_log({"timestamp": datetime.utcnow().isoformat(),
                    "user_email": user["sub"], "user_name": user["name"],
                    "user_role": user["role"], "action": "APPROVED", "department": dept, "note": ""})

                await manager.broadcast_agent_message({
                    "department": "System", "sender": "Director",
                    "message": f"✅ CEO ({user['name']}) approved **{dept}**. Resuming pipeline..."
                })
                company_app.update_state(manager.config, {"pending_approval": False})

                try:
                    async for event in company_app.astream(None, manager.config, stream_mode="values"):
                        manager.pipeline_state = event
                        await manager.broadcast({"type": "state_update", "state": event})
                        await asyncio.sleep(2)

                    new_state = company_app.get_state(manager.config)
                    if new_state.next:
                        await trigger_approval_gate()
                    else:
                        await manager.broadcast({"type": "pipeline_complete",
                            "message": "🎉 All departments complete. Lumynor product is ready for launch!"})
                except Exception as e:
                    print(f"🔥 PIPELINE EXCEPTION: {e}")
                    await manager.broadcast({"type": "human_chat", "sender": "System", "role": "system", "message": f"🔥 Error: {e}", "timestamp": datetime.utcnow().isoformat()})


            # ── CEO Rejection ──
            elif data.startswith("REJECT:") and user["role"] == "ceo":
                dept = data[len("REJECT:"):]
                manager.pending_approval = None
                append_audit_log({"timestamp": datetime.utcnow().isoformat(),
                    "user_email": user["sub"], "user_name": user["name"],
                    "user_role": user["role"], "action": "REJECTED", "department": dept, "note": ""})

                await manager.broadcast_agent_message({
                    "department": "System", "sender": "Director",
                    "message": f"❌ CEO ({user['name']}) rejected **{dept}**. Agents will revise."
                })
                
                try:
                    from agent_graph import run_department_node
                    dept_name = manager.pipeline_state.get("current_department", "R&D")
                    
                    # Add a system message forcing them to rethink
                    manager.pipeline_state.setdefault("chat_history", []).append({
                        "department": "System", "sender": "Director", "message": f"CEO rejected the current direction. Please discuss alternative approaches."
                    })
                    
                    # Run the node again for a revision
                    revised_data = await run_department_node(manager.pipeline_state, dept_name)
                    
                    # Merge chat history
                    updated_chat = manager.pipeline_state.get("chat_history", []) + revised_data["chat_history"]
                    manager.pipeline_state["chat_history"] = updated_chat
                    manager.pipeline_state["current_document"] = revised_data["current_document"]
                    
                    # Broadcast update
                    await manager.broadcast({"type": "state_update", "state": manager.pipeline_state})
                    
                    # Update LangGraph checkpoint memory
                    company_app.update_state(manager.config, {"chat_history": updated_chat, "current_document": revised_data["current_document"]})
                    
                    # Re-trigger approval gate
                    await trigger_approval_gate()
                except Exception as e:
                    print(f"🔥 REVISION EXCEPTION: {e}")
                    await manager.broadcast_agent_message({"department": "System", "sender": "Director", "message": f"Error during revision: {e}"})

    except WebSocketDisconnect:
        await manager.disconnect(user)
    except Exception as e:
        print(f"WS Error [{user['sub']}]: {e}")
        await manager.disconnect(user)

# ── BLOGS & LEADS ENDPOINTS (DATABASE IN PERSISTENT JSON FILES) ─────────────────
import urllib.request
import json

# DATA_DIR: persistent storage path (Railway volume). Falls back to script dir locally.
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
os.makedirs(DATA_DIR, exist_ok=True)

def _seed_data_file(filename, default):
    """Ensure a data file exists in DATA_DIR, seeding from committed copy if present."""
    target = os.path.join(DATA_DIR, filename)
    if not os.path.exists(target):
        committed = os.path.join(os.path.dirname(__file__), filename)
        if os.path.exists(committed) and committed != target:
            try:
                with open(committed) as src, open(target, "w") as dst:
                    dst.write(src.read())
                return target
            except Exception as e:
                print(f"[seed] {filename}: {e}")
        with open(target, "w") as f:
            json.dump(default, f)
    return target

LEADS_FILE = _seed_data_file("leads.json", [])
BLOGS_FILE = _seed_data_file("blogs.json", [])
COMMENTS_FILE = _seed_data_file("comments.json", {})

def read_json_file(filepath, default_val):
    if not os.path.exists(filepath):
        return default_val
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception:
        return default_val

def write_json_file(filepath, data):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error writing to {filepath}: {e}")

def ensure_html_content(text: str) -> str:
    """Blog `content` must be HTML — the website renders it with
    dangerouslySetInnerHTML and styles it via `.prose`. Some generators emit
    raw Markdown, which then shows literal `#`/`**`/`*` on the page. Convert
    Markdown → HTML, but leave content that's already HTML untouched (idempotent).
    """
    import re as _re
    if not text or not text.strip():
        return text
    # Already HTML? (has a block-level tag) — don't double-process.
    if _re.search(r'<(h[1-6]|p|ul|ol|div|section|article|blockquote|figure)\b', text, _re.I):
        return text
    try:
        import markdown as _markdown
        return _markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    except Exception as e:
        print(f"[ensure_html_content] markdown conversion failed: {e}")
        return text


# ── UNIFIED MULTI-PROVIDER LLM ROUTER ──────────────────────────────────────────
# One raw-HTTP entry point for every supported text LLM. Returns the model's raw
# text output (expected to be a JSON string for blog generation). The whole app
# uses urllib (no LLM SDK), so every provider — including Anthropic — goes through
# raw HTTP here for a single, uniform code path.

# OpenAI-compatible providers share one request/response shape (chat/completions).
_OPENAI_COMPATIBLE = {
    "openai":     "https://api.openai.com/v1/chat/completions",
    "deepseek":   "https://api.deepseek.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
# Default model per cloud provider (used when the configured model is empty or is
# the local Ollama default that doesn't apply to a cloud provider).
_LLM_DEFAULT_MODEL = {
    "gemini":     "gemini-2.5-flash",
    "openai":     "gpt-4o",
    "anthropic":  "claude-opus-4-8",
    "openrouter": "openai/gpt-4o",
    "deepseek":   "deepseek-chat",
}
# Where to find each provider's API key in the environment (first match wins).
_LLM_ENV_KEYS = {
    "gemini":     ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai":     ("OPENAI_API_KEY",),
    "anthropic":  ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "deepseek":   ("DEEPSEEK_API_KEY",),
}

def _http_post_json(url, payload, headers=None, timeout=90):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def _strip_code_fences(text: str) -> str:
    import re as _re
    t = (text or "").strip()
    if t.startswith("```"):
        t = _re.sub(r'^```(?:json)?\s*', '', t)
        t = _re.sub(r'\s*```$', '', t)
    return t.strip()

def call_llm_text(provider, api_name, model, api_key, base_url, system, user, timeout=90):
    """Call any configured LLM and return its raw text output.

    provider:  "api" (cloud) or "local" (Ollama)
    api_name:  gemini | openai | anthropic | openrouter | deepseek
    Routing preserves prior behavior: cloud when provider=="api" OR api_name=="gemini"
    (the historical default), local Ollama otherwise.
    """
    name = (api_name or "gemini").lower()
    use_cloud = provider == "api" or name == "gemini"
    full_prompt = f"{system}\n\n{user}"

    if not use_cloud:
        base = (base_url or "http://localhost:11434").rstrip("/")
        resp = _http_post_json(
            f"{base}/api/generate",
            {"model": model or "qwen2.5:latest", "prompt": full_prompt, "format": "json", "stream": False},
            timeout=timeout,
        )
        return _strip_code_fences(resp.get("response", ""))

    # Resolve the key from the request or the environment.
    if not api_key:
        for env in _LLM_ENV_KEYS.get(name, ()):
            api_key = os.getenv(env, "")
            if api_key:
                break
    if not api_key:
        raise ValueError(f"No API key configured for provider '{name}'. Set it in Settings or as an environment variable.")

    # A configured Ollama model name doesn't apply to a cloud provider.
    m = model if (model and model != "qwen2.5:latest") else _LLM_DEFAULT_MODEL.get(name, "")

    if name == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
        resp = _http_post_json(
            url,
            {"contents": [{"parts": [{"text": full_prompt}]}],
             "generationConfig": {"responseMimeType": "application/json"}},
            timeout=timeout,
        )
        return _strip_code_fences(resp["candidates"][0]["content"]["parts"][0]["text"])

    if name in _OPENAI_COMPATIBLE:
        headers = {"Authorization": f"Bearer {api_key}"}
        if name == "openrouter":
            headers["HTTP-Referer"] = "https://lumynor.com"
            headers["X-Title"] = "Lumynor Auto-Blogger"
        resp = _http_post_json(
            _OPENAI_COMPATIBLE[name],
            {"model": m,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
             "response_format": {"type": "json_object"}},
            headers=headers, timeout=timeout,
        )
        return _strip_code_fences(resp["choices"][0]["message"]["content"])

    if name == "anthropic":
        # Anthropic has no native JSON mode — instruct via prompt, then parse.
        sys_text = system + "\n\nRespond with ONLY the raw JSON object — no markdown code fences, no prose before or after."
        resp = _http_post_json(
            "https://api.anthropic.com/v1/messages",
            {"model": m, "max_tokens": 8192, "system": sys_text,
             "messages": [{"role": "user", "content": user}]},
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=timeout,
        )
        parts = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
        return _strip_code_fences("".join(parts))

    raise ValueError(f"Unknown LLM provider '{name}'.")

class LeadSubscribeRequest(BaseModel):
    name: str
    email: str
    source: str = "lead_magnet"

class BlogSaveRequest(BaseModel):
    id: str = None
    title: str
    slug: str
    category: str
    author: str
    summary: str
    content: str
    published: bool = True
    coverImage: str = ""
    primaryKeyword: str = ""
    secondaryKeywords: str = ""

class BlogCommentRequest(BaseModel):
    author: str
    content: str

class BlogGenerateRequest(BaseModel):
    prompt: str
    category: str
    primaryKeyword: str = ""
    secondaryKeywords: str = ""
    tone: str = "Technical Expert"
    customContext: str = ""
    llmProvider: str = "local"
    llmApiName: str = "gemini"
    llmModelName: str = "qwen2.5:latest"
    llmApiKey: str = ""
    llmBaseUrl: str = ""

@app.post("/api/leads/subscribe")
def subscribe_lead(req: LeadSubscribeRequest, _rl=Depends(rate_limit(5, 600, "leads"))):
    if db.lead_exists(req.email):
        return {"status": "success", "message": "Already subscribed."}
    new_lead = {
        "id": str(uuid.uuid4()),
        "name": req.name,
        "email": req.email,
        "source": req.source,
        "timestamp": datetime.utcnow().isoformat()
    }
    db.insert_lead(new_lead)
    return {"status": "success", "lead": new_lead}

@app.post("/api/leads/chat")
def chat_lead(req: dict, _rl=Depends(rate_limit(5, 600, "leads"))):
    new_lead = {
        "id": str(uuid.uuid4()),
        "source": req.get("source", "chatbot"),
        "query": req.get("query", ""),
        "features": req.get("features", ""),
        "budget": req.get("budget", ""),
        "contact": req.get("contact", ""),
        "timestamp": datetime.utcnow().isoformat()
    }
    db.insert_lead(new_lead)
    return {"status": "success", "lead": new_lead}

@app.get("/api/leads/all")
def get_leads(_admin: dict = Depends(_require_admin)):
    return db.get_all_leads()

@app.post("/api/leads/contact")
def contact_inquiry(req: dict, _rl=Depends(rate_limit(5, 600, "leads"))):
    """Store a full project inquiry from the Contact page."""
    new_lead = {
        "id":          str(uuid.uuid4()),
        "source":      "contact_form",
        "name":        req.get("name", ""),
        "email":       req.get("email", ""),
        "phone":       req.get("phone", ""),
        "company":     req.get("company", ""),
        "projectType": req.get("projectType", ""),
        "budget":      req.get("budget", ""),
        "timeline":    req.get("timeline", ""),
        "description": req.get("description", ""),
        "timestamp":   datetime.utcnow().isoformat(),
    }
    db.insert_lead(new_lead)
    _send_email_alert(
        f"New inquiry from {new_lead['name'] or new_lead['email']}",
        f"Type: {new_lead['projectType']}\nBudget: {new_lead['budget']}\n"
        f"Timeline: {new_lead['timeline']}\n\n{new_lead['description']}"
    )
    return {"status": "success", "lead": new_lead}


# ── Alert helpers (fire-and-forget; env vars gate them) ───────────────────────

def _send_email_alert(subject: str, body: str):
    """Send plain-text email via SMTP SSL.
    Requires env vars: ALERT_EMAIL_TO, ALERT_SMTP_HOST, ALERT_SMTP_USER, ALERT_SMTP_PASS.
    Silently no-ops when any of those are missing."""
    import smtplib, email.mime.text, email.mime.multipart, threading
    to   = os.getenv("ALERT_EMAIL_TO", "")
    host = os.getenv("ALERT_SMTP_HOST", "")
    user = os.getenv("ALERT_SMTP_USER", "")
    pwd  = os.getenv("ALERT_SMTP_PASS", "")
    if not (to and host and user and pwd):
        return
    def _send():
        try:
            msg = email.mime.multipart.MIMEMultipart()
            msg["From"] = user
            msg["To"]   = to
            msg["Subject"] = f"[Lumynor Blog] {subject}"
            msg.attach(email.mime.text.MIMEText(body, "plain"))
            with smtplib.SMTP_SSL(host, 465, timeout=15) as srv:
                srv.login(user, pwd)
                srv.sendmail(user, to, msg.as_string())
        except Exception as _e:
            print(f"[email_alert] {_e}")
    threading.Thread(target=_send, daemon=True).start()


def _fire_publish_webhook(blog: dict):
    """POST to PUBLISH_WEBHOOK_URL on every blog publish (fire-and-forget).
    Set PUBLISH_WEBHOOK_URL in Railway env to enable (e.g. Zapier, n8n, Slack webhook)."""
    import threading
    url = os.getenv("PUBLISH_WEBHOOK_URL", "")
    if not url:
        return
    payload = {
        "id":       blog.get("id", ""),
        "title":    blog.get("title", ""),
        "slug":     blog.get("slug", ""),
        "seoScore": blog.get("seoScore"),
        "url":      f"/blog/{blog.get('slug', '')}",
    }
    def _post():
        try:
            import urllib.request, json as _json
            data = _json.dumps(payload).encode()
            req  = urllib.request.Request(url, data=data,
                                          headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception as _e:
            print(f"[publish_webhook] {_e}")
    threading.Thread(target=_post, daemon=True).start()


@app.get("/api/blogs")
def get_published_blogs():
    return db.get_published_blogs()

@app.get("/api/blogs/admin")
def get_all_blogs(_admin: dict = Depends(_require_admin)):
    return db.get_all_blogs()


@app.get("/api/blogs/review-queue")
def get_review_queue(_admin: dict = Depends(_require_admin)):
    """Return auto-posted draft blogs that need human review before publishing."""
    queue = db.get_review_queue()
    return {"count": len(queue), "blogs": queue}


@app.get("/api/blogs/{slug_or_id}")
def get_single_blog(slug_or_id: str):
    blog = db.get_blog(slug_or_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog post not found")
    db.increment_blog_views(slug_or_id)
    comments = db.get_comments(blog.get("slug", slug_or_id))
    return {**blog, "comments": comments}

@app.post("/api/blogs")
def create_blog(req: BlogSaveRequest, _admin: dict = Depends(_require_admin)):
    new_blog = req.dict()
    new_blog["content"] = ensure_html_content(new_blog.get("content", ""))
    new_blog["id"] = str(uuid.uuid4())
    new_blog["created_at"] = datetime.utcnow().isoformat()
    db.insert_blog(new_blog)
    return {"status": "success", "blog": new_blog}

@app.put("/api/blogs/{blog_id}")
def update_blog(blog_id: str, req: BlogSaveRequest, _admin: dict = Depends(_require_admin)):
    existing = db.get_blog(blog_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Blog post not found")
    updated = req.dict()
    updated["content"] = ensure_html_content(updated.get("content", ""))
    updated["id"] = blog_id
    updated["created_at"] = existing.get("created_at", datetime.utcnow().isoformat())
    updated["updated_at"] = datetime.utcnow().isoformat()
    db.update_blog(blog_id, updated)
    return {"status": "success", "blog": updated}

@app.patch("/api/blogs/{blog_id}")
def patch_blog(blog_id: str, req: dict, _admin: dict = Depends(_require_admin)):
    """Partial update — merges fields without losing AI metadata."""
    existing = db.get_blog(blog_id)
    updated  = db.patch_blog(blog_id, req)
    if not updated:
        raise HTTPException(status_code=404, detail="Blog post not found")
    # Auto-index when a post is published for the first time
    just_published = req.get("published") is True and not (existing or {}).get("published")
    if just_published:
        indexer.index_blog_async(updated)
    return {"status": "success", "blog": updated}

@app.delete("/api/blogs/{blog_id}")
def delete_blog(blog_id: str, _admin: dict = Depends(_require_admin)):
    existing = db.get_blog(blog_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Blog post not found")
    db.delete_blog(blog_id)
    return {"status": "success", "message": "Blog deleted successfully."}


# ── INDEXER ENDPOINTS ─────────────────────────────────────────────────────────

@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    blogs = db.get_published_blogs()
    xml   = indexer.generate_sitemap(blogs)
    return Response(content=xml, media_type="application/xml")


@app.post("/api/indexer/blog/{blog_id}")
def index_single_blog(blog_id: str, _admin: dict = Depends(_require_admin)):
    blog = db.get_blog(blog_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog not found")
    if not blog.get("published"):
        raise HTTPException(status_code=400, detail="Only published blogs can be indexed")
    results = indexer.index_blog(blog)
    db.patch_blog(blog_id, {"last_indexed_at": results["indexed_at"], "index_results": results})
    return results


@app.post("/api/indexer/all")
def index_all(_admin: dict = Depends(_require_admin)):
    blogs   = db.get_published_blogs()
    results = indexer.index_all_blogs(blogs)
    return results


@app.get("/api/indexer/status")
def indexer_status(_admin: dict = Depends(_require_admin)):
    blogs = db.get_all_blogs()
    return [
        {
            "id":               b.get("id"),
            "slug":             b.get("slug"),
            "title":            b.get("title"),
            "published":        b.get("published"),
            "last_indexed_at":  b.get("last_indexed_at"),
            "index_results":    b.get("index_results"),
        }
        for b in blogs
    ]

@app.post("/api/blogs/{slug_or_id}/comments")
def add_comment(slug_or_id: str, req: BlogCommentRequest, _rl=Depends(rate_limit(10, 600, "comments"))):
    blog = db.get_blog(slug_or_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog post not found")
    new_comment = db.insert_comment(
        blog_id=blog.get("id", ""),
        blog_slug=blog.get("slug", slug_or_id),
        comment={"author": req.author, "email": getattr(req, "email", ""), "content": req.content},
    )
    return {"status": "success", "comment": new_comment}

@app.get("/api/system/ollama-models")
def get_ollama_models(baseUrl: str = "http://localhost:11434", _admin: dict = Depends(_require_admin)):
    try:
        url = f"{baseUrl.rstrip('/')}/api/tags"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3.0) as response:
            data = json.loads(response.read().decode())
            models = [m.get("name") for m in data.get("models", [])]
            return {"status": "success", "models": models}
    except Exception as e:
        print(f"Error querying Ollama API: {e}")
        return {"status": "success", "models": []}

@app.post("/api/blogs/generate")
def generate_blog_draft(req: BlogGenerateRequest, _admin: dict = Depends(_require_admin)):
    # Setup prompt
    system_instruction = (
        "You are an expert SEO and copywriter assistant. Generate a highly engaging blog post based on the requested user prompt, category, keywords, and tone.\n"
        "Respond ONLY with a JSON object containing three keys: 'title', 'summary', and 'content'.\n"
        "The 'content' key must contain high-quality markdown formatting with subheadings (H2, H3), lists, and bullet points. "
        "Include an H2 or H3 heading section titled 'Frequently Asked Questions' (FAQ) containing 3 quick question/answer pairs addressing search intent, "
        "and a 'References' section at the end with a hyperlink citing external sources."
    )
    
    user_prompt = (
        f"Topic / Prompt: {req.prompt}\n"
        f"Category: {req.category}\n"
        f"Primary Keyword: {req.primaryKeyword}\n"
        f"Secondary Keywords: {req.secondaryKeywords}\n"
        f"Tone: {req.tone}\n"
        f"Additional Context: {req.customContext}\n"
    )

    result_json = None
    try:
        text_content = call_llm_text(
            req.llmProvider, req.llmApiName, req.llmModelName, req.llmApiKey, req.llmBaseUrl,
            system_instruction, user_prompt, timeout=90,
        )
        result_json = json.loads(text_content.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[generate_blog_draft] {req.llmApiName} error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate draft with {req.llmApiName}: {str(e)}")

    if not result_json or "title" not in result_json or "content" not in result_json:
        # Fallback response
        return {
            "title": f"Draft: {req.prompt[:40]}",
            "summary": f"A draft post about {req.prompt}.",
            "content": ensure_html_content(f"# {req.prompt}\n\nThis is a fallback generated draft post covering the primary keyword '{req.primaryKeyword}'.\n\n### Frequently Asked Questions\n**Q: What is this?**\n*A: This is a placeholder blog post draft.*\n\n### References\n- [Lumynor Systems Official Website](https://lumynor.com)")
        }

    # The website renders content as HTML — make sure Markdown drafts become HTML.
    result_json["content"] = ensure_html_content(result_json.get("content", ""))
    return result_json

# ── SEO OPTIMIZATION ENDPOINT ──────────────────────────────────────────────────
class BlogSeoOptimizeRequest(BaseModel):
    title: str
    summary: str
    content: str
    primaryKeyword: str
    secondaryKeywords: str = ""
    llmProvider: str = "local"
    llmApiName: str = "gemini"
    llmModelName: str = "qwen2.5:latest"
    llmApiKey: str = ""
    llmBaseUrl: str = ""

@app.post("/api/blogs/optimize-seo")
def optimize_seo_blog(req: BlogSeoOptimizeRequest, _admin: dict = Depends(_require_admin)):
    system_instruction = (
        "You are an expert SEO and copywriting analyst. You have been given a draft blog post that needs to be optimized for search engine optimization (SEO) and E-E-A-T guidelines.\n"
        "Analyze the current content, keywords, and layout, and rewrite it to satisfy all SEO rules. Specifically:\n"
        "1. Ensure the primary keyword is in the title, and the title is between 30 and 60 characters.\n"
        "2. Ensure the primary keyword is in the summary, and the summary is between 120 and 160 characters.\n"
        "3. Ensure the word count is at least 600 words.\n"
        "4. Ensure the primary keyword and secondary keywords are integrated naturally throughout the text (primary density between 0.6% and 2.2%).\n"
        "5. Include the primary keyword in the first paragraph.\n"
        "6. Include the primary keyword in at least one subheading H2 or H3.\n"
        "7. Include a structured Frequently Asked Questions (FAQ) section at the end with at least 3 Q&A pairs answering user search intent.\n"
        "8. Include a 'References' section at the end with active authority hyperlinks.\n\n"
        "Respond ONLY with a JSON object containing three keys: 'title', 'summary', and 'content'. Do not include any explanatory text before or after the JSON."
    )
    
    user_prompt = (
        f"Original Title: {req.title}\n"
        f"Original Summary: {req.summary}\n"
        f"Original Content: {req.content}\n"
        f"Target Primary Keyword: {req.primaryKeyword}\n"
        f"Target Secondary Keywords: {req.secondaryKeywords}\n"
    )

    result_json = None
    try:
        text_content = call_llm_text(
            req.llmProvider, req.llmApiName, req.llmModelName, req.llmApiKey, req.llmBaseUrl,
            system_instruction, user_prompt, timeout=120,
        )
        result_json = json.loads(text_content.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[optimize_seo] {req.llmApiName} error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to optimize with {req.llmApiName}: {str(e)}")

    if not result_json or "title" not in result_json or "content" not in result_json:
        return {
            "title": f"Optimized: {req.title}",
            "summary": req.summary or f"An optimized post about {req.primaryKeyword}.",
            "content": f"# {req.title}\n\n{req.content}\n\n### Frequently Asked Questions\n**Q: What are the main benefits?**\n*A: Improved search optimization and structured layouts.*\n\n### References\n- [Lumynor Blog](https://lumynor.com)"
        }

    return result_json

# ── AUTO-BLOGGER SETTINGS & DAEMON ─────────────────────────────────────────────
AUTO_BLOG_SETTINGS_FILE = _seed_data_file("auto_blog_settings.json", {})

class AutoBlogSettingsUpdateRequest(BaseModel):
    enabled: bool = False
    niche: str = "Latest AI News & Developments"
    keywords: str = "AI agents, LLMs, model releases, agentic AI, SaaS tools"
    topics: str = ""
    blog_format: str = "deep_dive"  # "deep_dive" | "roundup"
    frequency_hours: int = 24
    author: str = "Danish"
    category: str = "AI News"
    auto_publish: bool = True
    tone: str = "Technical Expert"
    # LLM passthrough (optional, uses system env if omitted)
    llmProvider: str = "api"
    llmApiName: str = "gemini"
    llmModelName: str = "gemini-2.5-flash"
    llmApiKey: str = ""
    llmBaseUrl: str = ""

@app.get("/api/system/auto-blog-settings")
def get_auto_blog_settings(_admin: dict = Depends(_require_admin)):
    stored = db.get_settings("auto_blog")
    defaults = {
        "enabled": False,
        "niche": "Latest AI News & Developments",
        "topics": "AI agents, LLMs, model releases, agentic AI, SaaS tools",
        "blog_format": "deep_dive",
        "frequency_hours": 24,
        "author": "Danish",
        "category": "AI News",
        "auto_publish": True,
        "tone": "Technical Expert",
        "llmProvider": "local",
        "llmApiName": "gemini",
        "llmModelName": "qwen2.5:latest",
        "llmApiKey": "",
        "llmBaseUrl": "",
        "last_run": None,
        "next_run": None,
        "run_count": 0
    }
    defaults.update(stored)
    return defaults

@app.post("/api/system/auto-blog-settings")
def update_auto_blog_settings(req: AutoBlogSettingsUpdateRequest, _admin: dict = Depends(_require_admin)):
    existing = db.get_settings("auto_blog")
    updated = {**existing, **req.dict()}
    db.save_settings(updated, "auto_blog")
    return updated

def fetch_trending_search(niche: str):
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = ddgs.text(f"{niche} industry news 2026", max_results=3)
            if results:
                snippets = []
                for r in results:
                    snippets.append(f"Title: {r.get('title')}\nSnippet: {r.get('body')}")
                return "\n\n".join(snippets)
    except Exception as e:
        print(f"DuckDuckGo search error: {e}")
    return "No search results available. Fallback to general trends."

import re

async def generate_and_post_auto_blog(settings):
    niche = settings.get("niche", "Agentic AI")
    topics = settings.get("topics", settings.get("keywords", ""))  # support legacy 'keywords' key
    author = settings.get("author", "Danish")
    category = settings.get("category", "Agentic AI")
    auto_publish = settings.get("auto_publish", True)
    tone = settings.get("tone", "Technical Expert")
    
    # Fetch trending news
    trending_context = fetch_trending_search(niche)
    
    system_instruction = (
        f"You are an expert automated SEO blog writer with a '{tone}' writing style. Generate a highly engaging, professional blog post based on the trending news and keywords provided.\n"
        "Respond ONLY with a JSON object containing three keys: 'title', 'summary', and 'content'.\n"
        "The 'content' key must contain high-quality markdown formatting with subheadings (H2, H3), lists, and bullet points. "
        "Include an H2 or H3 heading section titled 'Frequently Asked Questions' (FAQ) containing 3 quick question/answer pairs, "
        "and a 'References' section at the end with a hyperlink citing external sources."
    )
    
    user_prompt = (
        f"Niche/Topic: {niche}\n"
        f"Trending Context:\n{trending_context}\n"
        f"Keywords to target: {topics}\n"
        "Write a complete, high-quality article addressing this topic."
    )
    
    result_json = None
    provider = settings.get("llmProvider", "local")
    api_name = settings.get("llmApiName", "gemini")
    model_name = settings.get("llmModelName", "qwen2.5:latest")
    api_key = settings.get("llmApiKey", "")
    base_url = settings.get("llmBaseUrl", "http://localhost:11434")

    try:
        loop = asyncio.get_event_loop()
        text_content = await loop.run_in_executor(
            None,
            lambda: call_llm_text(provider, api_name, model_name, api_key, base_url,
                                  system_instruction, user_prompt, timeout=90),
        )
        result_json = json.loads(text_content.strip())
    except Exception as e:
        print(f"Auto-Blogger {api_name} error: {e}")

    if not result_json or "title" not in result_json or "content" not in result_json:
        result_json = {
            "title": f"The Future of {niche} in 2026",
            "summary": f"Exploring trending developments in {niche} and their impact on digital products.",
            "content": f"# The Future of {niche} in 2026\n\nAutomated post covering trends in {niche}.\n\n### Frequently Asked Questions\n**Q: What are the key trends?**\n*A: Advanced AI integrations and custom agentic frameworks.*\n\n### References\n- [Lumynor Trends](https://lumynor.com)"
        }
        
    slug = re.sub(r'[^a-z0-9\s-]', '', result_json["title"].lower())
    slug = re.sub(r'[\s-]+', '-', slug).strip('-')

    new_blog = {
        "id": str(uuid.uuid4()),
        "title": result_json["title"],
        "slug": slug,
        "category": category,
        "author": author,
        "summary": result_json.get("summary", ""),
        "content": ensure_html_content(result_json["content"]),
        "published": auto_publish,
        "coverImage": "",
        "primaryKeyword": topics.split(",")[0].strip() if topics else "",
        "secondaryKeywords": topics,
        "created_at": datetime.utcnow().isoformat(),
        "is_auto_posted": True
    }

    db.insert_blog(new_blog)

    # Update runtime tracking fields in settings
    now_iso = datetime.utcnow().isoformat()
    settings["last_run"] = now_iso
    settings["run_count"] = settings.get("run_count", 0) + 1
    db.save_settings(settings)
    
    await manager.broadcast({
        "type": "blog_published",
        "blog": new_blog,
        "message": f"\U0001f4f0 Auto-Blogger published: '{new_blog['title']}'"
    })
    print(f"\U0001f4f0 Auto-Blogger published post: {new_blog['title']}")

_last_purge_ts = 0

def expire_stale_blogs():
    """Auto-EXPIRE stale posts by UNPUBLISHING (not deleting) them — preserves
    the URL/SEO equity and is reversible. A post is retired only if it's BOTH
    old (> BLOG_EXPIRY_DAYS, clamped 180-365 = 6mo-1yr) AND low-traffic
    (< BLOG_EXPIRY_MIN_VIEWS). High-traffic old posts stay live."""
    days = max(180, min(365, int(os.getenv("BLOG_EXPIRY_DAYS", "270"))))
    min_views = int(os.getenv("BLOG_EXPIRY_MIN_VIEWS", "50"))
    blogs = db.get_all_blogs()
    if not blogs:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    changed = 0
    for b in blogs:
        if not b.get("published", False):
            continue  # already hidden
        ts = b.get("created_at") or b.get("generatedAt")
        created = None
        if ts:
            try:
                created = datetime.fromisoformat(str(ts).replace("Z", ""))
            except Exception:
                created = None
        if created and created < cutoff and int(b.get("views", 0)) < min_views:
            db.patch_blog(b["id"], {
                "published":    False,
                "expiredAt":    datetime.utcnow().isoformat(),
                "expiredReason": f"auto-expired: >{days}d old, <{min_views} views",
            })
            changed += 1
    if changed:
        print(f"\U0001f4e6 Auto-unpublished {changed} stale low-traffic post(s) (>{days}d, <{min_views} views)")
    return changed


async def auto_blogger_daemon():
    print("\U0001f916 Auto-Blogger Daemon started.")
    while True:
        try:
            await asyncio.sleep(10)
            # Expire old posts (throttled to ~6h) regardless of auto-blog enabled state.
            global _last_purge_ts
            if time.time() - _last_purge_ts > 21600:
                _last_purge_ts = time.time()
                try:
                    expire_stale_blogs()
                except Exception as e:
                    print(f"[expire] error: {e}")
            settings = db.get_settings()
            if not settings.get("enabled", False):
                continue
            
            # Support both new field (frequency_hours int) and legacy (frequency string)
            frequency_hours = settings.get("frequency_hours")
            if frequency_hours is None:
                # Legacy string fallback
                freq_str = settings.get("frequency", "Daily")
                freq_map = {
                    "Every 2 Minutes": 2/60,
                    "Every 5 Minutes": 5/60,
                    "Every 30 Minutes": 0.5,
                    "Hourly": 1,
                    "Daily": 24,
                    "Weekly": 168
                }
                frequency_hours = freq_map.get(freq_str, 24)
            
            interval_seconds = int(frequency_hours * 3600)

            # Check last run - support both new (last_run) and legacy (last_posted_at)
            last_run_str = settings.get("last_run") or settings.get("last_posted_at")
                
            should_post = False
            if not last_run_str:
                should_post = True
            else:
                try:
                    last_run = datetime.fromisoformat(last_run_str)
                    elapsed = (datetime.utcnow() - last_run).total_seconds()
                    if elapsed >= interval_seconds:
                        should_post = True
                except Exception as ex:
                    print(f"Error parsing last_run: {ex}")
                    should_post = True
                    
            if should_post:
                print("Auto-Blogger: Time to generate a new post!")
                # Use the full HTML pipeline (v2). It produces clean HTML content,
                # structured FAQ/references, images and SEO scoring. It falls back
                # to the legacy generator only if there's no Gemini key or on error.
                await generate_and_post_auto_blog_v2(settings)
                
        except Exception as e:
            print(f"\U0001f525 Auto-Blogger Daemon Error: {e}")

# ── ENHANCED AUTO-BLOG PIPELINE ENDPOINTS ──────────────────────────────────────

from auto_blogger import (
    run_auto_blog_pipeline, research_trending_topics, _tavily_search,
    revise_blog_from_audit, validate_seo, _build_llm_cfg,
    validate_credibility, revise_blog_credibility, check_plagiarism, revise_blog_plagiarism,
    format_blog_html, inject_affiliate_links, strip_affiliate_links,
)

@app.get("/api/system/test-images")
async def test_images(_admin: dict = Depends(_require_admin)):
    """Test Pexels and Unsplash API keys from env — returns status, sample URL, or error for each."""
    import os as _os
    from auto_blogger import _search_pexels, _search_unsplash
    pexels_key   = _os.getenv("PEXELS_API_KEY", "")
    unsplash_key = _os.getenv("UNSPLASH_ACCESS_KEY", "")
    result = {}

    # Pexels
    if pexels_key:
        try:
            url = _search_pexels("artificial intelligence technology", pexels_key)
            result["pexels"] = {"status": "ok", "url": url} if url else {"status": "failed", "error": "returned no photos"}
        except Exception as e:
            result["pexels"] = {"status": "error", "error": str(e)}
    else:
        result["pexels"] = {"status": "no_key", "error": "PEXELS_API_KEY not set in Railway env"}

    # Unsplash
    if unsplash_key:
        try:
            photo = _search_unsplash("artificial intelligence technology", unsplash_key)
            result["unsplash"] = {"status": "ok", "url": photo["url"]} if photo else {"status": "failed", "error": "returned no photos"}
        except Exception as e:
            result["unsplash"] = {"status": "error", "error": str(e)}
    else:
        result["unsplash"] = {"status": "no_key", "error": "UNSPLASH_ACCESS_KEY not set in Railway env"}

    return result


@app.get("/api/system/test-tavily")
async def test_tavily(key: str = "", _admin: dict = Depends(_require_admin)):
    """Test whether a Tavily API key works. Pass ?key=tvly-xxx or leave blank to check env."""
    import os as _os
    resolved_key = key or _os.getenv("TAVILY_API_KEY", "")
    if not resolved_key:
        return {"status": "no_key", "message": "No TAVILY_API_KEY found in env or request param"}
    results = _tavily_search("latest AI news today", resolved_key, num=2, depth="basic")
    if results:
        return {
            "status": "ok",
            "key_source": "param" if key else "env",
            "results_count": len(results),
            "sample_title": results[0].get("title", ""),
            "sample_url": results[0].get("url", ""),
        }
    return {"status": "failed", "message": "Tavily returned empty results — check key validity"}

class AutoBlogRunRequest(BaseModel):
    niche: str = ""
    keywords: str = ""
    author: str = ""
    category: str = ""
    auto_publish: bool = False
    nanobanana_key: str = ""
    nanobanana_url: str = ""
    gemini_api_key: str = ""
    image_source: str = ""      # "web" (default) or "ai"
    unsplash_key: str = ""
    pexels_key: str = ""

async def _auto_generate_bg(merged_settings: dict, gemini_key: str, llm_cfg: dict,
                            auto_publish_flag: bool, settings_snapshot: dict,
                            _pub_blogs_ag: list, _recent_ag: list):
    """Full pipeline running as a fire-and-forget background task."""
    loop = asyncio.get_event_loop()
    try:
        blog_object = await loop.run_in_executor(
            None, run_auto_blog_pipeline, merged_settings, gemini_key, _recent_ag, _pub_blogs_ag
        )
    except Exception as e:
        print(f"[auto_generate_bg] Pipeline failed: {e}")
        await manager.broadcast({"type": "blog_error", "message": f"Auto-blog pipeline failed: {e}"})
        return

    # ── Stage A: SEO surgical revision — up to 3 loops ───────────────────────────
    min_score = int(os.getenv("BLOG_MIN_PUBLISH_SCORE", "90"))
    initial_score = blog_object.get("seoScore") or 0
    if initial_score < min_score:
        try:
            initial_audit = validate_seo(blog_object)
            stored_rb = blog_object.get("researchBrief") or {}
            rb = {"core_angle": stored_rb.get("core_angle", ""),
                  "lumynor_perspective": stored_rb.get("lumynor_perspective", ""),
                  "main_facts": [], "key_statistics": [], "claims_to_avoid": [], "faqs": []} if stored_rb else {}
            seo_revision = await loop.run_in_executor(
                None, revise_blog_from_audit,
                blog_object, initial_audit, rb, llm_cfg,
                os.getenv("TAVILY_API_KEY", ""), 3, _pub_blogs_ag,
            )
            blog_object = seo_revision["revised_blog"]
            if blog_object.get("content_html") and not blog_object.get("content_markdown") and not blog_object.get("content"):
                blog_object["content"] = blog_object["content_html"]
            blog_object["seoScore"] = seo_revision["new_seo_score"]
            blog_object["seoGrade"] = seo_revision["new_seo_grade"]
            print(f"[auto_generate_bg] SEO revision: {initial_score} → {blog_object['seoScore']}/100")
        except Exception as e:
            print(f"[auto_generate_bg] SEO revision failed: {e}")

    # ── Stage B: Credibility audit + surgical rewrite — up to 3 loops ─────────
    try:
        cred_audit = validate_credibility(blog_object)
        print(f"[auto_generate_bg] Credibility initial score: {cred_audit['score']}/100 — {cred_audit['status']}")
        if cred_audit["score"] < 90 and not cred_audit.get("hard_fail_reasons"):
            cred_result = await loop.run_in_executor(
                None, revise_blog_credibility,
                blog_object, cred_audit, llm_cfg, 3,
            )
            blog_object = cred_result["revised_blog"]
            if blog_object.get("content_html") and not blog_object.get("content_markdown") and not blog_object.get("content"):
                blog_object["content"] = blog_object["content_html"]
            blog_object["credibilityScore"] = cred_result["new_credibility_score"]
            blog_object["credibilityGrade"] = cred_result["new_credibility_grade"]
            print(f"[auto_generate_bg] Credibility after revision: {cred_result['new_credibility_score']}/100")
        elif cred_audit.get("hard_fail_reasons"):
            print(f"[auto_generate_bg] Credibility hard fail — skipping rewrite: {cred_audit['hard_fail_reasons']}")
            blog_object["credibilityScore"] = cred_audit["score"]
        else:
            blog_object["credibilityScore"] = cred_audit["score"]
    except Exception as e:
        print(f"[auto_generate_bg] Credibility pass failed: {e}")

    # ── Stage C: Plagiarism check + surgical rewrite — up to 3 loops ────────────
    tavily_key = os.getenv("TAVILY_API_KEY", "")
    try:
        _plag_content = blog_object.get("content_markdown", blog_object.get("content", ""))
        plag_audit = await loop.run_in_executor(
            None, check_plagiarism, _plag_content, tavily_key
        )
        print(f"[auto_generate_bg] Plagiarism initial score: {plag_audit['score']}/100 — {plag_audit['flagged_count']} flagged")
        if plag_audit["score"] < 90 and plag_audit.get("flagged"):
            plag_result = await loop.run_in_executor(
                None, revise_blog_plagiarism,
                blog_object, plag_audit, llm_cfg, tavily_key, 3,
            )
            blog_object = plag_result["revised_blog"]
            blog_object["plagiarismScore"] = plag_result["new_plagiarism_score"]
            print(f"[auto_generate_bg] Plagiarism after revision: {plag_result['new_plagiarism_score']}/100")
        else:
            blog_object["plagiarismScore"] = plag_audit["score"]
    except Exception as e:
        print(f"[auto_generate_bg] Plagiarism pass failed: {e}")

    # ── Format: Convert Markdown → branded HTML (runs after all audits pass) ────
    try:
        _sec_imgs = blog_object.pop("_section_imgs", [])
        blog_object = await loop.run_in_executor(
            None, format_blog_html, blog_object, _sec_imgs, _pub_blogs_ag
        )
        print(f"[auto_generate_bg] HTML formatting complete — content length: {len(blog_object.get('content', ''))}")
    except Exception as e:
        print(f"[auto_generate_bg] HTML formatting failed: {e}")
        if blog_object.get("content_markdown") and not blog_object.get("content"):
            blog_object["content"] = blog_object["content_markdown"]

    # ── Publish gate: SEO ≥ 90, credibility ≥ 75, plagiarism ≥ 75, real cover image ──
    final_score    = blog_object.get("seoScore") or 0
    cred_score     = blog_object.get("credibilityScore") or 0
    plag_score     = blog_object.get("plagiarismScore") or 100
    seo_ok         = final_score >= min_score
    cred_ok        = cred_score >= 75 or cred_score == 0
    plag_ok        = plag_score >= 75
    cover          = blog_object.get("coverImage") or ""
    has_real_image = bool(cover and "placehold.co" not in cover)
    publish = auto_publish_flag and seo_ok and cred_ok and plag_ok and has_real_image

    new_blog = {
        "id": str(uuid.uuid4()),
        **blog_object,
        "created_at": datetime.utcnow().isoformat(),
        "is_auto_posted": True,
        "published": publish,
    }
    if not seo_ok:
        new_blog["draftReason"] = f"SEO {final_score}/100 below publish threshold {min_score}"
    elif not cred_ok:
        new_blog["draftReason"] = f"Credibility {cred_score}/100 below minimum 75 — review sources and claims"
    elif not plag_ok:
        new_blog["draftReason"] = f"Plagiarism score {plag_score}/100 below minimum 75 — content too similar to external sources"
    elif not has_real_image:
        new_blog["draftReason"] = "No real cover image — add before publishing"
    db.insert_blog(new_blog)

    settings_snapshot["last_run"] = datetime.utcnow().isoformat()
    settings_snapshot["run_count"] = settings_snapshot.get("run_count", 0) + 1
    db.save_settings(settings_snapshot)

    if publish:
        _fire_publish_webhook(new_blog)
        _send_email_alert(
            f"Published: {new_blog['title']}",
            f"SEO score: {final_score}/100\nSlug: {new_blog.get('slug')}\nURL: /blog/{new_blog.get('slug')}"
        )
    elif new_blog.get("draftReason"):
        _send_email_alert(
            f"Draft saved for review: {new_blog.get('title', 'Untitled')}",
            f"Reason: {new_blog['draftReason']}\nSEO score: {final_score}/100\n"
            f"Review queue: /api/blogs/review-queue"
        )

    await manager.broadcast({
        "type": "blog_published" if publish else "blog_draft",
        "blog": new_blog,
        "message": f"{'📰 Published' if publish else '📝 Saved as draft'}: '{new_blog['title']}' (SEO: {final_score}/100)"
    })
    print(f"[auto_generate_bg] Done — blog_id={new_blog['id']} published={publish}")


@app.post("/api/blogs/auto-generate")
async def auto_generate_blog(req: AutoBlogRunRequest, _admin: dict = Depends(_require_admin)):
    """Kick off the full auto-blog pipeline as a background task and return 202 immediately."""
    settings = db.get_settings()

    merged_settings = {
        "niche": req.niche or settings.get("niche", "Technology"),
        "keywords": req.keywords or settings.get("topics", ""),
        "author": req.author or settings.get("author", "Lumynor Team"),
        "category": req.category or settings.get("category", "Technology"),
        "auto_publish": req.auto_publish if req.auto_publish else settings.get("auto_publish", False),
        "nanobanana_key": req.nanobanana_key or settings.get("nanobanana_key", ""),
        "nanobanana_url": req.nanobanana_url or settings.get("nanobanana_url", ""),
        "image_source": req.image_source or settings.get("image_source", "web"),
        "unsplash_key": req.unsplash_key or settings.get("unsplash_key", ""),
        "pexels_key": req.pexels_key or settings.get("pexels_key", ""),
    }

    gemini_key = (req.gemini_api_key or settings.get("llmApiKey") or
                  os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "")
    ollama_key = os.getenv("OLLAMA_API_KEY", "")
    if not gemini_key and not ollama_key:
        raise HTTPException(status_code=400, detail="No LLM key configured. Set GEMINI_API_KEY or OLLAMA_API_KEY in Railway environment.")

    llm_cfg = _build_llm_cfg(merged_settings, gemini_key)
    _all_blogs_ag = db.get_all_blogs()
    _pub_blogs_ag = [b for b in _all_blogs_ag if b.get("published")]
    _recent_ag    = [b["title"] for b in _all_blogs_ag if b.get("title")][-20:]
    auto_publish_flag = bool(merged_settings.get("auto_publish"))

    asyncio.create_task(_auto_generate_bg(
        merged_settings, gemini_key, llm_cfg,
        auto_publish_flag, settings,
        _pub_blogs_ag, _recent_ag,
    ))

    return JSONResponse(
        status_code=202,
        content={
            "status": "queued",
            "message": "Blog generation started. Poll /api/blogs for the new post or watch WebSocket for completion.",
            "niche": merged_settings["niche"],
            "keywords": merged_settings["keywords"],
        }
    )


@app.get("/api/trending-topics")
async def get_trending_topics(niche: str = "Technology", keywords: str = "", _admin: dict = Depends(_require_admin)):
    """Get trending topic suggestions for a niche without generating a full blog."""
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    settings = db.get_settings()
    llm_cfg = _build_llm_cfg(settings, gemini_key)
    if llm_cfg.get("provider") not in ("ollama_cloud", "ollama") and not gemini_key:
        raise HTTPException(status_code=400, detail="No LLM key configured. Set GEMINI_API_KEY or OLLAMA_API_KEY in Railway env.")
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, research_trending_topics, niche, keywords, llm_cfg)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ImageGenerateRequest(BaseModel):
    prompt: str
    width: int = 1200
    height: int = 630
    nanobanana_key: str = ""
    nanobanana_url: str = ""

@app.post("/api/images/generate")
def generate_image(req: ImageGenerateRequest, _admin: dict = Depends(_require_admin)):
    """Generate an AI image via Nanobanana (or fallback placeholder)."""
    import urllib.parse
    nanobanana_key = req.nanobanana_key or os.getenv("NANOBANANA_API_KEY", "")
    nanobanana_url = req.nanobanana_url or os.getenv("NANOBANANA_API_URL", "")
    image_url = None

    if nanobanana_key and nanobanana_url:
        try:
            api_endpoint = nanobanana_url.rstrip("/")
            payload = json.dumps({
                "prompt": req.prompt,
                "width": req.width,
                "height": req.height,
            }).encode("utf-8")
            http_req = urllib.request.Request(
                f"{api_endpoint}/generate",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {nanobanana_key}"
                },
                method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=30) as resp:
                resp_data = json.loads(resp.read().decode())
                image_url = resp_data.get("url") or resp_data.get("image_url") or resp_data.get("data", {}).get("url")
        except Exception as e:
            print(f"[image_gen] Nanobanana error: {e}")

    if not image_url:
        encoded = urllib.parse.quote(req.prompt[:80])
        image_url = f"https://placehold.co/{req.width}x{req.height}/0a0e1a/00f0ff?text={encoded}"

    return {"url": image_url, "prompt": req.prompt}


# ── PATCH: update auto-blog settings to include Nanobanana fields ──────────────
# Override the existing AutoBlogSettingsUpdateRequest to include new fields
class AutoBlogSettingsUpdateRequestV2(BaseModel):
    enabled: bool = False
    niche: str = "Agentic AI & Software Development"
    topics: str = ""
    keywords: str = ""
    frequency_hours: int = 24
    author: str = "Danish"
    category: str = "Agentic AI"
    auto_publish: bool = True
    tone: str = "Technical Expert"
    llmProvider: str = "api"
    llmApiName: str = "gemini"
    llmModelName: str = "gemini-2.5-flash"
    llmApiKey: str = ""
    llmBaseUrl: str = ""
    nanobanana_key: str = ""
    nanobanana_url: str = ""

@app.post("/api/system/auto-blog-settings/v2")
def update_auto_blog_settings_v2(req: AutoBlogSettingsUpdateRequestV2, _admin: dict = Depends(_require_admin)):
    existing = db.get_settings()
    updated = {**existing, **req.dict()}
    db.save_settings(updated)
    return updated


# ── ENHANCED GENERATE_AND_POST to use the new pipeline ─────────────────────────
async def generate_and_post_auto_blog_v2(settings: dict):
    """Enhanced auto-blog posting using the full pipeline."""
    # Check both GEMINI_API_KEY and GOOGLE_API_KEY — Railway may set either name.
    gemini_key = (settings.get("llmApiKey") or
                  os.getenv("GEMINI_API_KEY") or
                  os.getenv("GOOGLE_API_KEY", ""))
    ollama_key = os.getenv("OLLAMA_API_KEY") or (
        settings.get("llmApiKey") if (settings.get("llmApiName") or "").lower() in ("ollama_cloud", "ollama") else ""
    )
    if not gemini_key and not ollama_key:
        msg = "Auto-blog skipped: no LLM key found. Set GEMINI_API_KEY on Railway."
        print(f"[auto_blogger] {msg}")
        await manager.broadcast({"type": "blog_error", "message": msg})
        return

    # Pass recently published titles so Stage 1 picks a fresh, unique topic.
    _recent_blogs = db.get_all_blogs()
    _recent_topics = [b["title"] for b in _recent_blogs if b.get("title")][-20:]
    _pub_blogs_v2  = [b for b in _recent_blogs if b.get("published")]

    try:
        loop = asyncio.get_event_loop()
        blog_object = await loop.run_in_executor(
            None, run_auto_blog_pipeline, settings, gemini_key, _recent_topics, _pub_blogs_v2
        )

        # SEO publish gate: surgical revision if pipeline didn't hit 90+.
        min_score = int(os.getenv("BLOG_MIN_PUBLISH_SCORE", "90"))
        score = blog_object.get("seoScore") or 0
        llm_cfg_v2 = _build_llm_cfg(settings, gemini_key)
        if score < min_score:
            try:
                _audit = validate_seo(blog_object)
                stored_rb = blog_object.get("researchBrief") or {}
                rb = {"core_angle": stored_rb.get("core_angle", ""),
                      "lumynor_perspective": stored_rb.get("lumynor_perspective", ""),
                      "main_facts": [], "key_statistics": [], "claims_to_avoid": [], "faqs": []} if stored_rb else {}
                _rev = await loop.run_in_executor(
                    None, revise_blog_from_audit,
                    blog_object, _audit, rb, llm_cfg_v2,
                    os.getenv("TAVILY_API_KEY", ""), 2, _pub_blogs_v2,
                )
                blog_object = _rev["revised_blog"]
                if blog_object.get("content_html") and not blog_object.get("content_markdown") and not blog_object.get("content"):
                    blog_object["content"] = blog_object["content_html"]
                blog_object["seoScore"] = _rev["new_seo_score"]
                blog_object["seoGrade"] = _rev["new_seo_grade"]
                score = _rev["new_seo_score"]
                print(f"[auto_blogger] Post-pipeline revision: {_rev['score_progression']} — final {score}")
            except Exception as _re:
                print(f"[auto_blogger] Post-pipeline revision failed: {_re}")

        # Format: Markdown → branded HTML before saving
        try:
            _sec_imgs_v2 = blog_object.pop("_section_imgs", [])
            blog_object = await loop.run_in_executor(
                None, format_blog_html, blog_object, _sec_imgs_v2, _pub_blogs_v2
            )
        except Exception as _fe:
            print(f"[auto_blogger] HTML formatting failed: {_fe}")
            if blog_object.get("content_markdown") and not blog_object.get("content"):
                blog_object["content"] = blog_object["content_markdown"]

        seo_ok = score >= min_score

        # Image gate: a placehold.co URL means every real image source failed.
        # Don't publish imageless posts — readers expect a cover image.
        cover = blog_object.get("coverImage") or ""
        has_real_image = bool(cover and "placehold.co" not in cover)

        new_blog = {
            "id": str(uuid.uuid4()),
            **blog_object,
            "published": seo_ok and has_real_image,
            "created_at": datetime.utcnow().isoformat(),
            "is_auto_posted": True,
        }
        if not seo_ok:
            new_blog["draftReason"] = f"SEO {score} below publish threshold {min_score}"
            print(f"[auto_blogger] SEO {score} < {min_score} — saved as DRAFT for review")
        elif not has_real_image:
            new_blog["draftReason"] = "No real cover image (all sources failed) — add an image before publishing"
            print(f"[auto_blogger] No real cover image — saved as DRAFT")
        db.insert_blog(new_blog)

        settings["last_run"] = datetime.utcnow().isoformat()
        settings["run_count"] = settings.get("run_count", 0) + 1
        db.save_settings(settings)

        if new_blog.get("published"):
            _fire_publish_webhook(new_blog)
            _send_email_alert(
                f"Published: {new_blog['title']}",
                f"SEO score: {new_blog.get('seoScore')}/100\nSlug: {new_blog.get('slug')}\nURL: /blog/{new_blog.get('slug')}"
            )
        else:
            _send_email_alert(
                f"Draft saved for review: {new_blog.get('title', 'Untitled')}",
                f"Reason: {new_blog.get('draftReason', 'Unknown')}\nSEO score: {new_blog.get('seoScore')}/100\n"
                f"Review queue: /api/blogs/review-queue"
            )

        await manager.broadcast({
            "type": "blog_published",
            "blog": new_blog,
            "message": f"📰 Auto-Blogger published: '{new_blog['title']}' (SEO: {new_blog.get('seoScore')}/100)"
        })
        print(f"[auto_blogger] Published: {new_blog['title']} (SEO {new_blog.get('seoScore')}/100)")
    except Exception as e:
        import traceback
        err_msg = str(e)[:200]
        print(f"[auto_blogger_v2] Pipeline failed: {e}\n{traceback.format_exc()}")
        _send_email_alert("Pipeline failure", f"Auto-blog pipeline failed:\n{err_msg}")
        await manager.broadcast({
            "type": "blog_error",
            "message": f"Auto-blog pipeline failed: {err_msg}"
        })


@app.post("/api/system/trigger-blog")
async def trigger_blog_now(_admin: dict = Depends(_require_admin)):
    """Force the auto-blogger daemon to run on its next tick (within 10 seconds).
    Returns immediately — generation runs in the background and is broadcast via WebSocket."""
    settings = db.get_settings()
    settings["enabled"] = True
    settings["last_run"] = "1970-01-01T00:00:00"
    db.save_settings(settings)
    return {"status": "triggered", "message": "Blog generation will start within 10 seconds. Watch /api/blogs for the new post."}


@app.get("/api/blogs/{blog_id}/audit")
async def audit_blog(blog_id: str, _admin: dict = Depends(_require_admin)):
    """Run SEO + credibility auditors on a saved blog and return both reports."""
    blog = db.get_blog(blog_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog not found")

    tavily_key   = os.getenv("TAVILY_API_KEY", "")
    loop         = asyncio.get_event_loop()
    _audit_content = (blog.get("content_markdown") or blog.get("content_html")
                      or blog.get("content") or "")

    seo_report, cred_report, plag_report = await asyncio.gather(
        loop.run_in_executor(None, validate_seo, blog),
        loop.run_in_executor(None, validate_credibility, blog),
        loop.run_in_executor(None, check_plagiarism, _audit_content, tavily_key),
    )

    # Persist freshly computed scores so the blog manager table stays up to date
    db.patch_blog(blog_id, {
        "seoScore":        seo_report["score"],
        "credibilityScore": cred_report["score"],
    })

    return {
        "blog_id":    blog_id,
        "title":      blog.get("title", ""),
        "published":  blog.get("published", False),
        "seo": {
            "score":           seo_report["score"],
            "grade":           seo_report["grade"],
            "status":          seo_report["status"],
            "word_count":      seo_report.get("word_count", 0),
            "category_scores": seo_report.get("category_scores", {}),
            "issues":          seo_report.get("issues", []),
            "passed":          seo_report.get("passed", []),
            "fixes":           seo_report.get("fixes", []),
            "hard_fails":      seo_report.get("hard_fail_reasons", []),
        },
        "credibility": {
            "score":           cred_report["score"],
            "grade":           cred_report["grade"],
            "status":          cred_report["status"],
            "category_scores": cred_report.get("category_scores", {}),
            "issues":          cred_report.get("issues", []),
            "passed":          cred_report.get("passed", []),
            "fixes":           cred_report.get("fixes", []),
            "hard_fails":      cred_report.get("hard_fail_reasons", []),
            "repeated_sentences": cred_report.get("repeated_sentences", []),
        },
        "plagiarism": {
            "score":        plag_report["score"],
            "status":       plag_report["status"],
            "checked":      plag_report["checked"],
            "flagged_count": plag_report["flagged_count"],
            "flagged":      plag_report["flagged"],
        },
    }


@app.post("/api/blogs/{blog_id}/revise-credibility")
async def revise_blog_credibility_endpoint(blog_id: str, _admin: dict = Depends(_require_admin)):
    """Run up to 3 surgical credibility-rewrite loops on a saved blog post.
    Saves the improved content + score back to the database."""
    blog = db.get_blog(blog_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog not found")

    _cred_settings = db.get_settings()
    _cred_gemini   = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or
                      _cred_settings.get("llmApiKey") or "")
    llm_cfg = _build_llm_cfg(_cred_settings, _cred_gemini)
    loop    = asyncio.get_event_loop()

    cred_report = await loop.run_in_executor(None, validate_credibility, blog)
    initial_score = cred_report["score"]

    if initial_score >= 90 and not cred_report.get("hard_fail_reasons"):
        return {
            "message":             "Already credible — no rewrite needed",
            "initial_score":       initial_score,
            "new_credibility_score": initial_score,
            "score_progression":   [initial_score],
        }

    result = await loop.run_in_executor(
        None, revise_blog_credibility, blog, cred_report, llm_cfg, 3
    )

    revised_blog = result["revised_blog"]
    new_score    = result["new_credibility_score"]
    _rck = ("content_markdown" if revised_blog.get("content_markdown")
            else "content_html" if revised_blog.get("content_html")
            else "content")
    new_content = revised_blog.get(_rck, "")
    if new_content:
        if _rck == "content_markdown":
            # Re-render Markdown → HTML so the published body is never blank
            try:
                _pub_blogs_cred = [b for b in db.get_published_blogs() if b.get("id") != blog_id]
                revised_blog = await loop.run_in_executor(
                    None, format_blog_html, revised_blog, None, _pub_blogs_cred
                )
            except Exception as _fe:
                print(f"[revise-credibility] HTML formatting failed: {_fe}")
                if not revised_blog.get("content"):
                    revised_blog["content"] = revised_blog["content_markdown"]
            patch = {
                "content_markdown": revised_blog.get("content_markdown", ""),
                "content_html":     revised_blog.get("content_html", ""),
                "content":          revised_blog.get("content", ""),
                "credibilityScore": new_score,
            }
        else:
            patch = {_rck: new_content, "credibilityScore": new_score}
        db.patch_blog(blog_id, patch)
    else:
        db.patch_blog(blog_id, {"credibilityScore": new_score})

    return {
        "initial_score":         initial_score,
        "new_credibility_score": new_score,
        "new_credibility_grade": result.get("new_credibility_grade", ""),
        "score_progression":     result.get("score_progression", []),
        "hard_fail_reasons":     result.get("hard_fail_reasons", []),
    }


@app.post("/api/blogs/{blog_id}/revise-plagiarism")
async def revise_blog_plagiarism_endpoint(blog_id: str, _admin: dict = Depends(_require_admin)):
    """Run up to 3 surgical plagiarism-rewrite loops on a saved blog post.
    Flagged sentences are rephrased with original wording. Saves revised content + score."""
    blog = db.get_blog(blog_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog not found")

    tavily_key     = os.getenv("TAVILY_API_KEY", "")
    _plag_settings = db.get_settings()
    _plag_gemini   = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or
                      _plag_settings.get("llmApiKey") or "")
    llm_cfg    = _build_llm_cfg(_plag_settings, _plag_gemini)
    loop       = asyncio.get_event_loop()

    _plag_content = (blog.get("content_markdown") or blog.get("content_html")
                     or blog.get("content") or "")
    plag_report = await loop.run_in_executor(None, check_plagiarism, _plag_content, tavily_key)
    initial_score = plag_report["score"]

    if initial_score >= 90:
        db.patch_blog(blog_id, {"plagiarismScore": initial_score})
        return {
            "message":              "Already original — no rewrite needed",
            "initial_score":        initial_score,
            "new_plagiarism_score": initial_score,
            "score_progression":    [initial_score],
        }

    result = await loop.run_in_executor(
        None, revise_blog_plagiarism, blog, plag_report, llm_cfg, tavily_key, 3
    )

    revised_blog = result["revised_blog"]
    new_score    = result["new_plagiarism_score"]
    _rck = ("content_markdown" if revised_blog.get("content_markdown")
            else "content_html" if revised_blog.get("content_html")
            else "content")
    new_content = revised_blog.get(_rck, "")
    if new_content:
        if _rck == "content_markdown":
            # Re-render Markdown → HTML so the published body is never blank
            try:
                _pub_blogs_plag = [b for b in db.get_published_blogs() if b.get("id") != blog_id]
                revised_blog = await loop.run_in_executor(
                    None, format_blog_html, revised_blog, None, _pub_blogs_plag
                )
            except Exception as _fe:
                print(f"[revise-plagiarism] HTML formatting failed: {_fe}")
                if not revised_blog.get("content"):
                    revised_blog["content"] = revised_blog["content_markdown"]
            patch = {
                "content_markdown": revised_blog.get("content_markdown", ""),
                "content_html":     revised_blog.get("content_html", ""),
                "content":          revised_blog.get("content", ""),
                "plagiarismScore":  new_score,
            }
        else:
            patch = {_rck: new_content, "plagiarismScore": new_score}
        db.patch_blog(blog_id, patch)
    else:
        db.patch_blog(blog_id, {"plagiarismScore": new_score})

    return {
        "initial_score":        initial_score,
        "new_plagiarism_score": new_score,
        "score_progression":    result.get("score_progression", []),
        "flagged_remaining":    result.get("flagged", []),
    }


@app.post("/api/blogs/{blog_id}/format")
async def format_blog_html_endpoint(blog_id: str, _admin: dict = Depends(_require_admin)):
    """Convert content_markdown → branded HTML for Preview or Publish.
    Saves content_html + content back to the blog. Safe to call on already-HTML blogs (no-op)."""
    blog = db.get_blog(blog_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog not found")

    loop = asyncio.get_event_loop()
    published_blogs = db.get_published_blogs()

    formatted = await loop.run_in_executor(None, format_blog_html, blog, None, published_blogs)

    html = formatted.get("content_html", "") or formatted.get("content", "")
    if html:
        db.patch_blog(blog_id, {"content_html": html, "content": html})

    return {"content_html": html, "formatted": bool(html)}


# ── Affiliate Links ────────────────────────────────────────────────────────────

class AffiliateLinkCreate(BaseModel):
    keyword: str
    url: str

class AffiliateLinkUpdate(BaseModel):
    keyword: str | None = None
    url: str | None = None
    is_active: bool | None = None

@app.get("/api/affiliate")
async def list_affiliate_links(_admin: dict = Depends(_require_admin)):
    """List all affiliate links with click stats."""
    return db.get_affiliate_stats()

@app.post("/api/affiliate")
async def create_affiliate_link(req: AffiliateLinkCreate, _admin: dict = Depends(_require_admin)):
    if not req.keyword.strip() or not req.url.strip():
        raise HTTPException(status_code=400, detail="keyword and url are required")
    return db.create_affiliate_link(req.keyword, req.url)

@app.patch("/api/affiliate/{link_id}")
async def update_affiliate_link(link_id: str, req: AffiliateLinkUpdate, _admin: dict = Depends(_require_admin)):
    fields = {k: v for k, v in req.dict().items() if v is not None}
    updated = db.update_affiliate_link(link_id, fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Affiliate link not found")
    return updated

@app.delete("/api/affiliate/{link_id}")
async def delete_affiliate_link(link_id: str, _admin: dict = Depends(_require_admin)):
    db.delete_affiliate_link(link_id)
    return {"status": "deleted"}

@app.get("/api/affiliate/click/{link_id}")
async def track_affiliate_click(link_id: str, blog_id: str = "", blog_slug: str = ""):
    """Record click and redirect to the affiliate URL."""
    links = db.get_affiliate_links()
    link = next((l for l in links if l["id"] == link_id), None)
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    db.log_affiliate_click(link_id, blog_id, blog_slug)
    return RedirectResponse(url=link["url"], status_code=302)

@app.post("/api/blogs/{blog_id}/toggle-affiliates")
async def toggle_blog_affiliates(blog_id: str, _admin: dict = Depends(_require_admin)):
    """Enable or disable affiliate link injection on a specific blog.
    When enabling: injects active affiliate links into the blog HTML.
    When disabling: strips all affiliate links, restoring plain text."""
    blog = db.get_blog(blog_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog not found")

    currently_on = bool(blog.get("affiliate_links_enabled"))
    new_state = not currently_on
    html = blog.get("content") or blog.get("content_html") or ""

    if new_state:
        affiliate_links = db.get_affiliate_links()
        slug = blog.get("slug", "")
        html = strip_affiliate_links(html)  # remove any stale injections first
        html = inject_affiliate_links(html, affiliate_links, blog_id=blog_id, blog_slug=slug)
    else:
        html = strip_affiliate_links(html)

    db.patch_blog(blog_id, {
        "content": html,
        "content_html": html,
        "affiliate_links_enabled": new_state,
    })
    return {"affiliate_links_enabled": new_state, "blog_id": blog_id}


@app.post("/api/blogs/{blog_id}/revise")
async def revise_saved_blog(blog_id: str, _admin: dict = Depends(_require_admin)):
    """Run targeted content revision automation on a saved blog post.
    Classifies SEO issues into buckets → applies minimum surgical fix per bucket
    → re-audits → loops up to 2 times.
    Source issues trigger Tavily re-research only (no LLM fabrication).
    Returns revision notes, score progression, and publish recommendation.
    """
    blog = db.get_blog(blog_id)
    if not blog:
        raise HTTPException(status_code=404, detail="Blog not found")

    # Rate-limit: one revision per blog per hour to prevent runaway LLM spend.
    last_revised = blog.get("revised_at")
    if last_revised:
        try:
            delta = (datetime.utcnow() - datetime.fromisoformat(last_revised)).total_seconds()
            if delta < 3600:
                raise HTTPException(
                    status_code=429,
                    detail=f"Blog was revised {int(delta // 60)}m ago — wait 1 hour before revising again."
                )
        except HTTPException:
            raise
        except Exception:
            pass  # malformed date — allow revision

    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    settings = db.get_settings()
    llm_cfg = _build_llm_cfg(settings, gemini_key)
    if llm_cfg.get("provider") not in ("ollama_cloud", "ollama") and not gemini_key:
        raise HTTPException(status_code=400,
                            detail="No LLM key configured. Set GEMINI_API_KEY or OLLAMA_API_KEY.")

    # Run initial audit on the stored blog
    initial_audit = validate_seo(blog)

    # Reconstruct a compact research brief from what was stored with the blog
    stored_rb = blog.get("researchBrief") or {}
    research_brief = {
        "core_angle":        stored_rb.get("core_angle", ""),
        "lumynor_perspective": stored_rb.get("lumynor_perspective", ""),
        "main_facts":        [],
        "key_statistics":    [],
        "claims_to_avoid":   [],
        "faqs":              [],
    } if stored_rb else {}

    _pub_blogs_rev = [b for b in db.get_published_blogs() if b.get("id") != blog_id]

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, revise_blog_from_audit,
            blog, initial_audit, research_brief, llm_cfg,
            os.getenv("TAVILY_API_KEY", ""), 2, _pub_blogs_rev,
        )
    except Exception as e:
        import traceback
        print(f"[revise] {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Revision failed: {str(e)[:200]}")

    revised = result["revised_blog"]

    # Re-format Markdown → HTML so the published blog body is never blank after revision
    if revised.get("content_markdown"):
        try:
            _pub_blogs_fmt = [b for b in db.get_published_blogs() if b.get("id") != blog_id]
            revised = await loop.run_in_executor(
                None, format_blog_html, revised, None, _pub_blogs_fmt
            )
        except Exception as _fe:
            print(f"[revise] HTML formatting failed: {_fe}")
            if not revised.get("content"):
                revised["content"] = revised["content_markdown"]

    # Persist the revised blog back to Supabase
    cover = blog.get("coverImage", "")
    _rev_ck = ("content_markdown" if revised.get("content_markdown")
               else "content_html" if revised.get("content_html")
               else "content")
    _content_patch: dict = {}
    if _rev_ck == "content_markdown":
        # Save all three: Markdown source + rendered HTML (from format_blog_html above)
        _content_patch = {
            "content_markdown": revised.get("content_markdown", ""),
            "content_html":     revised.get("content_html", ""),
            "content":          revised.get("content", ""),
        }
    elif _rev_ck == "content_html":
        # Sync content_html → content so the frontend always reads the revised version
        _content_patch = {
            "content_html": revised.get("content_html", ""),
            "content":      revised.get("content_html", ""),
        }
    else:
        _content_patch = {"content": revised.get("content", blog.get("content", ""))}
    db.update_blog(blog["id"], {
        **blog,
        "title":           revised.get("title", blog["title"]),
        "slug":            revised.get("slug", blog.get("slug", "")),
        "summary":         revised.get("summary", blog.get("summary", "")),
        "metaDescription": revised.get("meta_description", blog.get("metaDescription", "")),
        "references":      revised.get("references", blog.get("references", [])),
        "seoScore":        result["new_seo_score"],
        "seoGrade":        result["new_seo_grade"],
        "published": (
            result["publish_recommendation"] == "publish"
            and bool(cover and "placehold.co" not in cover)
        ),
        "revised_at":      datetime.utcnow().isoformat(),
        "revision_log":    result["revision_notes"],
        **_content_patch,
    })

    return {
        "status":                 "success",
        "blog_id":                blog_id,
        "initial_seo_score":      initial_audit["score"],
        "initial_seo_grade":      initial_audit["grade"],
        "new_seo_score":          result["new_seo_score"],
        "new_seo_grade":          result["new_seo_grade"],
        "score_progression":      result["score_progression"],
        "loop_diffs":             result.get("loop_diffs", []),
        "publish_recommendation": result["publish_recommendation"],
        "verdict":                result["verdict"],
        "revision_notes":         result["revision_notes"],
        "remaining_issues":       result["remaining_issues"],
        "hard_fail_reasons":      result["hard_fail_reasons"],
    }


@app.post("/api/system/migrate-json-to-supabase")
def migrate_json_to_supabase(_admin: dict = Depends(_require_admin)):
    """One-time migration: copy blogs.json and leads.json into Supabase.
    Safe to run multiple times — skips rows that already exist by slug/email."""
    if not db.is_connected():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    results = {"blogs_inserted": 0, "blogs_skipped": 0, "leads_inserted": 0, "leads_skipped": 0, "errors": []}

    # ── Blogs ──────────────────────────────────────────────────────────────────
    old_blogs = read_json_file(BLOGS_FILE, [])
    for b in old_blogs:
        if not b.get("id") or not b.get("title"):
            continue
        try:
            # Ensure slug
            slug = b.get("slug") or re.sub(r'[^a-z0-9\s-]', '', b["title"].lower())
            slug = re.sub(r'[\s-]+', '-', slug).strip('-') or b["id"]
            b["slug"] = slug

            # Check duplicate by slug
            existing = db.get_blog(slug)
            if existing:
                results["blogs_skipped"] += 1
                continue

            db.insert_blog(b)
            results["blogs_inserted"] += 1
        except Exception as e:
            results["errors"].append(f"blog {b.get('id','?')}: {str(e)[:100]}")

    # ── Leads ──────────────────────────────────────────────────────────────────
    old_leads = read_json_file(LEADS_FILE, [])
    for lead in old_leads:
        if not lead.get("email"):
            continue
        try:
            if db.lead_exists(lead["email"]):
                results["leads_skipped"] += 1
                continue
            db.insert_lead(lead)
            results["leads_inserted"] += 1
        except Exception as e:
            results["errors"].append(f"lead {lead.get('email','?')}: {str(e)[:100]}")

    return {"status": "done", **results}


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_blogger_daemon())
    asyncio.create_task(weekly_intel_daemon())
    asyncio.create_task(digest_daemon())
    asyncio.create_task(atlas_proactive_daemon())
    asyncio.create_task(lumy_reminder_daemon())
    asyncio.create_task(whatsapp_session_keepalive_daemon())


async def digest_daemon():
    """Auto-send morning digest at configured UTC time (default 02:30 UTC = 08:00 IST).

    Uses a 15-minute catch-up window so a Railway restart shortly after the
    scheduled time doesn't silently skip the whole day.
    """
    import asyncio as _aio
    from datetime import datetime, timezone
    await _aio.sleep(90)
    last_sent_date: str | None = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            stored = db.get_settings("digest")
            send_hour   = int(stored.get("send_hour_utc", 2))
            send_minute = int(stored.get("send_minute", 30))
            to_number   = (stored.get("digestTo") or "").strip()
            today = now.date().isoformat()
            # Catch-up window: fire any time in [scheduled, scheduled+15min)
            # so a container restart right after the scheduled minute still sends.
            sched = send_hour * 60 + send_minute
            curr  = now.hour * 60 + now.minute
            if sched <= curr < sched + 15 and last_sent_date != today and to_number:
                print(f"[digest] Auto-sending morning digest for {today} (curr={curr}, sched={sched})")
                from digest import send_digest
                result = send_digest()
                ok = bool(result.get("ok"))
                db.save_settings({**stored,
                    "last_digest_at":    now.isoformat(),
                    "last_digest_ok":    ok,
                    "last_digest_error": "" if ok else str(result.get("error", "Unknown error"))[:300],
                }, "digest")
                # Only mark the day as handled on success — a failed send (e.g. Twilio
                # WhatsApp session window expired) should retry on the next tick within
                # this 15-min catch-up window instead of being silently skipped for the day.
                if ok:
                    last_sent_date = today
                print(f"[digest] Result: {result}")
        except Exception as e:
            print(f"[digest] daemon error: {e}")
        await _aio.sleep(60)


async def atlas_proactive_daemon():
    """Send ATLAS evening check-in message at configured UTC time (default 14:30 UTC = 20:00 IST).

    Uses a 15-minute catch-up window — same rationale as digest_daemon.
    """
    import asyncio as _aio
    from datetime import datetime, timezone
    await _aio.sleep(120)
    last_sent_date: str | None = None
    while True:
        try:
            now    = datetime.now(timezone.utc)
            stored = db.get_settings("atlas")
            send_hour   = int(stored.get("proactive_hour_utc", 14))
            send_minute = int(stored.get("proactive_minute", 30))
            enabled     = stored.get("proactive_enabled", True)
            to_number   = (db.get_settings("digest").get("digestTo") or "").strip()
            today = now.date().isoformat()
            sched = send_hour * 60 + send_minute
            curr  = now.hour * 60 + now.minute
            if enabled and sched <= curr < sched + 15 and last_sent_date != today and to_number:
                print(f"[atlas] Sending evening proactive message for {today} (curr={curr}, sched={sched})")
                result = ab.run_proactive_check()
                ok = bool(result.get("ok"))
                db.save_settings({**stored,
                    "last_proactive_at":    now.isoformat(),
                    "last_proactive_ok":    ok,
                    "last_proactive_error": "" if ok else str(result.get("error", "Unknown error"))[:300],
                }, "atlas")
                # Only mark the day as handled on success — a failed send (e.g. Twilio
                # WhatsApp session window expired, sandbox not joined) should retry on
                # the next tick within this 15-min window instead of going silent for the day.
                if ok:
                    last_sent_date = today
                print(f"[atlas] Result: {result.get('ok')} | type={result.get('situation', {}).get('msg_type')}")
        except Exception as e:
            print(f"[atlas] daemon error: {e}")
        await _aio.sleep(60)


async def weekly_intel_daemon():
    """Auto-generate weekly report every Monday if not yet produced for this week."""
    import asyncio as _aio
    await _aio.sleep(30)  # let app fully start
    while True:
        try:
            if wi.should_auto_generate():
                print("[weekly_intel] Monday detected — auto-generating weekly report")
                wi.generate_weekly_report()
                print("[weekly_intel] Weekly report generated")
        except Exception as e:
            print(f"[weekly_intel] auto-generate error: {e}")
        await _aio.sleep(3600)  # check hourly

# ── Revenue Radar OS — Phase 2 ────────────────────────────────────────────────
import revenue_radar as rr

# Leads
@app.get("/api/revenue/leads")
def revenue_leads_list(
    product: str = None, temperature: str = None,
    status: str = None, category: str = None, limit: int = 100,
    user=Depends(_require_admin)
):
    return rr.get_leads(product=product, temperature=temperature,
                        status=status, category=category, limit=limit)

@app.post("/api/revenue/leads")
def revenue_lead_create(body: dict, user=Depends(_require_admin)):
    lead = rr.create_lead(body, source='manual')
    if not lead:
        raise HTTPException(status_code=500, detail="Failed to create lead")
    return lead

@app.patch("/api/revenue/leads/{lead_id}")
def revenue_lead_update(lead_id: str, body: dict, user=Depends(_require_admin)):
    return rr.update_lead(lead_id, body)

@app.delete("/api/revenue/leads/{lead_id}")
def revenue_lead_delete(lead_id: str, user=Depends(_require_admin)):
    rr.delete_lead(lead_id)
    return {"ok": True}

@app.post("/api/revenue/leads/{lead_id}/approve")
def revenue_lead_approve(lead_id: str, user=Depends(_require_admin)):
    return rr.approve_lead(lead_id)

@app.post("/api/revenue/leads/{lead_id}/reject")
def revenue_lead_reject(lead_id: str, user=Depends(_require_admin)):
    return rr.reject_lead(lead_id)

# Auto-discovery
@app.post("/api/revenue/discover")
def revenue_discover(body: dict, user=Depends(_require_admin)):
    product  = body.get("product", "")
    category = body.get("category", "")
    limit    = int(body.get("limit", 10))
    location = body.get("location", "")
    return rr.run_auto_discovery(product, category, limit, location)

@app.post("/api/revenue/rescore")
def revenue_rescore(user=Depends(_require_admin)):
    return rr.rescore_all_leads()

# Contacts
@app.get("/api/revenue/contacts")
def revenue_contacts_list(lead_id: str, user=Depends(_require_admin)):
    return rr.get_contacts(lead_id)

@app.post("/api/revenue/contacts")
def revenue_contact_create(body: dict, user=Depends(_require_admin)):
    return rr.create_contact(body)

@app.patch("/api/revenue/contacts/{contact_id}")
def revenue_contact_update(contact_id: str, body: dict, user=Depends(_require_admin)):
    return rr.update_contact(contact_id, body)

@app.delete("/api/revenue/contacts/{contact_id}")
def revenue_contact_delete(contact_id: str, user=Depends(_require_admin)):
    rr.delete_contact(contact_id)
    return {"ok": True}

# Market Radar
@app.get("/api/revenue/signals")
def revenue_signals_list(product: str = None, user=Depends(_require_admin)):
    return rr.get_signals(product=product)

@app.post("/api/revenue/signals/scan")
def revenue_signals_scan(body: dict, user=Depends(_require_admin)):
    product = body.get("product", "all")
    return rr.run_market_scan(product)

@app.delete("/api/revenue/signals/{signal_id}")
def revenue_signal_dismiss(signal_id: str, user=Depends(_require_admin)):
    rr.dismiss_signal(signal_id)
    return {"ok": True}

# Launch Readiness
@app.get("/api/revenue/readiness")
def revenue_readiness(user=Depends(_require_admin)):
    return rr.get_launch_readiness()

# Intelligence: Funding + Market Trends
@app.get("/api/revenue/intelligence")
def revenue_intelligence_list(
    product: str = None, type: str = None,
    user=Depends(_require_admin)
):
    return rr.get_intelligence(product=product, scan_type=type)

@app.post("/api/revenue/intelligence/scan")
def revenue_intelligence_scan(body: dict, user=Depends(_require_admin)):
    product   = body.get("product", "all")
    scan_type = body.get("type", "all")
    return rr.run_intelligence_scan(product, scan_type)

@app.delete("/api/revenue/intelligence/{item_id}")
def revenue_intelligence_dismiss(item_id: str, user=Depends(_require_admin)):
    rr.dismiss_intelligence(item_id)
    return {"ok": True}

# Discovery query catalogue (for frontend dropdowns)
@app.get("/api/revenue/catalogue")
def revenue_catalogue(user=Depends(_require_admin)):
    return {
        prod: {
            "name": meta["name"],
            "categories": meta["categories"],
            "category_queries": list(rr.DISCOVERY_QUERIES.get(prod, {}).keys()),
        }
        for prod, meta in rr.PRODUCT_META.items()
    }


# ── Wire Lumy's WhatsApp orchestrator to the real backend agents ───────────────
# Read-only / contained actions run immediately; anything that touches the live
# site (publishing) requires a "haan"/"yes" confirmation reply first.
def _trigger_blog_tool(_params: dict) -> dict:
    settings = db.get_settings()
    settings["enabled"] = True
    settings["last_run"] = "1970-01-01T00:00:00"
    db.save_settings(settings)
    return {"status": "triggered"}


def _write_blog_tool(params: dict) -> dict:
    """Write a new blog with optional topic/keyword override, fire as background task."""
    settings = db.get_settings()
    topic   = (params.get("topic") or "").strip()
    keyword = (params.get("keyword") or "").strip()
    publish = bool(params.get("publish", False))

    merged = {
        "niche":    settings.get("niche", "Technology"),
        "keywords": keyword or settings.get("topics", ""),
        "author":   settings.get("author", "Lumynor Team"),
        "category": settings.get("category", "Technology"),
        "auto_publish": publish,
        "nanobanana_key": settings.get("nanobanana_key", ""),
        "nanobanana_url": settings.get("nanobanana_url", ""),
        "image_source":   settings.get("image_source", "web"),
        "unsplash_key":   settings.get("unsplash_key", ""),
        "pexels_key":     settings.get("pexels_key", ""),
    }
    if topic:
        merged["niche"] = topic

    gemini_key = settings.get("llmApiKey") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    if not gemini_key:
        return {"error": "No LLM key configured"}

    llm_cfg       = _build_llm_cfg(merged, gemini_key)
    all_blogs_now = db.get_all_blogs()
    pub_blogs     = [b for b in all_blogs_now if b.get("published")]
    recent_titles = [b["title"] for b in all_blogs_now if b.get("title")][-20:]

    import asyncio as _asyncio
    try:
        loop = _asyncio.get_event_loop()
        loop.create_task(_auto_generate_bg(merged, gemini_key, llm_cfg, publish, settings, pub_blogs, recent_titles))
    except RuntimeError:
        pass
    return {"status": "started", "topic": topic or merged["niche"], "publish": publish}


def _find_blog(params: dict) -> dict | None:
    blogs = db.get_all_blogs()
    slug  = (params.get("blog_slug") or "").strip()
    title = (params.get("title_contains") or "").strip().lower()
    if slug:
        match = next((b for b in blogs if b.get("slug") == slug), None)
        if match:
            return match
    if title:
        return next((b for b in blogs if title in (b.get("title") or "").lower()), None)
    return None


def _publish_blog_tool(params: dict) -> dict:
    blog = _find_blog(params)
    if not blog:
        return {"error": "Blog not found — check the title or slug."}
    db.patch_blog(blog["id"], {"published": True})
    return {"status": "published", "title": blog.get("title"), "slug": blog.get("slug")}


def _unpublish_blog_tool(params: dict) -> dict:
    blog = _find_blog(params)
    if not blog:
        return {"error": "Blog not found — check the title or slug."}
    db.patch_blog(blog["id"], {"published": False})
    return {"status": "unpublished", "title": blog.get("title")}


def _list_blogs_tool(_params: dict) -> dict:
    blogs = db.get_all_blogs()
    summary = [
        {"title": b.get("title"), "slug": b.get("slug"), "published": b.get("published"), "seoScore": b.get("seoScore")}
        for b in blogs[:15]
    ]
    return {"count": len(blogs), "blogs": summary}


def _add_affiliate_tool(params: dict) -> dict:
    keyword = (params.get("keyword") or "").strip()
    url     = (params.get("url") or "").strip()
    if not keyword or not url:
        return {"error": "Both keyword and url are required."}
    link = db.create_affiliate_link(keyword, url)
    return {"status": "added", "keyword": keyword, "url": url, "id": link.get("id")}


def _remove_affiliate_tool(params: dict) -> dict:
    keyword = (params.get("keyword") or "").strip().lower()
    if not keyword:
        return {"error": "keyword is required."}
    links   = db.get_affiliate_links()
    matched = [l for l in links if keyword in l["keyword"].lower()]
    if not matched:
        return {"error": f"No affiliate link found matching '{keyword}'."}
    for l in matched:
        db.delete_affiliate_link(l["id"])
    return {"status": "removed", "count": len(matched), "keywords": [l["keyword"] for l in matched]}


def _ist(dt_iso: str) -> str:
    """Format a stored UTC ISO timestamp as a readable IST string."""
    try:
        dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
        ist = dt + timedelta(hours=5, minutes=30)
        return ist.strftime("%a %d %b, %I:%M %p IST")
    except Exception:
        return dt_iso


def _set_reminder_tool(params: dict) -> dict:
    text   = (params.get("text") or "").strip()
    due_at = (params.get("due_at") or "").strip()
    if not text or not due_at:
        return {"error": "Both text and due_at are required."}
    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except Exception:
        return {"error": f"Could not parse time '{due_at}'."}
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if due < datetime.now(timezone.utc):
        return {"error": "That time is already in the past, jaan — give me a future time."}
    row = db.create_lumy_reminder(text, due.isoformat())
    return {"status": "set", "title": f"{text} — {_ist(row['due_at'])}"}


def _list_reminders_tool(_params: dict) -> dict:
    pending = db.get_pending_lumy_reminders()
    if not pending:
        return {"status": "empty", "title": "koi pending reminder nahi hai"}
    lines = [f"{i+1}. {r['text']} — {_ist(r['due_at'])}" for i, r in enumerate(pending[:10])]
    return {"status": "listed", "count": len(pending), "title": "; ".join(lines)}


def _cancel_reminder_tool(params: dict) -> dict:
    phrase = (params.get("text_contains") or "").strip()
    if not phrase:
        return {"error": "Tell me which reminder to cancel (a phrase from it)."}
    cancelled = db.cancel_lumy_reminders_matching(phrase)
    if not cancelled:
        return {"error": f"No pending reminder matching '{phrase}'."}
    return {"status": "cancelled", "count": len(cancelled),
            "title": "; ".join(r["text"] for r in cancelled)}


async def lumy_reminder_daemon():
    """Deliver due reminders to WhatsApp. Checks every 60s; unsent+overdue rows are
    picked up on the next tick after any restart, so nothing is silently lost."""
    import asyncio as _aio
    await _aio.sleep(60)
    while True:
        try:
            for r in db.get_due_lumy_reminders():
                result = ab.send_atlas_message(f"⏰ Reminder, jaan: {r['text']}\n— Lumy ❤️")
                if result.get("ok"):
                    db.mark_lumy_reminder_sent(r["id"])
                    print(f"[reminders] delivered: {r['text'][:60]}")
                else:
                    print(f"[reminders] send failed (will retry next tick): {result.get('error')}")
        except Exception as e:
            print(f"[reminders] daemon error: {e}")
        await _aio.sleep(60)


async def whatsapp_session_keepalive_daemon():
    """Twilio's WhatsApp free-form session closes 24h after the founder's last
    inbound message — after that, Lumy can't send anything until he texts her
    again. This warns him 2h before that window closes, once per window, so he
    can send the sandbox join code and keep the line open. Checks every 10 min —
    a 2h warning doesn't need 60s precision."""
    import asyncio as _aio
    await _aio.sleep(90)
    while True:
        try:
            sess = db.get_settings("whatsapp_session")
            last_inbound_raw = sess.get("last_inbound_at")
            if last_inbound_raw:
                last_inbound  = datetime.fromisoformat(last_inbound_raw)
                window_close  = last_inbound + timedelta(hours=24)
                remind_at     = window_close - timedelta(hours=2)
                now           = datetime.utcnow()
                already_sent  = sess.get("last_keepalive_reminder_for") == last_inbound_raw
                if remind_at <= now < window_close and not already_sent:
                    join_code = sess.get("sandbox_join_code", "gulf-obtain")
                    text = (
                        f"⏰ Jaan, humari WhatsApp session 2 ghante mein band ho jayegi. "
                        f"Send *join {join_code}* abhi is chat mein, warna main tumhe reply nahi kar paungi.\n— Lumy ❤️"
                    )
                    result = ab.send_atlas_message(text)
                    if result.get("ok"):
                        db.save_settings({**sess, "last_keepalive_reminder_for": last_inbound_raw}, "whatsapp_session")
                        print("[whatsapp_session] keepalive reminder sent")
                    else:
                        print(f"[whatsapp_session] keepalive send failed (will retry next tick): {result.get('error')}")
        except Exception as e:
            print(f"[whatsapp_session] daemon error: {e}")
        await _aio.sleep(600)


ab.register_tools({
    "design_audit": {
        "label": "Design Audit",
        "description": "Run a design/UX audit on a Lumynor page and save the report. Params: url (defaults to lumynorsystems.com).",
        "fn": lambda p: da.save_audit(da.run_audit(
            p.get("url") or "https://lumynorsystems.com",
            [p.get("url") or "https://lumynorsystems.com"],
        )),
        "high_impact": False,
    },
    "authority_scan": {
        "label": "Authority Scan",
        "description": "Scan recent activity for publishable story/content opportunities.",
        "fn": lambda p: auth.scan_opportunities(),
        "high_impact": False,
    },
    "weekly_report": {
        "label": "Weekly Intelligence Report",
        "description": "Generate this week's strategic intelligence report.",
        "fn": lambda p: wi.generate_weekly_report(),
        "high_impact": False,
    },
    "revenue_scan": {
        "label": "Revenue Signal Scan",
        "description": "Scan for new revenue/lead signals. Params: product (defaults to 'all').",
        "fn": lambda p: rr.run_market_scan(p.get("product", "all")),
        "high_impact": False,
    },
    "trigger_blog": {
        "label": "Publish New Blog Post",
        "description": "Research, write, and PUBLISH a new blog post live on the website (runs within ~10s).",
        "fn": _trigger_blog_tool,
        "high_impact": True,
    },
    "write_blog": {
        "label": "Write Blog Post",
        "description": "Research and write a new blog post on a specific topic/keyword. Params: topic (required), keyword (optional), publish (true=live, false=draft). Default is draft.",
        "fn": _write_blog_tool,
        "high_impact": True,
    },
    "publish_blog": {
        "label": "Publish Blog",
        "description": "Make an existing blog post live on the website. Params: title_contains (partial title search) or blog_slug.",
        "fn": _publish_blog_tool,
        "high_impact": True,
    },
    "unpublish_blog": {
        "label": "Unpublish Blog",
        "description": "Take a blog post offline (back to draft). Params: title_contains or blog_slug.",
        "fn": _unpublish_blog_tool,
        "high_impact": True,
    },
    "list_blogs": {
        "label": "List Blog Posts",
        "description": "Show all blog posts with their titles, slugs, publish status, and SEO scores.",
        "fn": _list_blogs_tool,
        "high_impact": False,
    },
    "add_affiliate": {
        "label": "Add Affiliate Link",
        "description": "Add a keyword→URL affiliate link that auto-injects into blogs. Params: keyword, url.",
        "fn": _add_affiliate_tool,
        "high_impact": False,
    },
    "remove_affiliate": {
        "label": "Remove Affiliate Link",
        "description": "Remove an affiliate link by keyword. Params: keyword.",
        "fn": _remove_affiliate_tool,
        "high_impact": False,
    },
    "set_reminder": {
        "label": "Set Reminder",
        "description": "Set a reminder that Lumy delivers to WhatsApp at the given time. Params: text, due_at (ISO 8601 UTC).",
        "fn": _set_reminder_tool,
        "high_impact": False,
    },
    "list_reminders": {
        "label": "List Reminders",
        "description": "Show all pending reminders with their delivery times.",
        "fn": _list_reminders_tool,
        "high_impact": False,
    },
    "cancel_reminder": {
        "label": "Cancel Reminder",
        "description": "Cancel a pending reminder. Params: text_contains (a phrase from the reminder).",
        "fn": _cancel_reminder_tool,
        "high_impact": False,
    },
})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

