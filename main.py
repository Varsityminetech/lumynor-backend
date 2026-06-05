import asyncio
import os
import uuid
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from auth import (
    seed_users, seed_audit, authenticate_user, create_access_token,
    decode_token, append_audit_log, get_audit_logs, update_user_credentials
)
from fastapi.responses import FileResponse
from agent_graph import company_app, set_broadcast_callback, set_raw_broadcast_callback
from exporter import markdown_to_docx, markdown_to_pptx

app = FastAPI(title="Lumynor Systems Engine")

# ── EXPORT ENDPOINTS ──────────────────────────────────────────────────────────
@app.get("/export/docx")
async def export_docx():
    content = manager.pipeline_state.get("current_document", "No content available.")
    path = os.path.join(os.path.dirname(__file__), "export.docx")
    markdown_to_docx(content, path)
    return FileResponse(path, filename="Lumynor_Report.docx")

@app.get("/export/pptx")
async def export_pptx():
    content = manager.pipeline_state.get("current_document", "No content available.")
    path = os.path.join(os.path.dirname(__file__), "export.pptx")
    markdown_to_pptx(content, path)
    return FileResponse(path, filename="Lumynor_Presentation.pptx")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

seed_users()
seed_audit()

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
def login(req: LoginRequest):
    user = authenticate_user(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": user["email"], "name": user["name"], "role": user["role"]})
    return {"access_token": token, "token_type": "bearer",
            "user": {"email": user["email"], "name": user["name"], "role": user["role"]}}

@app.get("/audit/logs")
def audit_logs():
    return get_audit_logs()

from fastapi import Depends, Header

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
def subscribe_lead(req: LeadSubscribeRequest):
    leads = read_json_file(LEADS_FILE, [])
    # Check if duplicate
    if any(lead.get("email") == req.email for lead in leads):
        return {"status": "success", "message": "Already subscribed."}
    new_lead = {
        "id": str(uuid.uuid4()),
        "name": req.name,
        "email": req.email,
        "source": req.source,
        "timestamp": datetime.utcnow().isoformat()
    }
    leads.append(new_lead)
    write_json_file(LEADS_FILE, leads)
    return {"status": "success", "lead": new_lead}

@app.post("/api/leads/chat")
def chat_lead(req: dict):
    leads = read_json_file(LEADS_FILE, [])
    new_lead = {
        "id": str(uuid.uuid4()),
        "source": req.get("source", "chatbot"),
        "query": req.get("query", ""),
        "features": req.get("features", ""),
        "budget": req.get("budget", ""),
        "contact": req.get("contact", ""),
        "timestamp": datetime.utcnow().isoformat()
    }
    leads.append(new_lead)
    write_json_file(LEADS_FILE, leads)
    return {"status": "success", "lead": new_lead}

@app.get("/api/leads/all")
def get_leads():
    return read_json_file(LEADS_FILE, [])

@app.get("/api/blogs")
def get_published_blogs():
    blogs = read_json_file(BLOGS_FILE, [])
    return [b for b in blogs if b.get("published", True)]

@app.get("/api/blogs/admin")
def get_all_blogs():
    return read_json_file(BLOGS_FILE, [])

@app.get("/api/blogs/{slug_or_id}")
def get_single_blog(slug_or_id: str):
    blogs = read_json_file(BLOGS_FILE, [])
    # Look by slug first, then ID
    for blog in blogs:
        if blog.get("slug") == slug_or_id or blog.get("id") == slug_or_id:
            comments = read_json_file(COMMENTS_FILE, {}).get(blog.get("id"), [])
            blog_copy = dict(blog)
            blog_copy["comments"] = comments
            return blog_copy
    raise HTTPException(status_code=404, detail="Blog post not found")

@app.post("/api/blogs")
def create_blog(req: BlogSaveRequest):
    blogs = read_json_file(BLOGS_FILE, [])
    new_blog = req.dict()
    new_blog["id"] = str(uuid.uuid4())
    new_blog["created_at"] = datetime.utcnow().isoformat()
    blogs.append(new_blog)
    write_json_file(BLOGS_FILE, blogs)
    return {"status": "success", "blog": new_blog}

@app.put("/api/blogs/{blog_id}")
def update_blog(blog_id: str, req: BlogSaveRequest):
    blogs = read_json_file(BLOGS_FILE, [])
    for idx, blog in enumerate(blogs):
        if blog.get("id") == blog_id:
            updated = req.dict()
            updated["id"] = blog_id
            updated["created_at"] = blog.get("created_at", datetime.utcnow().isoformat())
            updated["updated_at"] = datetime.utcnow().isoformat()
            blogs[idx] = updated
            write_json_file(BLOGS_FILE, blogs)
            return {"status": "success", "blog": updated}
    raise HTTPException(status_code=404, detail="Blog post not found")

@app.patch("/api/blogs/{blog_id}")
def patch_blog(blog_id: str, req: dict):
    """
    Partial update — merges provided fields into the existing blog without
    losing AI-generated metadata (faq, references, images, seoScore, tags).
    Use this to manually insert backlinks / affiliate links into content,
    toggle published status, or tweak any single field — even after publishing.
    """
    blogs = read_json_file(BLOGS_FILE, [])
    for idx, blog in enumerate(blogs):
        if blog.get("id") == blog_id:
            # Only update provided keys; preserve everything else
            for key, value in req.items():
                if key not in ("id", "created_at"):
                    blog[key] = value
            blog["updated_at"] = datetime.utcnow().isoformat()
            blogs[idx] = blog
            write_json_file(BLOGS_FILE, blogs)
            return {"status": "success", "blog": blog}
    raise HTTPException(status_code=404, detail="Blog post not found")

@app.delete("/api/blogs/{blog_id}")
def delete_blog(blog_id: str):
    blogs = read_json_file(BLOGS_FILE, [])
    initial_len = len(blogs)
    blogs = [b for b in blogs if b.get("id") != blog_id]
    if len(blogs) == initial_len:
        raise HTTPException(status_code=404, detail="Blog post not found")
    write_json_file(BLOGS_FILE, blogs)
    # Also clean up comments
    comments_db = read_json_file(COMMENTS_FILE, {})
    comments_db.pop(blog_id, None)
    write_json_file(COMMENTS_FILE, comments_db)
    return {"status": "success", "message": "Blog deleted successfully."}

@app.post("/api/blogs/{slug_or_id}/comments")
def add_comment(slug_or_id: str, req: BlogCommentRequest):
    blogs = read_json_file(BLOGS_FILE, [])
    blog_id = None
    for blog in blogs:
        if blog.get("slug") == slug_or_id or blog.get("id") == slug_or_id:
            blog_id = blog.get("id")
            break
    if not blog_id:
        raise HTTPException(status_code=404, detail="Blog post not found")
    
    comments_db = read_json_file(COMMENTS_FILE, {})
    if blog_id not in comments_db:
        comments_db[blog_id] = []
    
    new_comment = {
        "id": str(uuid.uuid4()),
        "author": req.author,
        "content": req.content,
        "timestamp": datetime.utcnow().isoformat()
    }
    comments_db[blog_id].append(new_comment)
    write_json_file(COMMENTS_FILE, comments_db)
    return {"status": "success", "comment": new_comment}

@app.get("/api/system/ollama-models")
def get_ollama_models(baseUrl: str = "http://localhost:11434"):
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
def generate_blog_draft(req: BlogGenerateRequest):
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

    if req.llmProvider == "api" or req.llmApiName == "gemini":
        # API Provider (Gemini fallback using langchain-google-genai or direct request)
        # We can construct a direct HTTP request to Gemini API to avoid dependency issues, or use langchain
        api_key = req.llmApiKey or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=400, detail="Gemini API Key is not set.")
        
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            payload = {
                "contents": [{
                    "parts": [{"text": f"{system_instruction}\n\n{user_prompt}"}]
                }],
                "generationConfig": {
                    "responseMimeType": "application/json"
                }
            }
            req_data = json.dumps(payload).encode("utf-8")
            http_req = urllib.request.Request(
                url, data=req_data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=30.0) as response:
                resp_data = json.loads(response.read().decode())
                text_content = resp_data["candidates"][0]["content"]["parts"][0]["text"]
                result_json = json.loads(text_content.strip())
        except Exception as e:
            print(f"Gemini generation error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to generate draft with Gemini: {str(e)}")

    else:
        # Local Provider (Ollama)
        base_url = req.llmBaseUrl or "http://localhost:11434"
        model_name = req.llmModelName or "qwen2.5:latest"
        try:
            url = f"{base_url.rstrip('/')}/api/generate"
            payload = {
                "model": model_name,
                "prompt": f"{system_instruction}\n\n{user_prompt}",
                "format": "json",
                "stream": False
            }
            req_data = json.dumps(payload).encode("utf-8")
            http_req = urllib.request.Request(
                url, data=req_data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=60.0) as response:
                resp_data = json.loads(response.read().decode())
                text_content = resp_data.get("response", "")
                result_json = json.loads(text_content.strip())
        except Exception as e:
            print(f"Ollama generation error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to generate draft with local Ollama: {str(e)}")

    if not result_json or "title" not in result_json or "content" not in result_json:
        # Fallback response
        return {
            "title": f"Draft: {req.prompt[:40]}",
            "summary": f"A draft post about {req.prompt}.",
            "content": f"# {req.prompt}\n\nThis is a fallback generated draft post covering the primary keyword '{req.primaryKeyword}'.\n\n### Frequently Asked Questions\n**Q: What is this?**\n*A: This is a placeholder blog post draft.*\n\n### References\n- [Lumynor Systems Official Website](https://lumynor.com)"
        }

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
def optimize_seo_blog(req: BlogSeoOptimizeRequest):
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

    if req.llmProvider == "api" or req.llmApiName == "gemini":
        api_key = req.llmApiKey or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=400, detail="Gemini API Key is not set.")
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            payload = {
                "contents": [{
                    "parts": [{"text": f"{system_instruction}\n\n{user_prompt}"}]
                }],
                "generationConfig": {
                    "responseMimeType": "application/json"
                }
            }
            req_data = json.dumps(payload).encode("utf-8")
            http_req = urllib.request.Request(
                url, data=req_data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=35.0) as response:
                resp_data = json.loads(response.read().decode())
                text_content = resp_data["candidates"][0]["content"]["parts"][0]["text"]
                result_json = json.loads(text_content.strip())
        except Exception as e:
            print(f"Gemini SEO optimization error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to optimize with Gemini: {str(e)}")

    else:
        base_url = req.llmBaseUrl or "http://localhost:11434"
        model_name = req.llmModelName or "qwen2.5:latest"
        try:
            url = f"{base_url.rstrip('/')}/api/generate"
            payload = {
                "model": model_name,
                "prompt": f"{system_instruction}\n\n{user_prompt}",
                "format": "json",
                "stream": False
            }
            req_data = json.dumps(payload).encode("utf-8")
            http_req = urllib.request.Request(
                url, data=req_data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=60.0) as response:
                resp_data = json.loads(response.read().decode())
                text_content = resp_data.get("response", "")
                result_json = json.loads(text_content.strip())
        except Exception as e:
            print(f"Ollama SEO optimization error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to optimize with Ollama: {str(e)}")

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
    niche: str = "Agentic AI & Software Development"
    topics: str = ""
    frequency_hours: int = 24
    author: str = "Danish"
    category: str = "Agentic AI"
    auto_publish: bool = True
    tone: str = "Technical Expert"
    # LLM passthrough (optional, uses system env if omitted)
    llmProvider: str = "local"
    llmModelName: str = "qwen2.5:latest"
    llmApiKey: str = ""
    llmBaseUrl: str = ""

