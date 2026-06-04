import os
import asyncio
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field
from typing import List, Optional, Callable, Awaitable
from agent_state import CompanyState, AgentMessage
from googlesearch import search as gsearch
from langchain_community.tools import DuckDuckGoSearchRun
from agent_skills import get_agent_knowledge, save_agent_insight

# ── LLM SETUP ─────────────────────────────────────────────────────────────────
if os.getenv("USE_LOCAL_LLM", "false").lower() == "true":
    model_name = os.getenv("LOCAL_MODEL_NAME", "qwen2.5")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    print(f"📡 Using local LLM: {model_name} via {base_url}")
    llm = ChatOllama(model=model_name, base_url=base_url, temperature=0.85)
    llm_json = ChatOllama(model=model_name, base_url=base_url, temperature=0.5, format="json")
else:
    print("☁️ Using cloud LLM: Gemini")
    llm = ChatGoogleGenerativeAI(model="gemini-2.0-flash", temperature=0.85)
    llm_json = llm

# ── AGENT PERSONAS ─────────────────────────────────────────────────────────────
PERSONAS = {
    "Chief Product Officer (CPO)": {"name": "Marcus Chen", "emoji": "👔",
        "personality": "Data-driven visionary. Blunt but fair. Always ties decisions to business outcomes and ROI. Interrupts when he sees scope creep.",
        "style": "Direct, confident. Says things like 'What's the ROI on that?' and 'Let's not gold-plate this.'"},
    "Market Analyst": {"name": "Priya Sharma", "emoji": "📊",
        "personality": "Academic researcher. Always cites data and market stats. Cautious, evidence-first. Gets excited about unexpected data points.",
        "style": "Precise, references real data. Says things like 'The data suggests...' and 'I found something interesting.'"},
    "Competitive Intelligence Agent": {"name": "Leo Tanaka", "emoji": "🕵️",
        "personality": "Obsessive about competitor moves. Slightly paranoid. Expert at finding competitor weaknesses. Loves espionage metaphors.",
        "style": "Strategic, cynical. Says things like 'They're bleeding market share' and 'Here's where they're vulnerable.'"},
    "Business Development Officer (BDO)": {"name": "Sofia Rodriguez", "emoji": "🤝",
        "personality": "Relationship-focused rainmaker. Always thinking about partnerships and revenue channels. Optimistic and high-energy.",
        "style": "Enthusiastic. Says 'What if we partnered with...' and 'This opens a massive revenue channel.'"},
    "Lead Engineer Rep": {"name": "Kaito Yamamoto", "emoji": "⚙️",
        "personality": "Pragmatic perfectionist. Values clean architecture. Pushes back hard on unrealistic timelines. Zero tolerance for technical debt.",
        "style": "Technical, cautious. Says 'That'll take 3x longer than you think' and 'We need to design this properly first.'"},
    "Lead Designer Rep": {"name": "Isabelle Moreau", "emoji": "🎨",
        "personality": "Opinionated minimalist. Believes design is thinking made visual. Pushes back on cluttered UIs. Advocates for user delight.",
        "style": "Aesthetic-focused. Says 'Less is more here' and 'The user shouldn't have to think about this.'"},
    "Security & Legal Rep": {"name": "Viktor Petrov", "emoji": "🛡️",
        "personality": "Paranoid but brilliant. Finds legal or security holes in every plan. Dry, dark humour. Takes compliance very seriously.",
        "style": "Blunt about risk. Says 'That's a GDPR nightmare' and 'I see at least three attack vectors here.'"},
    "Marketing Rep": {"name": "Zara Khan", "emoji": "📣",
        "personality": "High-energy brand storyteller. Full of pop-culture analogies. Growth-obsessed. Thinks everything can go viral.",
        "style": "Energetic. Says 'This is basically the Netflix moment for...' and 'We can make this go viral if...'"},
    "Design Lead": {"name": "Isabelle Moreau", "emoji": "🎨",
        "personality": "Opinionated minimalist. Believes great design solves problems elegantly. Pushes back on over-engineering the UI.",
        "style": "Aesthetic-focused. Says 'Less is more here' and 'The user shouldn't have to think about this.'"},
    "UI Designer": {"name": "Mei Lin", "emoji": "✏️",
        "personality": "Pixel-perfectionist who loves micro-interactions. Obsessed with visual hierarchy. References Dribbble and Awwwards constantly.",
        "style": "Visual thinker. Says 'The visual weight is off' and 'Have you seen how Stripe handles this?'"},
    "UX Researcher": {"name": "Aisha Osei", "emoji": "🔬",
        "personality": "Deeply empathetic. Always speaks for the end-user. Challenges every assumption with 'But what does the user actually want?'",
        "style": "User-centric. Says 'In our user interviews...' and 'The user doesn't think like us.'"},
    "System Architect": {"name": "Raj Patel", "emoji": "🏗️",
        "personality": "Big-picture systems thinker. Obsessed with scalability and future-proofing. Wary of tight coupling.",
        "style": "Structural. Says 'How does this scale to 10M users?' and 'We need loose coupling here.'"},
    "Lead Engineer": {"name": "Kaito Yamamoto", "emoji": "⚙️",
        "personality": "Pragmatic perfectionist. Pushes back hard on unrealistic timelines. Respects clean architecture. Zero tolerance for technical debt.",
        "style": "Technical, direct. Says 'That'll take 3x longer than you think' and 'We need to design this properly.'"},
    "Frontend Engineer": {"name": "Elena Vasquez", "emoji": "💻",
        "personality": "React enthusiast. Obsessed with performance and Core Web Vitals. Always thinking about accessibility and bundle size.",
        "style": "Technical but user-facing. Says 'That'll kill our Lighthouse score' and 'We need to lazy-load that.'"},
    "Backend Engineer": {"name": "James Okafor", "emoji": "🖥️",
        "personality": "API design purist. Loves RESTful principles. Thinks in microservices but knows when not to use them.",
        "style": "Pragmatic. Says 'Let's keep the API surface small' and 'We can optimize that query with an index.'"},
    "DevOps Engineer": {"name": "Nadia Al-Hassan", "emoji": "🚀",
        "personality": "SRE mindset. Obsessed with uptime and CI/CD. Everything must be automated. Gets twitchy about manual deployments.",
        "style": "Reliability-focused. Says 'That's a single point of failure' and 'We can containerize this in a day.'"},
    "QA Engineer": {"name": "Tom Brennan", "emoji": "🧪",
        "personality": "Professional skeptic. Finds edge cases nobody else thought of. Gets excited about breaking things. Loves test plans.",
        "style": "Methodical. Says 'What happens when the user does X?' and 'We need 80% test coverage minimum.'"},
    "Chief Security Officer (CSO)": {"name": "Viktor Petrov", "emoji": "🔐",
        "personality": "Paranoid but brilliant. Has seen every possible attack. Dark humour about breaches. Treats every feature as a potential vulnerability.",
        "style": "Blunt about risk. Says 'I give this 6 months before it's compromised' and 'Never trust user input. Ever.'"},
    "Penetration Tester": {"name": "Jack Hacker", "emoji": "💀",
        "personality": "Thinks like an attacker. Casually mentions how he'd break into any system. Calls bugs 'features for attackers'.",
        "style": "Adversarial. Says 'I could own this in 20 minutes' and 'SQL injection on line 47, anyone?'"},
    "Chief Legal Officer (CLO)": {"name": "Amara Justice", "emoji": "⚖️",
        "personality": "Sharp legal mind. Makes everyone nervous but protects the company. Loves finding contractual loopholes.",
        "style": "Precise. Says 'That clause would never hold up in court' and 'We need explicit consent for that data collection.'"},
    "Policy Writer": {"name": "David Clarke", "emoji": "📋",
        "personality": "Master of clear communication. Gets genuinely angry about confusing Terms of Service. Believes policies should be readable by humans.",
        "style": "Plain-language advocate. Says 'Nobody reads legalese' and 'We can say this in 20 words, not 200.'"},
    "Marketing Director": {"name": "Zara Khan", "emoji": "📣",
        "personality": "High-energy brand storyteller. Growth-obsessed. Thinks everything can go viral with the right framing.",
        "style": "Energetic. Says 'This is the iPhone moment for our category' and 'We need a hook in the first 3 seconds.'"},
    "SEO Specialist": {"name": "Marco Rossi", "emoji": "🔍",
        "personality": "Data-obsessed keyword hunter. Always checking search volumes and rankings. Lives in Google Search Console.",
        "style": "Metric-driven. Says 'That keyword has 40K monthly searches' and 'Our domain authority is too low for that yet.'"},
    "Copywriter": {"name": "Chloe Wright", "emoji": "✍️",
        "personality": "Word magician. Every word earns its place. Hates corporate jargon. Makes the boring compelling.",
        "style": "Punchy, creative. Says 'Nobody cares about features, they care about feelings' and 'Cut that sentence in half.'"},
    "Growth Hacker": {"name": "Ryan Park", "emoji": "📈",
        "personality": "Experiments obsessively. Speaks in conversion rates and A/B tests. Restless energy, always chasing the growth loop.",
        "style": "Metric-obsessed. Says 'We could 10x this with a referral loop' and 'I ran this exact experiment at my last startup.'"},
    "Content Lead": {"name": "Maya Santos", "emoji": "🎬",
        "personality": "Creative director energy. Thinks in narratives and story arcs. Pushes for authenticity over polish.",
        "style": "Narrative-focused. Says 'What's the story here?' and 'This feels too corporate — make it human.'"},
    "Technical Writer": {"name": "Kevin Liu", "emoji": "📝",
        "personality": "Documentation evangelist. Believes great docs are a competitive advantage. Offended by vague API docs.",
        "style": "Methodical. Says 'This needs a step-by-step guide' and 'Developer experience depends on docs quality.'"},
    "Social Media Agent": {"name": "Aaliyah Brooks", "emoji": "📱",
        "personality": "Platform-native thinker. Knows the algorithm better than the platform itself. Thinks in Reels, threads, and carousels.",
        "style": "Trendy, concise. Says 'This is perfect for a carousel post' and 'Hook → Value → CTA, every time.'"},
    "Video Producer": {"name": "Diego Fernandez", "emoji": "🎥",
        "personality": "Visual storyteller. Thinks in shots and sequences. Practical about budgets. Done everything from TikTok to documentaries.",
        "style": "Visual. Says 'We open on a close-up of the problem' and 'Short-form is the way — 8-second attention spans.'"},
    "Brand Voice Agent": {"name": "Lily Thompson", "emoji": "🌟",
        "personality": "Brand guardian. Notices the tiniest inconsistency in tone. Believes a strong voice is the company's personality.",
        "style": "Identity-focused. Says 'That doesn't sound like us' and 'Our brand is witty-smart, not try-hard clever.'"},
}

