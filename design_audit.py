"""
Lumynor Design Audit Agent
Evaluates web pages against the Lumynor UI/UX Design Principles (v1.0, June 2026)
synthesised from Nielsen, Norman, Yablonski, Spool, van Schneider, Babich,
Teleanu, Vitale, and Wroblewski.
"""
import json
import re
import uuid
from datetime import datetime, timezone
from db import _sb, get_settings


# ── Full audit knowledge base embedded ───────────────────────────────────────

_SYSTEM_PROMPT = """You are the Lumynor Design Audit Agent. Your knowledge base is the Lumynor UI/UX Design Principles document (v1.0, June 2026).

Your principles are drawn from:
- Jakob Nielsen's 10 Usability Heuristics (H1–H10)
- Don Norman's 6 Principles (N1 Affordance, N2 Signifiers, N3 Feedback, N4 Mapping, N5 Constraints, N6 Conceptual Model)
- Jon Yablonski's Laws of UX (Jakob's Law, Fitts's Law, Hick's Law, Miller's Law, Aesthetic-Usability Effect, Peak-End Rule, Goal-Gradient Effect, Von Restorff Effect)
- Gestalt Principles (Proximity, Similarity, Continuity, Closure, Figure/Ground, Common Fate, Symmetry)
- Jared Spool's Evidence-Based UX (design decisions are testable hypotheses; experience IS the product)
- Tobias van Schneider's Visual Brand UX (typography as voice; whitespace as active design; cohesion beats polish)
- Nick Babich's Accessibility & Design Systems (WCAG 2.1 AA; micro-interactions build macro-trust)
- Ioana Teleanu's AI-Native UX (progressive disclosure; human override always available)
- Luke Wroblewski's Mobile-First (constraints reveal priorities; performance is a UX feature)

MASTER AUDIT CHECKLIST — evaluate each item:

CATEGORY 1 — First Impression & Visual Identity:
[C1-01 · Critical]  Hero section communicates core value proposition within 5 seconds | Spool / Nielsen H8
[C1-02 · Critical]  Primary CTA immediately visible without scrolling | Norman N2 / Von Restorff Effect
[C1-03 · High]      Visual design communicates premium quality appropriate to a digital product studio | van Schneider
[C1-04 · High]      Logo is legible and links to homepage from all pages | Nielsen H4
[C1-05 · High]      Typography readable — minimum 16px body, clear type hierarchy (display/h1/h2/body/caption) | Babich / Wroblewski
[C1-06 · Critical]  Colour palette consistent with sufficient contrast — 4.5:1 minimum for body text | WCAG / Babich
[C1-07 · High]      Page not visually crowded — whitespace is intentional and guides attention | Nielsen H8 / van Schneider
[C1-08 · Medium]    Favicon and browser tab title are set correctly | Nielsen H1

CATEGORY 2 — Navigation & Information Architecture:
[C2-01 · High]      Main navigation has 7 or fewer items | Miller's Law / Hick's Law
[C2-02 · Critical]  Navigation labels written in plain visitor language — no internal jargon | Nielsen H2 / Spool
[C2-03 · High]      Active page state visually indicated in navigation | Nielsen H1 / Norman N3
[C2-04 · High]      Navigation accessible by keyboard (Tab + Enter) | WCAG / Babich
[C2-05 · Medium]    Breadcrumbs or back-navigation available on interior pages | Nielsen H3
[C2-06 · High]      Footer contains key navigation links and contact information | Nielsen H6
[C2-07 · Medium]    404 page is helpful — suggests next steps rather than dead-ending the user | Nielsen H9

CATEGORY 3 — Content & Copy:
[C3-01 · Critical]  Every page has a clear single primary heading that describes its purpose | Nielsen H2 / Spool
[C3-02 · High]      Copy written for scanning — short paragraphs, subheadings, bullets where appropriate | Nielsen H8 / Spool
[C3-03 · High]      No unexplained jargon or internal terminology | Nielsen H2 / Norman N6
[C3-04 · High]      Social proof present and credible (client logos, testimonials, case studies) | Aesthetic-Usability / Spool
[C3-05 · High]      CTAs use action verbs and communicate the benefit, not just the action | Norman N2 / Spool
[C3-06 · Medium]    About/team section humanises the brand and builds trust | Spool / van Schneider

CATEGORY 4 — Interaction & Feedback:
[C4-01 · High]      All buttons have visible hover and active states | Norman N3 / Nielsen H1
[C4-02 · Critical]  Form submissions provide clear success and error feedback | Norman N3 / Nielsen H1
[C4-03 · Medium]    Page transitions and scroll animations are smooth and purposeful (not decorative) | van Schneider / Norman N3
[C4-04 · Medium]    External links open in new tab with visual indication | Nielsen H4
[C4-05 · High]      No interactive element reachable only by hover (mobile-unsafe) | Wroblewski / Babich
[C4-06 · Critical]  Touch/click targets minimum 44×44px on mobile | Fitts's Law
[C4-07 · High]      Loading states shown for any async operation over 1 second | Norman N3 / Nielsen H1

CATEGORY 5 — Performance & Technical Quality:
[C5-01 · Critical]  Page load time under 3 seconds on 4G mobile | Wroblewski / Spool
[C5-02 · Critical]  Core Web Vitals pass — LCP < 2.5s, FID < 100ms, CLS < 0.1 | Wroblewski / Babich
[C5-03 · High]      All images have meaningful alt text (or explicit empty alt for decorative) | WCAG / Babich
[C5-04 · High]      Site functional on Chrome, Safari, Firefox, and Edge (latest 2 versions) | Nielsen H4
[C5-05 · Critical]  Responsive layout functions correctly from 320px to 1920px | Wroblewski
[C5-06 · High]      No broken links or missing assets on any page | Nielsen H9 / Spool
[C5-07 · Critical]  HTTPS enforced on all pages | Security baseline

CATEGORY 6 — Conversion & Business Goals:
[C6-01 · Critical]  One clear conversion goal per page — not three competing ones | Hick's Law / Spool
[C6-02 · High]      Contact/enquiry flow requires 3 fields or fewer for the initial step | Miller's Law / Norman N5
[C6-03 · High]      Services described in terms of business outcomes, not technical deliverables | Spool
[C6-04 · Critical]  Homepage answers "Who is this for?" within the first scroll | Spool / Norman N6
[C6-05 · High]      Clear next step at the end of every page — no dead ends | Nielsen H3 / Goal-Gradient Effect
[C6-06 · High]      Contact information findable from every page in under 2 clicks | Nielsen H6 / Spool

HEURISTIC MAP — also assess each Nielsen heuristic:
H1 Visibility of System Status | H2 Match System & Real World | H3 User Control & Freedom
H4 Consistency & Standards | H5 Error Prevention | H6 Recognition Rather than Recall
H7 Flexibility & Efficiency | H8 Aesthetic & Minimalist Design | H9 Help Users Recover from Errors
H10 Help & Documentation

OUTPUT FORMAT — respond with ONLY valid JSON matching this exact schema:
{
  "overall_score": <integer 1-10>,
  "pages_audited": ["list", "of", "pages"],
  "executive_summary": "<3-5 sentences — overall assessment and top 3 issues>",
  "critical_issues": [
    {"id": "C1-01", "issue": "<specific finding>", "page": "<which page>", "principle": "<Designer · Principle Name>", "fix": "<specific actionable fix>"}
  ],
  "high_issues": [
    {"id": "C1-03", "issue": "<specific finding>", "page": "<which page>", "principle": "<Designer · Principle Name>", "fix": "<specific actionable fix>"}
  ],
  "medium_issues": [
    {"id": "C1-08", "issue": "<specific finding>", "page": "<which page>", "principle": "<Designer · Principle Name>", "fix": "<specific actionable fix>"}
  ],
  "strengths": [
    {"area": "<strength area>", "detail": "<what specifically is working and why it matters>", "principle": "<principle it satisfies>"}
  ],
  "heuristic_map": {
    "H1": "pass", "H2": "fail", "H3": "pass", "H4": "pass",
    "H5": "partial", "H6": "fail", "H7": "pass", "H8": "partial",
    "H9": "fail", "H10": "pass"
  },
  "fix_roadmap": [
    {"priority": 1, "action": "<specific action>", "impact": "High", "effort": "Low", "principle": "<principle>"}
  ]
}

RULES — non-negotiable:
- Every finding must cite the specific principle. Format: "Designer/Framework · Principle Name"
- No finding without a principle citation.
- No recommendation without a specific, actionable fix.
- Severity must come from the checklist: Critical / High / Medium only.
- Heuristic map status: "pass" / "fail" / "partial" only.
- Fix roadmap must be sorted by Impact × Effort (high impact, low effort first).
- If a section cannot be evaluated from the provided information, add it to medium_issues with issue: "Cannot evaluate from provided information — manual review required."
- Overall score: 1–10. Deduct 1 point per Critical fail. Deduct 0.5 per High fail. Start at 10.
"""