@app.get("/api/system/auto-blog-settings")
def get_auto_blog_settings():
    stored = read_json_file(AUTO_BLOG_SETTINGS_FILE, {})
    # Provide defaults for any missing keys
    defaults = {
        "enabled": False,
        "niche": "Agentic AI & Software Development",
        "topics": "AI agents, TypeScript, system architecture, developer tools",
        "frequency_hours": 24,
        "author": "Danish",
        "category": "Agentic AI",
        "auto_publish": True,
        "tone": "Technical Expert",
        "llmProvider": "local",
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
def update_auto_blog_settings(req: AutoBlogSettingsUpdateRequest):
    # Load existing file to preserve runtime fields (last_run, run_count, etc.)
    existing = read_json_file(AUTO_BLOG_SETTINGS_FILE, {})
    # Merge: user-submitted config overwrites config keys, runtime keys preserved
    updated = {**existing, **req.dict()}
    write_json_file(AUTO_BLOG_SETTINGS_FILE, updated)
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
    api_name = "gemini" if provider == "api" else "local"
    model_name = settings.get("llmModelName", "qwen2.5:latest")
    api_key = settings.get("llmApiKey") or os.getenv("GEMINI_API_KEY")
    base_url = settings.get("llmBaseUrl", "http://localhost:11434")
    
    if provider == "api" or api_name == "gemini":
        if api_key:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
                payload = {
                    "contents": [{
                        "parts": [{"text": f"{system_instruction}\n\n{user_prompt}"}]
                    }],
                    "generationConfig": {
                        "responseMimeType": "application/json"
                    }
                }
                req_data = json.dumps(payload).encode("utf-8")
                http_req = urllib.request.Request(
                    url, data=req_data, headers={"Content-Type": "application/json"}, method="POST"
                )
                loop = asyncio.get_event_loop()
                def run_request():
                    with urllib.request.urlopen(http_req, timeout=45.0) as response:
                        return json.loads(response.read().decode())
                resp_data = await loop.run_in_executor(None, run_request)
                text_content = resp_data["candidates"][0]["content"]["parts"][0]["text"]
                result_json = json.loads(text_content.strip())
            except Exception as e:
                print(f"Auto-Blogger Gemini error: {e}")
    else:
        try:
            url = f"{base_url.rstrip('/')}/api/generate"
            payload = {
                "model": model_name,
                "prompt": f"{system_instruction}\n\n{user_prompt}",
                "format": "json",
                "stream": False
            }
            req_data = json.dumps(payload).encode("utf-8")
            http_req = urllib.request.Request(
                url, data=req_data, headers={"Content-Type": "application/json"}, method="POST"
            )
            loop = asyncio.get_event_loop()
            def run_request():
                with urllib.request.urlopen(http_req, timeout=90.0) as response:
                    return json.loads(response.read().decode())
            resp_data = await loop.run_in_executor(None, run_request)
            text_content = resp_data.get("response", "")
            result_json = json.loads(text_content.strip())
        except Exception as e:
            print(f"Auto-Blogger Ollama error: {e}")
            
    if not result_json or "title" not in result_json or "content" not in result_json:
        result_json = {
            "title": f"The Future of {niche} in 2026",
            "summary": f"Exploring trending developments in {niche} and their impact on digital products.",
            "content": f"# The Future of {niche} in 2026\n\nAutomated post covering trends in {niche}.\n\n### Frequently Asked Questions\n**Q: What are the key trends?**\n*A: Advanced AI integrations and custom agentic frameworks.*\n\n### References\n- [Lumynor Trends](https://lumynor.com)"
        }
        
    blogs = read_json_file(BLOGS_FILE, [])
    slug = re.sub(r'[^a-z0-9\s-]', '', result_json["title"].lower())
    slug = re.sub(r'[\s-]+', '-', slug).strip('-')
    
    new_blog = {
        "id": str(uuid.uuid4()),
        "title": result_json["title"],
        "slug": slug,
        "category": category,
        "author": author,
        "summary": result_json.get("summary", ""),
        "content": result_json["content"],
        "published": auto_publish,
        "coverImage": "",
        "primaryKeyword": topics.split(",")[0].strip() if topics else "",
        "secondaryKeywords": topics,
        "created_at": datetime.utcnow().isoformat(),
        "is_auto_posted": True
    }
    
    blogs.append(new_blog)
    write_json_file(BLOGS_FILE, blogs)
    
    # Update runtime tracking fields in settings file
    now_iso = datetime.utcnow().isoformat()
    settings["last_run"] = now_iso
    settings["run_count"] = settings.get("run_count", 0) + 1
    write_json_file(AUTO_BLOG_SETTINGS_FILE, settings)
    
    await manager.broadcast({
        "type": "blog_published",
        "blog": new_blog,
        "message": f"\U0001f4f0 Auto-Blogger published: '{new_blog['title']}'"
    })
    print(f"\U0001f4f0 Auto-Blogger published post: {new_blog['title']}")

async def auto_blogger_daemon():
    print("\U0001f916 Auto-Blogger Daemon started.")
    while True:
        try:
            await asyncio.sleep(10)
            settings = read_json_file(AUTO_BLOG_SETTINGS_FILE, {})
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
                await generate_and_post_auto_blog(settings)
                
        except Exception as e:
            print(f"\U0001f525 Auto-Blogger Daemon Error: {e}")

# ── ENHANCED AUTO-BLOG PIPELINE ENDPOINTS ──────────────────────────────────────

from auto_blogger import run_auto_blog_pipeline, research_trending_topics

class AutoBlogRunRequest(BaseModel):
    niche: str = ""
    keywords: str = ""
    author: str = ""
    category: str = ""
    auto_publish: bool = False
    nanobanana_key: str = ""
    nanobanana_url: str = ""
    gemini_api_key: str = ""

@app.post("/api/blogs/auto-generate")
async def auto_generate_blog(req: AutoBlogRunRequest):
    """Full auto-blog pipeline: trending research → keyword research → longform writing → images → SEO."""
    # Merge request with saved settings
    settings = read_json_file(AUTO_BLOG_SETTINGS_FILE, {})

    merged_settings = {
        "niche": req.niche or settings.get("niche", "Technology"),
        "keywords": req.keywords or settings.get("topics", ""),
        "author": req.author or settings.get("author", "Lumynor Team"),
        "category": req.category or settings.get("category", "Technology"),
        "auto_publish": req.auto_publish if req.auto_publish else settings.get("auto_publish", False),
        "nanobanana_key": req.nanobanana_key or settings.get("nanobanana_key", ""),
        "nanobanana_url": req.nanobanana_url or settings.get("nanobanana_url", ""),
    }

    gemini_key = req.gemini_api_key or settings.get("llmApiKey") or os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        raise HTTPException(status_code=400, detail="Gemini API key is required. Set it in Settings or provide in request.")

    try:
        loop = asyncio.get_event_loop()
        blog_object = await loop.run_in_executor(None, run_auto_blog_pipeline, merged_settings, gemini_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auto-blog pipeline failed: {str(e)}")

    # Save to blogs.json
    blogs = read_json_file(BLOGS_FILE, [])
    new_blog = {
        "id": str(uuid.uuid4()),
        **blog_object,
        "created_at": datetime.utcnow().isoformat(),
        "is_auto_posted": True,
    }
    blogs.append(new_blog)
    write_json_file(BLOGS_FILE, blogs)

    # Update auto-blog runtime settings
    settings["last_run"] = datetime.utcnow().isoformat()
    settings["run_count"] = settings.get("run_count", 0) + 1
    write_json_file(AUTO_BLOG_SETTINGS_FILE, settings)

    await manager.broadcast({
        "type": "blog_published",
        "blog": new_blog,
        "message": f"📰 Auto-Blogger published: '{new_blog['title']}' (SEO Score: {new_blog.get('seoScore', 'N/A')}/100)"
    })

    return {
        "status": "success",
        "blog": new_blog,
        "pipeline_log": blog_object.get("pipelineLog", []),
        "seo_report": {
            "score": new_blog.get("seoScore"),
            "grade": new_blog.get("seoGrade"),
            "word_count": new_blog.get("wordCount"),
        }
    }


@app.get("/api/trending-topics")
async def get_trending_topics(niche: str = "Technology", keywords: str = ""):
    """Get trending topic suggestions for a niche without generating a full blog."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY not set")
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, research_trending_topics, niche, keywords, gemini_key)
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
def generate_image(req: ImageGenerateRequest):
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
def update_auto_blog_settings_v2(req: AutoBlogSettingsUpdateRequestV2):
    existing = read_json_file(AUTO_BLOG_SETTINGS_FILE, {})
    updated = {**existing, **req.dict()}
    write_json_file(AUTO_BLOG_SETTINGS_FILE, updated)
    return updated


# ── ENHANCED GENERATE_AND_POST to use the new pipeline ─────────────────────────
async def generate_and_post_auto_blog_v2(settings: dict):
    """Enhanced auto-blog posting using the full pipeline."""
    gemini_key = settings.get("llmApiKey") or os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        print("[auto_blogger] No Gemini key — falling back to legacy generator")
        await generate_and_post_auto_blog(settings)
        return

    try:
        loop = asyncio.get_event_loop()
        blog_object = await loop.run_in_executor(None, run_auto_blog_pipeline, settings, gemini_key)

        blogs = read_json_file(BLOGS_FILE, [])
        new_blog = {
            "id": str(uuid.uuid4()),
            **blog_object,
            "created_at": datetime.utcnow().isoformat(),
            "is_auto_posted": True,
        }
        blogs.append(new_blog)
        write_json_file(BLOGS_FILE, blogs)

        settings["last_run"] = datetime.utcnow().isoformat()
        settings["run_count"] = settings.get("run_count", 0) + 1
        write_json_file(AUTO_BLOG_SETTINGS_FILE, settings)

        await manager.broadcast({
            "type": "blog_published",
            "blog": new_blog,
            "message": f"📰 Auto-Blogger published: '{new_blog['title']}' (SEO: {new_blog.get('seoScore')}/100)"
        })
        print(f"[auto_blogger] Published: {new_blog['title']} (SEO {new_blog.get('seoScore')}/100)")
    except Exception as e:
        print(f"[auto_blogger_v2] Error: {e} — falling back to legacy")
        await generate_and_post_auto_blog(settings)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_blogger_daemon())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