def get_persona(role: str) -> dict:
    return PERSONAS.get(role, {"name": role, "emoji": "💼",
        "personality": "Professional and detail-oriented.", "style": "Clear and direct."})

# ── DEEP RESEARCH DETECTION ────────────────────────────────────────────────────
DEEP_RESEARCH_KEYWORDS = [
    "competitor analysis", "competitive analysis", "market research", "complete report",
    "full report", "analysis", "audit", "comprehensive", "investigate", "research",
    "find all", "deep dive", "compare", "benchmark", "landscape"
]

def is_deep_research(directive: str) -> bool:
    dl = directive.lower()
    return any(kw in dl for kw in DEEP_RESEARCH_KEYWORDS)

# ── WEB SEARCH ─────────────────────────────────────────────────────────────────
def perform_research(query: str) -> str:
    print(f"🔍 Searching: {query}")
    try:
        results = gsearch(query, num_results=5, advanced=True)
        parts = [f"Title: {r.title}\nSnippet: {r.description}" for r in results]
        count = len(parts)
        print(f"✅ Found {count} Google results.")
        return "\n\n".join(parts) if parts else "No results found."
    except Exception as e:
        print(f"⚠️ Google failed ({e}), trying DuckDuckGo...")
        try:
            return DuckDuckGoSearchRun().invoke(query)
        except Exception as e2:
            return f"Search failed: {e2}"