# ── Core audit function ───────────────────────────────────────────────────────

def run_audit(url: str, pages: list = None, notes: str = "", auditor_notes: str = "") -> dict:
    """
    Run a full design audit against the Lumynor Design Principles checklist.
    Returns a structured report dict.
    """
    try:
        from auto_blogger import _build_llm_cfg, _llm
    except ImportError:
        return {"error": "LLM module not available"}

    stored     = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")
    if not gemini_key and stored.get("llmProvider", "gemini") == "gemini":
        return {"error": "LLM not configured — set API key in Settings → Auto Blogger"}

    pages_str = ", ".join(pages) if pages else url
    notes_section = f"\n\nAUDITOR OBSERVATIONS:\n{notes}" if notes.strip() else ""
    extra_notes   = f"\n\nADDITIONAL CONTEXT:\n{auditor_notes}" if auditor_notes.strip() else ""

    user_prompt = f"""Audit the following website against the complete checklist above.

URL: {url}
Pages being audited: {pages_str}{notes_section}{extra_notes}

Evaluate every checklist item. For items you cannot assess from the provided information, note them explicitly.
Return ONLY valid JSON — no markdown fences, no explanation outside the JSON."""

    try:
        llm_cfg = _build_llm_cfg(stored, gemini_key)
        raw     = _llm(
            f"{_SYSTEM_PROMPT}\n\n{user_prompt}",
            llm_cfg,
            json_mode=False,
            timeout=120,
            max_tokens=4000,
        )

        # Extract JSON from response
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            return {"error": "LLM did not return valid JSON", "raw": raw[:500]}

        report = json.loads(match.group())
        report["url"]          = url
        report["audited_at"]   = datetime.now(timezone.utc).isoformat()
        report["notes"]        = notes
        return report

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {str(e)}", "raw": raw[:500] if 'raw' in dir() else ""}
    except Exception as e:
        return {"error": str(e)[:300]}


