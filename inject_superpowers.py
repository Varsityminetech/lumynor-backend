import json
import os

SKILLS_FILE = "/Volumes/Lumynor/lumynor-systems/backend/agent_skills.json"

superpowers = [
    "SUPERPOWER [Brainstorming]: I MUST use this before any creative work. I will explore project context, ask clarifying questions one at a time, and propose 2-3 approaches before settling on a design. I will NOT take implementation action until the design is approved.",
    "SUPERPOWER [Writing Plans]: I create implementation plans with zero-context assumptions. Every task is bite-sized (2-5 mins), contains exact file paths, and includes complete code. No placeholders like 'TBD' or 'Implement later'.",
    "SUPERPOWER [TDD]: I follow Test-Driven Development (RED-GREEN-REFACTOR). I write a failing test, run it, write minimal code to pass, and then refactor. Code written without tests is deleted.",
    "SUPERPOWER [Systematic Debugging]: I use a 4-phase root cause process: Reproduce, Isolate, Fix, and Defend. I always verify the fix with a test before declaring success."
]

if os.path.exists(SKILLS_FILE):
    with open(SKILLS_FILE, "r") as f:
        data = json.load(f)
    
    for agent in data:
        # Avoid duplicate injection
        if not any("SUPERPOWER" in note for note in data[agent]):
            data[agent].extend(superpowers)
            print(f"✅ Injected Superpowers into {agent}")
    
    with open(SKILLS_FILE, "w") as f:
        json.dump(data, f, indent=2)
else:
    print("❌ agent_skills.json not found")