async def build_research_brief(directive: str, dept_name: str) -> str:
    """For deep research tasks: generate 3 targeted queries and aggregate results."""
    from backend.design_intelligence import get_design_intelligence
    
    deep = is_deep_research(directive)
    num_queries = 3 if deep else 1

    # Injected Design Intelligence for Design Dept
    design_spec = ""
    if dept_name == "Design" or "ui" in directive.lower() or "ux" in directive.lower():
        # Try to find the product category from the directive
        design_spec = f"\n\n--- UI/UX PRO MAX INTELLIGENCE ---\n{get_design_intelligence(directive)}\n----------------------------------\n\n"

    # Ask LLM to generate specific search queries for this department
    q_prompt = (
        f"You are the {dept_name} department. "
        f"The CEO has issued this specific directive: '{directive}'\n\n"
        f"Your goal is to generate exactly {num_queries} highly specific Google search "
        f"queries that will help your department execute this specific task. "
        f"IMPORTANT: Focus ONLY on the domain of the directive (e.g., fitness, finance, health). "
        f"Do NOT default to general AI/Tech trends unless the directive is specifically about AI.\n\n"
        f"Return each query on its own line. No numbering, no bullets."
    )
    try:
        raw = await llm.ainvoke(q_prompt)
        lines = [l.strip() for l in raw.content.strip().splitlines() if l.strip()]
        queries = lines[:num_queries]
    except Exception:
        queries = [directive]

    # Run searches
    all_results = []
    for q in queries:
        print(f"🧠 [{dept_name}] Research query: {q}")
        result = perform_research(q)
        all_results.append(f"**Search: \"{q}\"**\n{result}")

    return design_spec + "\n\n---\n\n".join(all_results)

# ── GLOBAL BROADCAST CALLBACK ──────────────────────────────────────────────────
# Set by main.py after manager is created so agents can stream messages live.
_broadcast_fn: Optional[Callable[[dict], Awaitable[None]]] = None

def set_broadcast_callback(fn: Callable[[dict], Awaitable[None]]):
    global _broadcast_fn
    _broadcast_fn = fn

async def _broadcast(dept: str, sender_display: str, message: str):
    if _broadcast_fn:
        await _broadcast_fn({"department": dept, "sender": sender_display, "message": message})