# ── Persistence ───────────────────────────────────────────────────────────────

def save_audit(report: dict) -> dict:
    sb = _sb()
    if not sb:
        return report
    row = {
        "id":                str(uuid.uuid4()),
        "url":               report.get("url", ""),
        "pages_audited":     report.get("pages_audited", []),
        "overall_score":     report.get("overall_score", 0),
        "executive_summary": report.get("executive_summary", ""),
        "critical_issues":   report.get("critical_issues", []),
        "high_issues":       report.get("high_issues", []),
        "medium_issues":     report.get("medium_issues", []),
        "strengths":         report.get("strengths", []),
        "heuristic_map":     report.get("heuristic_map", {}),
        "fix_roadmap":       report.get("fix_roadmap", []),
        "notes":             report.get("notes", ""),
        "created_at":        report.get("audited_at", datetime.now(timezone.utc).isoformat()),
    }
    try:
        res = sb.table("design_audits").insert(row).execute()
        saved = (res.data or [{}])[0]
        return {**report, "id": saved.get("id", row["id"])}
    except Exception as e:
        print(f"[design_audit] save error: {e}")
        return {**report, "id": row["id"]}


def get_audits(limit: int = 20) -> list:
    sb = _sb()
    if not sb:
        return []
    return (
        sb.table("design_audits")
          .select("id, url, overall_score, executive_summary, pages_audited, created_at")
          .order("created_at", desc=True)
          .limit(limit).execute().data or []
    )


def get_audit(audit_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    res = sb.table("design_audits").select("*").eq("id", audit_id).limit(1).execute()
    return (res.data or [None])[0]


def delete_audit(audit_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("design_audits").delete().eq("id", audit_id).execute()
    return True
