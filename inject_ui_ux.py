import json
import os

SKILLS_FILE = "/Volumes/Lumynor/lumynor-systems/backend/agent_skills.json"

ui_ux_pro_skills = [
    "UX PRO MAX [Design Reasoning]: I use the industry-specific reasoning engine to match product types (SaaS, Fintech, Healthcare) to proven UI patterns and color moods.",
    "UX PRO MAX [Anti-Patterns]: I strictly AVOID AI-style pink/purple gradients, harsh animations, and low-contrast accessibility failures.",
    "UX PRO MAX [Tech-Ready]: I implement designs using modern stacks like shadcn/ui, Tailwind CSS, and Framer Motion for premium, responsive experiences.",
    "UX PRO MAX [Checklist]: I verify every UI for cursor-pointers, smooth hover states (200-300ms), and WCAG AA contrast before delivery."
]

design_agents = ["Isabelle Moreau", "Zara Khan", "Leo Tanaka", "Sofia Rodriguez", "Design Lead", "UI Designer", "UX Researcher", "System Architect"]

if os.path.exists(SKILLS_FILE):
    with open(SKILLS_FILE, "r") as f:
        data = json.load(f)
    
    for agent in design_agents:
        if agent in data:
            # Avoid duplicate injection
            if not any("UX PRO MAX" in note for note in data[agent]):
                data[agent].extend(ui_ux_pro_skills)
                print(f"✨ Injected UI/UX Pro Max into {agent}")
    
    with open(SKILLS_FILE, "w") as f:
        json.dump(data, f, indent=2)
else:
    print("❌ agent_skills.json not found")