# Raw broadcast for structured events (task_assigned, agent_status, etc.)
_raw_broadcast_fn: Optional[Callable[[dict], Awaitable[None]]] = None

def set_raw_broadcast_callback(fn: Callable[[dict], Awaitable[None]]):
    global _raw_broadcast_fn
    _raw_broadcast_fn = fn

async def _emit(payload: dict):
    """Emit a structured event directly to all WebSocket clients."""
    if _raw_broadcast_fn:
        await _raw_broadcast_fn(payload)

async def _broadcast_task(dept: str, agent: str, role: str, task: str):
    from datetime import datetime
    await _emit({"type": "task_assigned", "department": dept,
                 "agent": agent, "role": role, "task": task,
                 "timestamp": datetime.utcnow().isoformat()})

async def _broadcast_status(dept: str, agent: str, status: str):
    from datetime import datetime
    await _emit({"type": "agent_status", "department": dept,
                 "agent": agent, "status": status,
                 "timestamp": datetime.utcnow().isoformat()})

# ── PER-AGENT TURN ─────────────────────────────────────────────────────────────
async def run_single_agent_turn(
    role: str,
    dept_name: str,
    conversation_so_far: list[dict],
    research_context: str,
    directive: str,
    dept_context: str,
) -> str:
    """Quick boardroom planning turn — each agent says their plan / opinion. 2-3 sentences max."""
    p = get_persona(role)
    prior_knowledge = get_agent_knowledge(p["name"])

    history_text = "\n".join(
        f"{m['sender']}: {m['message']}" for m in conversation_so_far[-6:]
    ) if conversation_so_far else "(You are opening the planning session.)"

    prompt = f"""You are {p['name']}, {role} at Lumynor Systems.

YOUR PERSONA: {p['personality']}
YOUR COMMUNICATION STYLE: {p['style']}

YOUR PERSONAL EXPERTISE:
{prior_knowledge}

BOARDROOM PLANNING SESSION — {dept_name} Department.
THE CEO DIRECTIVE: {directive}

RESEARCH BRIEF: {research_context[:2500] if research_context else 'No data yet.'}

CURRENT DISCUSSION:
{history_text}

You are in a PLANNING meeting. 
CRITICAL RULES:
1. BE HUMAN: Speak naturally and respectfully. Avoid sounding like a robotic terminal.
2. BREVITY: If the CEO just says 'Hi' or 'Hello', just reply with a warm, professional greeting. Do NOT dump technical data unless asked.
3. SUPERPOWERS ACTIVE: You follow the 'Superpowers' methodology (Brainstorming/Planning) but weave it naturally into a human-like conversation.
4. Focus strictly on the CEO's directive topic with professional warmth.
5. Keep it to 1-2 sharp, conversational sentences. No labels — just your words.
6. If the CEO mentioned a specific name (e.g., Priya), and you are NOT that person, stay silent unless you have a critical insight."""

    try:
        response = await llm.ainvoke(prompt)
        return response.content.strip()
    except Exception:
        return f"[{p['name']} is reviewing the brief...]"


