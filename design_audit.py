"""
Lumynor Design Audit Agent
Evaluates web pages against the Lumynor UI/UX Design Principles (v1.0, June 2026).

Fetches the live URL with a headless Playwright browser (renders React/SPAs fully),
then queries Google PageSpeed Insights — so every finding is grounded in real data.
"""
import json
import re
import shutil
import uuid
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from db import _sb, get_settings


# ── Live page fetcher (Playwright headless browser) ───────────────────────────

def _find_system_chromium() -> str | None:
    """Return path to a system-installed Chromium binary, or None."""
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _fetch_page_playwright(url: str) -> dict:
    """
    Render the page with a headless Chromium (waits for React/SPA hydration)
    then extract the full live DOM.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    result = {
        "url": url,
        "fetch_error": None,
        "render_method": "playwright",
        "is_spa": False,          # We rendered it — content is fully visible
        "status_code": None,
        "title": None,
        "meta_description": None,
        "h1s": [],
        "h2s": [],
        "h3s": [],
        "nav_links": [],
        "button_texts": [],
        "link_texts": [],
        "images_total": 0,
        "images_missing_alt": 0,
        "external_links_no_target": 0,
        "footer_links": [],
        "form_fields": 0,
        "has_favicon": False,
        "primary_ctas": [],
        "audience_statement": None,
        "hero_text": None,
        "https": url.startswith("https://"),
        "body_text": "",
    }

    try:
        chromium_path = _find_system_chromium()
        with sync_playwright() as pw:
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                    "--disable-web-security",
                ],
            }
            if chromium_path:
                launch_kwargs["executable_path"] = chromium_path

            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )
            page = context.new_page()

            try:
                resp = page.goto(url, wait_until="networkidle", timeout=30_000)
                result["status_code"] = resp.status if resp else None
                # Extra wait for React hydration / lazy mounts
                page.wait_for_timeout(2_500)

                result["title"] = page.title()

                # Meta description
                meta = page.query_selector('meta[name="description"]')
                if meta:
                    result["meta_description"] = meta.get_attribute("content")

                # Favicon
                result["has_favicon"] = bool(
                    page.query_selector('link[rel*="icon"]')
                )

                # Headings
                result["h1s"] = [
                    el.text_content().strip()
                    for el in page.query_selector_all("h1")
                    if el.text_content().strip()
                ][:5]
                result["h2s"] = [
                    el.text_content().strip()
                    for el in page.query_selector_all("h2")
                    if el.text_content().strip()
                ][:8]
                result["h3s"] = [
                    el.text_content().strip()
                    for el in page.query_selector_all("h3")
                    if el.text_content().strip()
                ][:8]

                # Navigation — deduplicate to avoid counting mobile menu items twice
                # (both desktop and hidden mobile menus live inside <nav> in the DOM)
                nav_raw = [
                    el.text_content().strip()
                    for el in page.query_selector_all("nav a")
                    if el.text_content().strip()
                ]
                result["nav_links"] = list(dict.fromkeys(nav_raw))

                # Buttons
                result["button_texts"] = [
                    el.text_content().strip()
                    for el in page.query_selector_all("button")
                    if el.text_content().strip()
                ][:15]

                # Links sample
                result["link_texts"] = [
                    el.text_content().strip()
                    for el in page.query_selector_all("a")
                    if el.text_content().strip()
                ][:30]

                # Footer links
                result["footer_links"] = [
                    el.text_content().strip()
                    for el in page.query_selector_all("footer a")
                    if el.text_content().strip()
                ]

                # Images
                imgs = page.query_selector_all("img")
                result["images_total"] = len(imgs)
                result["images_missing_alt"] = len(
                    [i for i in imgs if not i.get_attribute("alt")]
                )

                # External links without target=_blank
                ext = page.query_selector_all('a[href^="http"]')
                result["external_links_no_target"] = len(
                    [a for a in ext if not a.get_attribute("target")]
                )

                # Form fields
                result["form_fields"] = len(
                    page.query_selector_all("input, select, textarea")
                )

                # Primary CTA elements — btn-primary styled links/buttons are the
                # explicit conversion actions (e.g. "Start Your Project", "Contact Us")
                result["primary_ctas"] = [
                    el.text_content().strip()
                    for el in page.query_selector_all('[class*="btn-primary"]')
                    if el.text_content().strip()
                ][:8]

                # Target audience statement — explicitly labelled element that answers
                # "who is this for?" Use aria-label="target-audience" on the element.
                audience_el = page.query_selector('[aria-label="target-audience"]')
                if audience_el:
                    result["audience_statement"] = audience_el.text_content().strip()

                # Hero section text — first <section> captures the above-fold content
                # including audience tag, H1, and primary CTA copy
                first_section = page.query_selector("section")
                if first_section:
                    result["hero_text"] = " ".join(first_section.inner_text().split())[:600]

                # Body text (rendered)
                body_text = " ".join(page.inner_text("body").split())
                result["body_text"] = body_text[:2000]

            except PWTimeout:
                result["fetch_error"] = "Page load timed out after 30s"
            finally:
                browser.close()

    except ImportError:
        raise  # let caller fall back to HTTP
    except Exception as exc:
        result["fetch_error"] = str(exc)[:300]

    return result


def _fetch_page_http(url: str) -> dict:
    """
    Plain HTTP fallback — only works for server-rendered pages.
    Detects JS SPAs and marks content as unverifiable.
    """
    result = {
        "url": url,
        "fetch_error": None,
        "render_method": "http",
        "is_spa": False,
        "status_code": None,
        "title": None,
        "meta_description": None,
        "h1s": [],
        "h2s": [],
        "h3s": [],
        "nav_links": [],
        "button_texts": [],
        "link_texts": [],
        "images_total": 0,
        "images_missing_alt": 0,
        "external_links_no_target": 0,
        "footer_links": [],
        "form_fields": 0,
        "has_favicon": False,
        "primary_ctas": [],
        "audience_statement": None,
        "hero_text": None,
        "https": url.startswith("https://"),
        "body_text": "",
    }

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LumynorAuditBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        result["status_code"] = resp.status_code
        soup = BeautifulSoup(resp.text, "html.parser")

        body_text = " ".join(soup.get_text(" ", strip=True).split())
        result["is_spa"] = (
            len(body_text) < 300
            and bool(soup.find("div", id=re.compile(r"^(root|app)$")))
        )

        result["title"] = soup.title.get_text(strip=True) if soup.title else None
        meta = soup.find("meta", {"name": "description"})
        result["meta_description"] = meta.get("content", "").strip() if meta else None
        result["has_favicon"] = bool(
            soup.find("link", rel=lambda r: r and any("icon" in x for x in r))
        )

        if not result["is_spa"]:
            result["h1s"]          = [h.get_text(strip=True) for h in soup.find_all("h1")][:5]
            result["h2s"]          = [h.get_text(strip=True) for h in soup.find_all("h2")][:8]
            result["h3s"]          = [h.get_text(strip=True) for h in soup.find_all("h3")][:8]
            result["nav_links"]    = [a.get_text(strip=True) for a in soup.select("nav a") if a.get_text(strip=True)]
            result["button_texts"] = [b.get_text(strip=True) for b in soup.find_all("button") if b.get_text(strip=True)][:15]
            result["link_texts"]   = [a.get_text(strip=True) for a in soup.find_all("a") if a.get_text(strip=True)][:30]
            result["footer_links"] = [a.get_text(strip=True) for a in soup.select("footer a")]

            imgs = soup.find_all("img")
            result["images_total"]       = len(imgs)
            result["images_missing_alt"] = len([i for i in imgs if not i.get("alt")])

            ext = [a for a in soup.find_all("a", href=True) if a["href"].startswith("http")]
            result["external_links_no_target"] = len([a for a in ext if not a.get("target")])

            result["form_fields"] = len(soup.find_all(["input", "select", "textarea"]))
            result["body_text"]   = body_text[:2000]

    except Exception as exc:
        result["fetch_error"] = str(exc)[:200]

    return result


def _fetch_page(url: str) -> dict:
    """Try Playwright first; fall back to plain HTTP if not installed or fails."""
    try:
        return _fetch_page_playwright(url)
    except ImportError:
        print("[design_audit] playwright not installed — using HTTP fallback")
    except Exception as exc:
        print(f"[design_audit] Playwright failed ({exc}) — using HTTP fallback")
    return _fetch_page_http(url)


# ── PageSpeed Insights ────────────────────────────────────────────────────────

def _fetch_pagespeed(url: str) -> dict:
    """
    Call Google PageSpeed Insights API (unauthenticated, mobile strategy).
    Returns real Core Web Vitals and Lighthouse scores.
    """
    try:
        api = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url={url}&strategy=mobile"
        resp = requests.get(api, timeout=45)
        if resp.status_code != 200:
            return {"error": f"PageSpeed API returned {resp.status_code}"}

        data   = resp.json()
        cats   = data.get("lighthouseResult", {}).get("categories", {})
        audits = data.get("lighthouseResult", {}).get("audits", {})

        def score(key):
            s = cats.get(key, {}).get("score")
            return round(s * 100) if s is not None else "n/a"

        def metric(key):
            return audits.get(key, {}).get("displayValue", "n/a")

        return {
            "performance":     score("performance"),
            "accessibility":   score("accessibility"),
            "seo":             score("seo"),
            "best_practices":  score("best-practices"),
            "lcp":             metric("largest-contentful-paint"),
            "cls":             metric("cumulative-layout-shift"),
            "tbt":             metric("total-blocking-time"),
            "speed_index":     metric("speed-index"),
            "fcp":             metric("first-contentful-paint"),
            "images_not_lazy": audits.get("uses-lazy-loading", {}).get("score"),
            "image_alts":      audits.get("image-alt", {}).get("score"),
            "tap_targets":     audits.get("tap-targets", {}).get("score"),
        }
    except Exception as exc:
        return {"error": str(exc)[:200]}


# ── Context builder ───────────────────────────────────────────────────────────

def _build_context(page: dict, pagespeed: dict) -> str:
    """Serialise fetched data into a readable block for the LLM prompt."""
    lines = [f"=== LIVE PAGE DATA: {page['url']} ==="]

    if page.get("fetch_error"):
        lines.append(f"⚠ Fetch failed: {page['fetch_error']}")
    else:
        method = page.get("render_method", "http")
        lines.append(f"Render method: {method.upper()} | Status: {page['status_code']} | HTTPS: {page['https']}")

        if page["is_spa"]:
            lines.append("⚠ JavaScript SPA detected — HTML content is client-rendered. Only <title> and <meta> available.")
        else:
            if method == "playwright":
                lines.append("✓ Full React/SPA rendered by headless browser — all DOM content is accurate.")

        lines.append(f"Title: {page['title']}")
        lines.append(f"Meta description: {page['meta_description']}")
        lines.append(f"Favicon: {'yes' if page['has_favicon'] else 'no'}")

        if not page["is_spa"]:
            lines.append(f"H1 headings: {page['h1s']}")
            lines.append(f"H2 headings: {page['h2s']}")
            lines.append(f"Navigation links (actual labels, deduplicated): {page['nav_links']}")
            # Target audience statement (aria-label="target-audience")
            if page.get("audience_statement"):
                lines.append(f"★ TARGET AUDIENCE STATEMENT (explicit element with aria-label='target-audience', visible above H1): \"{page['audience_statement']}\"")
                lines.append("  → C6-04 PASSES: the page explicitly answers 'Who is this for?' via this element.")
            # Primary CTAs are the most important conversion elements
            if page.get("primary_ctas"):
                lines.append(f"★ PRIMARY CTA ELEMENTS (btn-primary styled — these are the main conversion actions visible on the page): {page['primary_ctas']}")
            else:
                lines.append("Primary CTA elements: none detected via btn-primary class")
            lines.append(f"Button texts (all buttons): {page['button_texts']}")
            lines.append(f"Link texts (sample): {page['link_texts'][:20]}")
            lines.append(f"Footer links: {page['footer_links']}")
            lines.append(f"Images: {page['images_total']} total, {page['images_missing_alt']} missing alt attribute")
            lines.append(f"External links without target=_blank: {page['external_links_no_target']}")
            lines.append(f"Form input fields: {page['form_fields']}")
            if page.get("hero_text"):
                lines.append(f"★ HERO SECTION TEXT (above-fold content — first section rendered): {page['hero_text']}")
            if page["body_text"]:
                lines.append(f"Body text sample:\n{page['body_text']}")

    lines.append("")
    lines.append("=== GOOGLE PAGESPEED INSIGHTS (Mobile) ===")
    if "error" in pagespeed:
        lines.append(f"⚠ PageSpeed API error: {pagespeed['error']}")
        lines.append("  Do NOT invent performance numbers. Mark C5-01 and C5-02 as 'Requires PageSpeed data — cannot verify.'")
    else:
        lines.append(f"Performance: {pagespeed['performance']}/100")
        lines.append(f"Accessibility: {pagespeed['accessibility']}/100")
        lines.append(f"SEO: {pagespeed['seo']}/100")
        lines.append(f"Best Practices: {pagespeed['best_practices']}/100")
        lines.append(f"LCP (Largest Contentful Paint): {pagespeed['lcp']}")
        lines.append(f"CLS (Cumulative Layout Shift): {pagespeed['cls']}")
        lines.append(f"TBT (Total Blocking Time / FID proxy): {pagespeed['tbt']}")
        lines.append(f"FCP (First Contentful Paint): {pagespeed['fcp']}")
        lines.append(f"Speed Index: {pagespeed['speed_index']}")
        lines.append(f"Lazy loading score: {pagespeed['images_not_lazy']}")
        lines.append(f"Image alt attribute score: {pagespeed['image_alts']}")
        lines.append(f"Tap target size score: {pagespeed['tap_targets']}")

    return "\n".join(lines)


# ── System prompt ─────────────────────────────────────────────────────────────

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

HEURISTIC MAP — assess each Nielsen heuristic:
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
  "high_issues": [...],
  "medium_issues": [...],
  "strengths": [
    {"area": "<strength area>", "detail": "<what specifically is working>", "principle": "<principle it satisfies>"}
  ],
  "heuristic_map": {"H1": "pass", "H2": "fail", ...},
  "fix_roadmap": [
    {"priority": 1, "action": "<specific action>", "impact": "High", "effort": "Low", "principle": "<principle>"}
  ]
}

ABSOLUTE RULES — violating any of these makes the audit worthless:
1. The LIVE PAGE DATA above was captured by a headless browser that fully rendered the React app. It is authoritative. Nav labels, headings, button texts, and link texts shown are EXACTLY what users see.
2. NEVER invent or assume content not present in the LIVE PAGE DATA. If nav_links shows ['Work', 'Products', 'Blog', 'About'], those are the real labels — report them verbatim.
3. For checklist items that CAN be verified from fetched data (nav labels, headings, image alt, HTTPS, footer links, PageSpeed scores), your finding MUST match the data exactly.
4. For items that require visual inspection (colour contrast ratios, whitespace quality, animation quality), note: "Requires visual review — cannot verify from rendered source."
5. For performance items: USE the PageSpeed scores provided. If PageSpeed errored, write "PageSpeed unavailable — cannot verify." NEVER invent LCP/CLS values.
6. Overall score: start at 10. Deduct 1 per Critical fail (evidence-based only). Deduct 0.5 per High fail (evidence-based only). Items that cannot be verified do NOT cause deductions.
7. Every finding must include: a principle citation AND a specific, actionable fix. Generic fixes are not acceptable.
8. PRIMARY CTA IDENTIFICATION (C1-02): The "★ PRIMARY CTA ELEMENTS" field lists all btn-primary styled elements — these are the actual high-prominence conversion buttons/links visible on the page. If this field lists items like "Start Your Project", that CTA IS present and visible. Do NOT flag C1-02 as failing if primary CTAs are listed. The nav_links field contains navigation links, not the primary hero CTA.
9. NAVIGATION COUNT (C2-01): The nav_links field is already deduplicated. Count only the unique items shown. Do NOT report duplicates unless the exact same label appears twice in the deduplicated list.
10. HERO CONTENT AND AUDIENCE (C1-01, C6-04): If the "★ TARGET AUDIENCE STATEMENT" field is present, C6-04 PASSES — the page explicitly answers "Who is this for?" NEVER mark C6-04 as failing when this field exists. For C1-01: if the H1 and hero_text are present, the hero communicates a value proposition and C1-01 passes.
11. HOVER AND ACTIVE STATES (C4-01): CSS :hover and :active pseudo-states CANNOT be observed from a static rendered page. A Playwright render captures the initial idle state only. NEVER flag C4-01 as failing based on a static render — mark it as "Requires interactive testing — cannot verify from static DOM render." Only flag if the auditor's observations explicitly note missing hover states.
12. LOADING INDICATORS (C4-07): Loading spinners and progress bars are dynamic states that only appear AFTER a user submits a form or triggers an async action. A static Playwright render will NEVER show them. NEVER flag C4-07 as failing based on static render — mark as "Requires form submission testing — cannot verify from static render." Only flag if the auditor explicitly reports that submitting a form shows no feedback.
"""


