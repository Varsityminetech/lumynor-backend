import json
import os

SKILLS_FILE = "/Volumes/Lumynor/lumynor-systems/backend/agent_skills.json"

FORMATTING_SKILLS = [
    "Professional Document Design: Use a clear hierarchy with H1 for titles, H2 for sections, and H3 for sub-points. Never exceed 3 levels of depth.",
    "Aesthetic Presentation: Use bolding for key terms, but sparingly. Bullet points should be concise and parallel in structure.",
    "Visual Clarity: Use tables for comparing data or listing specifications. Always include a header row and clear alignment.",
    "Premium Deliverables: Every document must start with a concise 'Executive Summary' or 'Key Takeaways' box using a blockquote or bolded list.",
    "PPT-Ready Content: Structure reports so they can be easily converted to slides: 1 main point per heading, max 6 bullets per section.",
    "Tone & Style: Maintain a professional, executive tone. Avoid jargon where possible; explain it where necessary.",
    "Data Storytelling: When presenting numbers, always provide context (e.g., 'a 20% increase' is better than just '20%').",
    "Accessibility in Docs: Use descriptive alt-text placeholders for images and ensure high contrast in any suggested color palettes.",
    "Action-Oriented Formatting: End sections with a 'Next Steps' or 'Action Items' checklist for the team.",
    "White Space & Rhythm: Keep paragraphs short (3-5 lines). Use white space to separate complex ideas and improve readability."
]

AGENT_NAMES = [
    "Marcus Chen", "Priya Sharma", "Leo Tanaka", "Sofia Rodriguez", 
    "Kaito Yamamoto", "Isabelle Moreau", "Viktor Petrov", "Zara Khan",
    "Mei Lin", "Aisha Osei", "Raj Patel", "Elena Vasquez", "James Okafor",
    "Nadia Al-Hassan", "Tom Brennan", "Jack Hacker", "Amara Justice",
    "David Clarke", "Marco Rossi", "Chloe Wright", "Ryan Park",
    "Maya Santos", "Kevin Liu", "Aaliyah Brooks", "Diego Fernandez", "Lily Thompson"
]

def seed_formatting_training():
    if os.path.exists(SKILLS_FILE):
        with open(SKILLS_FILE, "r") as f:
            try:
                data = json.load(f)
            except:
                data = {}
    else:
        data = {}

    for name in AGENT_NAMES:
        if name not in data:
            data[name] = []
        # Prepend formatting skills so they are always in context
        # We take a subset or all depending on space
        for skill in FORMATTING_SKILLS:
            if skill not in data[name]:
                data[name].append(skill)
        # Keep last 30
        data[name] = data[name][-30:]

    with open(SKILLS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"✅ Successfully trained {len(AGENT_NAMES)} agents in professional formatting and presentation design.")

if __name__ == "__main__":
    seed_formatting_training()