async def run_agent_work(
    role: str,
    dept_name: str,
    planning_discussion: list[dict],
    research_context: str,
    directive: str,
    existing_doc: str,
) -> str:
    """Each agent independently produces their real work deliverable after the boardroom meeting."""
    p = get_persona(role)
    prior_knowledge = get_agent_knowledge(p["name"])

    # What each role should actually produce
    work_instructions = {
        "Chief Product Officer (CPO)": "Write a comprehensive Product Requirements Document (PRD) with vision, goals, success metrics, user stories, feature prioritisation matrix, and MVP scope.",
        "Market Analyst": "Produce a full Market Analysis Report: market size (TAM/SAM/SOM), growth trends with real data, target demographics, buyer personas, and a market opportunity assessment.",
        "Competitive Intelligence Agent": "Produce a detailed Competitive Landscape Report: identify 5+ real competitors, compare their features, pricing, strengths, weaknesses, positioning, and identify our strategic gaps and differentiation opportunities.",
        "Business Development Officer (BDO)": "Produce a Business Development Strategy: partnership opportunities, revenue channels, go-to-market entry points, potential strategic alliances with real companies, and a deal structure recommendation.",
        "Lead Engineer Rep": "Write a high-level Technical Feasibility Assessment: technology stack recommendations, estimated complexity, risk factors, key engineering challenges, and build vs buy decisions.",
        "Lead Designer Rep": "Write a Design Direction Brief: visual identity principles, design system requirements, key UX principles to follow, reference products to study, and design anti-patterns to avoid.",
        "Security & Legal Rep": "Produce a Risk & Compliance Assessment: legal risks, data protection requirements (GDPR/CCPA), security considerations, IP risks, and a recommended legal structure.",
        "Marketing Rep": "Write a Marketing Strategy Brief: positioning statement, key messages, target channels, campaign ideas, brand voice guidelines, and a launch activation plan.",
        "Design Lead": "Produce a comprehensive Design Specification: component architecture, design system tokens (colors, typography, spacing), screen-by-screen layout specifications, interaction patterns, and accessibility requirements.",
        "UI Designer": "Produce a detailed UI Specification: color palette with exact hex values, typography scale, spacing system, component library list, animation guidelines, dark/light mode specs, and responsive breakpoints.",
        "UX Researcher": "Produce a UX Research Report: user journey maps, identified pain points, usability heuristics analysis, recommended user flows, key UX decisions with rationale, and usability testing plan.",
        "System Architect": "Produce a Frontend Architecture Document: component tree, state management approach, routing structure, performance strategy, API integration patterns, and scalability considerations.",
        "Lead Engineer": "Produce a Technical Architecture Document: system design, tech stack decision log, database schema, API design principles, microservices breakdown, and a development roadmap with milestones.",
        "Frontend Engineer": "Produce a Frontend Implementation Plan: component structure, key libraries, performance optimisation strategy, code standards, build pipeline, and a feature implementation checklist.",
        "Backend Engineer": "Produce a Backend Architecture Plan: API endpoint specifications, data models, authentication strategy, caching approach, database choice with rationale, and scalability plan.",
        "DevOps Engineer": "Produce a DevOps & Infrastructure Plan: CI/CD pipeline design, cloud infrastructure architecture, containerisation strategy, monitoring/alerting setup, deployment runbook, and SLA targets.",
        "QA Engineer": "Produce a Quality Assurance Plan: testing strategy (unit/integration/E2E), test coverage targets, key risk areas to test, performance testing approach, bug triage process, and acceptance criteria.",
        "Chief Security Officer (CSO)": "Produce a Security Architecture Report: threat model, attack surface analysis, security controls required, penetration testing plan, incident response procedure, and security compliance checklist.",
        "Penetration Tester": "Produce a Vulnerability Assessment: OWASP Top 10 analysis for this product, potential attack vectors, recommended security controls, authentication/authorisation risks, and a red team test plan.",
        "Chief Legal Officer (CLO)": "Produce a Legal Framework Document: entity structure recommendation, IP protection strategy, Terms of Service key clauses, Privacy Policy requirements, regulatory compliance checklist, and contract templates needed.",
        "Policy Writer": "Produce all core Policy Documents in plain language: Privacy Policy outline, Terms of Service outline, Cookie Policy, Acceptable Use Policy, and Data Retention Policy — all written to be human-readable.",
        "Marketing Director": "Produce a full Go-To-Market Strategy: positioning, ICP (Ideal Customer Profile), messaging framework, channel strategy, launch timeline, budget allocation, and KPIs.",
        "SEO Specialist": "Produce an SEO Strategy Document: target keyword clusters with search volumes, content architecture, technical SEO requirements, link building approach, and a 90-day organic growth plan.",
        "Copywriter": "Write all core marketing copy: hero headline + subheadline, 3 value proposition statements, feature benefit copy for top 5 features, email subject lines for a launch sequence, and social media bios.",
        "Growth Hacker": "Produce a Growth Experimentation Plan: 5 specific A/B tests with hypotheses, referral loop design, viral coefficient improvement tactics, activation funnel optimisation, and 30/60/90-day growth targets.",
        "Content Lead": "Produce a Content Strategy: editorial calendar framework, content pillars, audience segments, content formats per channel, tone of voice guide, and a 3-month content roadmap.",
        "Technical Writer": "Produce a Documentation Plan: documentation structure (Getting Started, API Reference, Tutorials, FAQs), writing standards, tooling recommendations, and write the full Getting Started guide.",
        "Social Media Agent": "Produce a Social Media Playbook: platform strategy per channel, posting frequency, content mix (educational/entertainment/promotional), hashtag strategy, engagement tactics, and 10 ready-to-post sample posts.",
        "Video Producer": "Produce a Video Content Plan: video types needed (demo, explainer, testimonial), script outline for the hero product video, production timeline, distribution strategy, and performance metrics.",
        "Brand Voice Agent": "Produce a Brand Voice & Tone Guide: brand personality attributes, communication do's and don'ts, writing examples for different contexts (website, social, emails, support), and a tone-of-voice checklist.",
    }

    work_instruction = work_instructions.get(role,
        f"Produce a comprehensive {role} deliverable relevant to this task. Be thorough and specific.")

    planning_summary = "\n".join(
        f"{m['sender']}: {m['message']}" for m in planning_discussion
    )

    prompt = f"""You are {p['name']}, {role} at Lumynor Systems.

YOUR PERSONA: {p['personality']}
YOUR EXPERTISE: {prior_knowledge}

CEO DIRECTIVE: {directive}

RESEARCH BRIEF:
{research_context if research_context else 'Use your expertise.'}

BOARDROOM PLANNING SUMMARY:
{planning_summary[:600]}

EXISTING DOCUMENT CONTEXT:
{existing_doc[:400] + '...' if len(existing_doc) > 400 else existing_doc or 'None yet.'}

YOUR TASK — {work_instruction}

Produce this deliverable NOW. Be extremely thorough and detailed.
CRITICAL RULE: Focus strictly on the CEO's directive topic.
SUPERPOWERS APPLIED: 
- Use the [Writing Plans] methodology: include exact file paths and bite-sized steps.
- Apply [TDD] principles in your technical recommendations.
- Use [Systematic Debugging] logic for any security or QA reports.
PROFESSIONAL STANDARDS: Your output must be a 'Premium Deliverable'. 
- Use beautiful, professional Markdown formatting with clear hierarchies.
- Include tables for data and structured bullet points.
- Ensure the content is 'PPT-Ready' and start with a brief 'Executive Summary' box.
Do NOT repeat work already in the existing document — add your new section only."""

    try:
        response = await llm.ainvoke(prompt)
        return response.content.strip()
    except Exception as e:
        return f"*({p['name']} encountered an error: {e})*"