# ── Core audit function ───────────────────────────────────────────────────────

def run_audit(url: str, pages: list = None, notes: str = "", auditor_notes: str = "") -> dict:
    """
    Run a full design audit:
    1. Render the live URL with Playwright (falls back to HTTP)
    2. Query Google PageSpeed Insights
    3. Build a factual context block
    4. Ask the LLM to evaluate against the 41-item checklist
    """
    try:
        from auto_blogger import _build_llm_cfg, _llm
    except ImportError:
        return {"error": "LLM module not available"}

    stored     = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")
    if not gemini_key and stored.get("llmProvider", "gemini") == "gemini":
        return {"error": "LLM not configured — set API key in Settings → Auto Blogger"}

    # ── Step 1: fetch live data ───────────────────────────────────────────────
    page_data = _fetch_page(url)
    pagespeed = _fetch_pagespeed(url)
    live_ctx  = _build_context(page_data, pagespeed)

    # ── Step 2: build prompt ──────────────────────────────────────────────────
    pages_str   = ", ".join(pages) if pages else "Homepage"
    notes_block = f"\nAUDITOR OBSERVATIONS (manual notes from reviewer):\n{notes.strip()}" if notes.strip() else ""
    extra_block = f"\nADDITIONAL CONTEXT:\n{auditor_notes.strip()}" if auditor_notes.strip() else ""

    render_note = (
        "The page was rendered by a headless Chromium browser, so all React/SPA content is fully visible."
        if page_data.get("render_method") == "playwright"
        else "The page could not be rendered by browser — content may be incomplete if it's a JavaScript SPA."
    )

    user_prompt = f"""Audit the website below. Use the LIVE PAGE DATA as ground truth.

URL: {url}
Pages to evaluate: {pages_str}
Render note: {render_note}

{live_ctx}
{notes_block}
{extra_block}

INSTRUCTIONS:
- Every finding based on nav labels, headings, button texts, footer links, or PageSpeed scores MUST quote the actual fetched value.
- The rendered DOM data is accurate — do not second-guess it.
- If PageSpeed errored, do not penalise performance items — mark them as unverifiable.
- For visual-only items (contrast ratios, animation quality), note "Requires visual review."
- Do NOT assume anything not present in the data. If you cannot verify it, say so.

Return ONLY valid JSON — no markdown fences, no explanation outside the JSON."""

    # ── Step 3: LLM evaluation ────────────────────────────────────────────────
    try:
        llm_cfg = _build_llm_cfg(stored, gemini_key)
        raw = _llm(
            f"{_SYSTEM_PROMPT}\n\n{user_prompt}",
            llm_cfg,
            json_mode=False,
            timeout=120,
            max_tokens=4000,
        )

        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            return {"error": "LLM did not return valid JSON", "raw": raw[:500]}

        report = json.loads(match.group())
        report["url"]           = url
        report["audited_at"]    = datetime.now(timezone.utc).isoformat()
        report["notes"]         = notes
        report["pagespeed"]     = pagespeed
        report["is_spa"]        = page_data.get("is_spa", False)
        report["render_method"] = page_data.get("render_method", "http")
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
