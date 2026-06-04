import csv
import os

REASONING_FILE = "/Volumes/Lumynor/lumynor-systems/ui_ux_pro_repo/src/ui-ux-pro-max/data/ui-reasoning.csv"

def get_design_intelligence(query):
    """Searches the UI/UX Pro Max intelligence engine for a specific product type or category."""
    if not os.path.exists(REASONING_FILE):
        return "Design intelligence database not found."
    
    query = query.lower()
    matches = []
    
    with open(REASONING_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Match against Category or Keywords
            if query in row['UI_Category'].lower():
                matches.append(row)
                
    if not matches:
        return f"No exact match for '{query}'. Please try a more general category (e.g., SaaS, E-commerce, Dashboard)."
    
    # Return the first best match
    best = matches[0]
    report = f"""
### 🎨 UI/UX PRO MAX DESIGN INTELLIGENCE: {best['UI_Category']}
- **Recommended Pattern**: {best['Recommended_Pattern']}
- **Style Priority**: {best['Style_Priority']}
- **Color Mood**: {best['Color_Mood']}
- **Typography Mood**: {best['Typography_Mood']}
- **Key Effects**: {best['Key_Effects']}
- **Decision Rules**: {best['Decision_Rules']}
- **Anti-Patterns (AVOID)**: {best['Anti_Patterns']}
- **Severity**: {best['Severity']}
"""
    return report

if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "SaaS"
    print(get_design_intelligence(q))