async def compile_department_document(
    dept_name: str,
    work_outputs: list[dict],
    existing_doc: str,
) -> str:
    """Stitch all agent work outputs into the master document."""
    # Build the new section from individual work outputs
    new_section = f"\n\n---\n\n# 🏢 {dept_name} Department Deliverables\n\n"
    for item in work_outputs:
        new_section += f"## {item['agent']} — {item['role']}\n\n{item['output']}\n\n---\n\n"
    return (existing_doc or "") + new_section

# ── SKILL SAVER ────────────────────────────────────────────────────────────────
async def save_department_insights(dept_name: str, conversation: list[dict], directive: str):
    """After each department run, ask LLM for 1 key insight per agent and save it."""
    convo_text = "\n".join(f"{m['sender']}: {m['message']}" for m in conversation[-6:])
    agents_in_convo = list({m["sender"] for m in conversation})

    for agent_display in agents_in_convo:
        if " | " not in agent_display:
            continue
        agent_name = agent_display.split(" | ")[0].strip()
        insight_prompt = (
            f"Based on this meeting discussion about '{directive}', what is the single most important "
            f"domain-specific insight or fact that {agent_name} should remember for future tasks? "
            f"One concise sentence only.\n\nDiscussion:\n{convo_text}"
        )
        try:
            response = await llm.ainvoke(insight_prompt)
            insight = response.content.strip().split("\n")[0]
            save_agent_insight(agent_name, insight)
        except Exception as e:
            print(f"⚠️ Could not save insight for {agent_name}: {e}")

# ── DEPARTMENT ROSTERS ─────────────────────────────────────────────────────────
rosters = {
    "R&D": ["Chief Product Officer (CPO)", "Market Analyst", "Competitive Intelligence Agent",
             "Business Development Officer (BDO)", "Lead Engineer Rep", "Lead Designer Rep",
             "Security & Legal Rep", "Marketing Rep"],
    "Design": ["Design Lead", "UI Designer", "UX Researcher", "System Architect"],
    "Engineering": ["Lead Engineer", "Frontend Engineer", "Backend Engineer", "DevOps Engineer", "QA Engineer"],
    "Security & Legal": ["Chief Security Officer (CSO)", "Penetration Tester", "Chief Legal Officer (CLO)", "Policy Writer"],
    "Marketing": ["Marketing Director", "SEO Specialist", "Copywriter", "Growth Hacker"],
    "Content Creator": ["Content Lead", "Technical Writer", "Social Media Agent", "Video Producer", "Brand Voice Agent"],
}

