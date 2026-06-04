import json
import os

SKILLS_FILE = os.path.join(os.path.dirname(__file__), "agent_skills.json")

def _load() -> dict:
    if os.path.exists(SKILLS_FILE):
        with open(SKILLS_FILE, "r") as f:
            return json.load(f)
    return {}

def _save(data: dict):
    with open(SKILLS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_agent_knowledge(agent_name: str) -> str:
    """Retrieve the last 5 knowledge notes for an agent to inject into their prompt."""
    data = _load()
    notes = data.get(agent_name, [])
    if not notes:
        return "No prior knowledge notes yet — this is your first session."
    recent = notes[-10:]
    return "\n".join(f"  • {n}" for n in recent)

def save_agent_insight(agent_name: str, insight: str):
    """Append a new insight to an agent's persistent knowledge base."""
    data = _load()
    if agent_name not in data:
        data[agent_name] = []
    data[agent_name].append(insight)
    # Cap at 30 insights per agent to prevent unbounded growth
    data[agent_name] = data[agent_name][-30:]
    _save(data)
    print(f"💾 [{agent_name}] saved new insight: {insight[:60]}...")
