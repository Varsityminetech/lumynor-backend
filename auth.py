import json
import os
import secrets
import bcrypt
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt

_DEFAULT_SECRET = "lumynor-super-secret-key-change-in-production"
SECRET_KEY = os.getenv("SECRET_KEY", _DEFAULT_SECRET)
if SECRET_KEY == _DEFAULT_SECRET:
    # A predictable JWT signing key means anyone can forge legacy tokens (which are
    # accepted by /ws and /auth/admin/credentials). Loudly flag it; set SECRET_KEY
    # in the Railway environment to a long random value.
    print("⚠️  SECURITY: SECRET_KEY is the insecure default — set SECRET_KEY env var to a random secret.")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 8

_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
os.makedirs(_DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(_DATA_DIR, "users.json")
AUDIT_FILE = os.path.join(_DATA_DIR, "audit_log.json")

# Seeded legacy passwords come from env, NOT a hardcoded well-known value. The old
# hardcoded "lumynor_ceo_2024" was public in source and re-seeded on every Railway
# deploy (ephemeral fs), so anyone could log in via /auth/login. Now: use
# LEGACY_ADMIN_PASSWORD if set, otherwise a random per-boot password (effectively
# disabling legacy login unless the operator sets one). The main dashboard uses
# Supabase auth and does not depend on these.
_LEGACY_PW = os.getenv("LEGACY_ADMIN_PASSWORD") or secrets.token_urlsafe(24)

DEFAULT_USERS = [
    {"email": "admin@lumynor.com",     "name": "CEO",        "role": "ceo",      "password": _LEGACY_PW},
    {"email": "observer1@lumynor.com", "name": "Observer 1", "role": "observer", "password": secrets.token_urlsafe(24)},
    {"email": "observer2@lumynor.com", "name": "Observer 2", "role": "observer", "password": secrets.token_urlsafe(24)},
]

def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def _verify(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def seed_users():
    if not os.path.exists(USERS_FILE):
        hashed = []
        for u in DEFAULT_USERS:
            hashed.append({
                "email": u["email"],
                "name":  u["name"],
                "role":  u["role"],
                "hashed_password": _hash(u["password"])
            })
        with open(USERS_FILE, "w") as f:
            json.dump(hashed, f, indent=2)
        print("✅ Users seeded successfully.")

def seed_audit():
    if not os.path.exists(AUDIT_FILE):
        with open(AUDIT_FILE, "w") as f:
            json.dump([], f)

def load_users():
    with open(USERS_FILE) as f:
        return json.load(f)

def update_user_credentials(old_email: str, new_email: str, new_password: str = None) -> bool:
    users = load_users()
    updated = False
    for user in users:
        if user["email"] == old_email:
            user["email"] = new_email
            if new_password:
                user["hashed_password"] = _hash(new_password)
            updated = True
            break
    if updated:
        with open(USERS_FILE, "w") as f:
            json.dump(users, f, indent=2)
    return updated

def verify_password(plain: str, hashed: str) -> bool:
    return _verify(plain, hashed)

def authenticate_user(email: str, password: str) -> Optional[dict]:
    users = load_users()
    for user in users:
        if user["email"] == email and _verify(password, user["hashed_password"]):
            return user
    return None

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None

def append_audit_log(entry: dict):
    seed_audit()
    with open(AUDIT_FILE, "r") as f:
        logs = json.load(f)
    logs.append(entry)
    with open(AUDIT_FILE, "w") as f:
        json.dump(logs, f, indent=2)

def get_audit_logs():
    seed_audit()
    with open(AUDIT_FILE) as f:
        return json.load(f)