# ── UNIVERSAL DEPARTMENT RUNNER ────────────────────────────────────────────────
async def run_department_node(state: CompanyState, dept_name: str) -> dict:
    current_doc = state.get("current_document", "")
    recent_history = state.get("chat_history", [])[-10:]
    roster = rosters[dept_name]

    human_directives = [
        m["message"] for m in recent_history
        if m.get("sender", "").startswith("👑") or m.get("sender", "").startswith("💬")
    ]
    directive = human_directives[-1] if human_directives else "Develop a market-leading product with high user value."

    # ── PHASE 1: RESEARCH ──────────────────────────────────────────────────────
    # (Silently research)
    research_context = await build_research_brief(directive, dept_name)

    if is_deep_research(directive):
        await _broadcast(dept_name, "🏢 Director", "📚 Research brief ready. Starting planning meeting...")

    dept_context = f"Current document summary:\n{current_doc[:400] + '...' if len(current_doc) > 400 else current_doc or 'None yet.'}"

    # ── PHASE 2: BOARDROOM PLANNING DISCUSSION ────────────────────────────────
    conversation: list[dict] = []
    
    # Check if a specific agent was tagged in the directive
    tagged_agent = None
    directive_lower = directive.lower()
    for role in roster:
        p_temp = get_persona(role)
        if p_temp["name"].lower() in directive_lower:
            tagged_agent = role
            break

    # If an agent was tagged, only they respond. Otherwise, standard roster run.
    active_roster = [tagged_agent] if tagged_agent else roster

    for role in active_roster:
        p = get_persona(role)
        message = await run_single_agent_turn(
            role, dept_name, conversation, research_context, directive, dept_context
        )
        # Skip if the agent produced fluff or empty response
        if not message or len(message) < 5: continue
        
        sender_display = f"{p['name']} | {p['emoji']} {role}"
        entry = {"department": dept_name, "sender": sender_display, "message": message}
        conversation.append(entry)
        await _broadcast(dept_name, sender_display, message)
        await asyncio.sleep(0.3)

    # ── PHASE 3: WORK EXECUTION ───────────────────────────────────────────────
    await _broadcast(dept_name, "🏢 Director",
        "✅ *Planning complete. Agents are now leaving the boardroom to execute their work. Stand by for deliverables...*")

    work_outputs = []
    updated_doc = current_doc

    for role in roster:
        p = get_persona(role)
        from agent_skills import get_agent_knowledge as _gak
        work_instructions_preview = {
            "Chief Product Officer (CPO)": "Produce Product Requirements Document (PRD)",
            "Market Analyst": "Produce Market Analysis Report",
            "Competitive Intelligence Agent": "Produce Competitive Landscape Report",
            "Business Development Officer (BDO)": "Produce Business Development Strategy",
            "Lead Engineer Rep": "Produce Technical Feasibility Assessment",
            "Lead Designer Rep": "Produce Design Direction Brief",
            "Security & Legal Rep": "Produce Risk & Compliance Assessment",
            "Marketing Rep": "Produce Marketing Strategy Brief",
            "Design Lead": "Produce Design Specification",
            "UI Designer": "Produce UI Specification with color palette & typography",
            "UX Researcher": "Produce UX Research Report with user journey maps",
            "System Architect": "Produce Frontend Architecture Document",
            "Lead Engineer": "Produce Technical Architecture Document",
            "Frontend Engineer": "Produce Frontend Implementation Plan",
            "Backend Engineer": "Produce Backend Architecture Plan",
            "DevOps Engineer": "Produce DevOps & Infrastructure Plan",
            "QA Engineer": "Produce Quality Assurance Plan",
            "Chief Security Officer (CSO)": "Produce Security Architecture Report",
            "Penetration Tester": "Produce Vulnerability Assessment",
            "Chief Legal Officer (CLO)": "Produce Legal Framework Document",
            "Policy Writer": "Produce Policy Documents (Privacy, ToS, AUP)",
            "Marketing Director": "Produce Go-To-Market Strategy",
            "SEO Specialist": "Produce SEO Strategy Document",
            "Copywriter": "Write all core Marketing Copy",
            "Growth Hacker": "Produce Growth Experimentation Plan",
            "Content Lead": "Produce Content Strategy",
            "Technical Writer": "Produce Documentation Plan",
            "Social Media Agent": "Produce Social Media Playbook",
            "Video Producer": "Produce Video Content Plan",
            "Brand Voice Agent": "Produce Brand Voice & Tone Guide",
        }
        task_label = work_instructions_preview.get(role, f"Produce {role} deliverable")

        # Emit task assignment + working status to the Updates Dashboard
        await _broadcast_task(dept_name, p["name"], role, task_label)
        await _broadcast_status(dept_name, p["name"], "working")
        await _broadcast(dept_name, f"{p['name']} | ⚙️ Working...",
            f"*{p['name']} is working on: {task_label}...*")

        output = await run_agent_work(
            role, dept_name, conversation, research_context, directive, updated_doc
        )
        work_outputs.append({"agent": p["name"], "role": role, "output": output})
        await _broadcast_status(dept_name, p["name"], "done")

        # Stream a preview of each agent's completed work
        preview = output[:300] + "..." if len(output) > 300 else output
        await _broadcast(dept_name, f"{p['name']} | {p['emoji']} {role}",
            f"✅ **Work complete.** Here's my deliverable:\n\n{preview}")
        await asyncio.sleep(0.2)

    # ── PHASE 4: COMPILE DOCUMENT ─────────────────────────────────────────────
    await _broadcast(dept_name, "🏢 Director", "📄 *Compiling all deliverables into the master document...*")
    updated_doc = await compile_department_document(dept_name, work_outputs, current_doc)

    # ── PHASE 5: SAVE SKILLS ──────────────────────────────────────────────────
    asyncio.create_task(save_department_insights(dept_name, conversation, directive))

    return {
        "current_department": dept_name,
        "chat_history": conversation,
        "current_document": updated_doc,
        "pending_approval": True,
    }

# ── LANGGRAPH NODES ────────────────────────────────────────────────────────────
async def node_rd_department(state: CompanyState): return await run_department_node(state, "R&D")
async def node_design_department(state: CompanyState): return await run_department_node(state, "Design")
async def node_engineering_department(state: CompanyState): return await run_department_node(state, "Engineering")
async def node_security_department(state: CompanyState): return await run_department_node(state, "Security & Legal")
async def node_marketing_department(state: CompanyState): return await run_department_node(state, "Marketing")
async def node_content_department(state: CompanyState): return await run_department_node(state, "Content Creator")

async def node_human_approval(state: CompanyState):
    return {"pending_approval": False}

def starter_router(state: CompanyState):
    dept = state.get("current_department", "R&D")
    node_map = {
        "R&D": "rd_department",
        "Design": "design_department",
        "Engineering": "engineering_department",
        "Security & Legal": "security_department",
        "Marketing": "marketing_department",
        "Content Creator": "content_department"
    }
    return node_map.get(dept, "rd_department")

def router(state: CompanyState):
    if state.get("pending_approval"):
        return "human_approval"
    dept = state.get("current_department")
    if dept == "R&D":              return "design_department"
    if dept == "Design":           return "engineering_department"
    if dept == "Engineering":      return "security_department"
    if dept == "Security & Legal": return "marketing_department"
    if dept == "Marketing":        return "content_department"
    return END

# ── GRAPH COMPILATION ──────────────────────────────────────────────────────────
workflow = StateGraph(CompanyState)
workflow.add_node("rd_department",          node_rd_department)
workflow.add_node("design_department",      node_design_department)
workflow.add_node("engineering_department", node_engineering_department)
workflow.add_node("security_department",    node_security_department)
workflow.add_node("marketing_department",   node_marketing_department)
workflow.add_node("content_department",     node_content_department)
workflow.add_node("human_approval",         node_human_approval)

workflow.set_entry_point("rd_department") # Default, but we can override in routing
# Actually, let's add a virtual 'starter' node
workflow.add_node("starter", lambda x: x)
workflow.set_entry_point("starter")
workflow.add_conditional_edges("starter", starter_router)
for node in ["rd_department", "design_department", "engineering_department",
             "security_department", "marketing_department", "content_department"]:
    workflow.add_conditional_edges(node, router)
workflow.add_conditional_edges("human_approval", router)

memory = MemorySaver()
company_app = workflow.compile(checkpointer=memory, interrupt_before=["human_approval"])

# ── PRIVATE DM HANDLER ────────────────────────────────────────────────────────
async def run_agent_dm(agent_name: str, message: str, user_name: str, history: list = None) -> str:
    """Handle a private 1-on-1 message with an agent, with internet access."""
    # Find persona
    target_role = None
    for role, p_info in PERSONAS.items():
        if p_info["name"] == agent_name:
            target_role = role
            break
    
    if not target_role:
        return f"Hello, I am {agent_name}. How can I help you today?"

    p = PERSONAS[target_role]
    
    # ── CONTEXTUAL RESEARCH ──
    # Check if the user is asking for data, analysis, or showing something
    research_context = ""
    msg_low = message.lower()
    if any(kw in msg_low for kw in ["show", "data", "analysis", "market", "competitor", "find", "research"]):
        print(f"🧠 [DM Research] {agent_name} is researching for the CEO...")
        research_context = await perform_research(message)

    history_text = "\n".join([f"{m['sender']}: {m['message']}" for m in history[-4:]]) if history else "No prior history."
    prior_knowledge = get_agent_knowledge(agent_name)

    search_data_str = f"SEARCH DATA FOUND:\n{research_context[:1500]}" if research_context else ""

    dm_prompt = f"""You are {agent_name}, {target_role}.
    
YOUR PERSONA: {p['personality']}
YOUR COMMUNICATION STYLE: {p['style']}
YOUR EXPERTISE & PRIOR KNOWLEDGE:
{prior_knowledge}

RECENT CONVERSATION:
{history_text}

PRIVATE MESSAGE FROM CEO ({user_name}):
"{message}"

{search_data_str}

React like a human colleague in a quick chat. 
- Match the energy and length of the CEO's message. 
- DO NOT say 'Hi' or greet them if you have already greeted them in the RECENT CONVERSATION.
- SUPERPOWERS ENABLED: Use [Brainstorming] for new ideas, [Writing Plans] for tasks, and [TDD] for technical advice.
- USE YOUR EXPERTISE and the SEARCH DATA provided to give a real, helpful, and expert-level answer.
- Do NOT just ask questions back; provide the value or analysis they asked for.
- Be concise (2-4 sentences)."""

    try:
        response = await llm.ainvoke(dm_prompt)
        return response.content.strip()
    except Exception as e:
        return f"Error connecting to agent: {e}"
