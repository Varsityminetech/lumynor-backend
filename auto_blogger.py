"""
Lumynor Auto Blogger — Full Pipeline
Trending Research → Keyword Research → Web Research → Longform Writing → SEO → Images
"""

import os
import json
import urllib.request
import urllib.parse
import urllib.error
import re
from datetime import datetime

# ── LLM HELPERS (Gemini + Ollama Cloud) ─────────────────────────────────────────
# Every pipeline stage calls _llm(prompt, llm_cfg, ...). llm_cfg is either a bare
# Gemini API key string (back-compat) or a config dict from _build_llm_cfg().

# Best Ollama Cloud model for long-form blog writing with reliable JSON output.
_OLLAMA_DEFAULT_MODEL = "gpt-oss:120b"  # alternatives: deepseek-v3.1:671b, glm-4.6, kimi-k2


def _build_llm_cfg(settings: dict, gemini_key: str) -> dict:
    """Decide which LLM the pipeline uses. If an Ollama Cloud key is present
    (settings or OLLAMA_API_KEY env), use Ollama Cloud; otherwise Gemini."""
    name = (settings.get("llmApiName") or "").lower()
    ollama_key = (settings.get("llmApiKey", "") if name in ("ollama_cloud", "ollama") else "") \
        or os.getenv("OLLAMA_API_KEY", "")
    if ollama_key:
        # Per-task model routing. Heavy writing uses a quality-first fallback
        # chain (each tried in order until one succeeds — handles models that
        # need a higher plan, e.g. deepseek 403s and skips to the next). Light
        # JSON stages use a small fast model.
        writing_models = [m.strip() for m in (os.getenv("OLLAMA_WRITING_MODELS", "") or "").split(",") if m.strip()] \
            or ["glm-4.6", "deepseek-v3.1:671b", "gpt-oss:120b"]
        fast_model = os.getenv("OLLAMA_MODEL_FAST") or "gpt-oss:20b"
        return {
            "provider": "ollama_cloud",
            "model": writing_models[0],           # default / non-task calls
            "writing_models": writing_models,     # quality-first fallback chain
            "fast_model": fast_model,             # light structured/JSON stages
            "ollama_key": ollama_key,
            "ollama_host": settings.get("llmBaseUrl") or os.getenv("OLLAMA_HOST") or "https://ollama.com",
        }
    return {
        "provider": "gemini",
        "gemini_key": gemini_key or settings.get("llmApiKey", "") or os.getenv("GEMINI_API_KEY", ""),
    }


def _llm(prompt: str, llm_cfg, json_mode: bool = False, timeout: int = 60, max_tokens: int = 8192, task: str = "fast") -> str:
    """Dispatch a text-generation call to the configured provider.
    task="writing" uses the quality-first fallback chain; otherwise a fast model."""
    if isinstance(llm_cfg, str):  # back-compat: bare Gemini key
        llm_cfg = {"provider": "gemini", "gemini_key": llm_cfg}
    if llm_cfg.get("provider") in ("ollama_cloud", "ollama"):
        if task == "writing":
            models = llm_cfg.get("writing_models") or [llm_cfg.get("model")]
        else:
            models = [llm_cfg.get("fast_model") or llm_cfg.get("model")]
        last_err = None
        for m in [x for x in models if x]:
            try:
                return _ollama_generate(prompt, llm_cfg, json_mode, timeout, max_tokens, model=m)
            except Exception as e:
                last_err = e
                print(f"[ollama_cloud] model '{m}' failed ({str(e)[:80]}); trying next in chain...")
                continue
        if last_err:
            raise last_err
        raise RuntimeError("No Ollama Cloud model available")
    return _gemini_generate(prompt, llm_cfg.get("gemini_key", ""), json_mode, timeout, max_tokens)


def _gemini_generate(prompt: str, api_key: str, json_mode: bool = False, timeout: int = 60, max_tokens: int = 8192) -> str:
    """Call Gemini 2.5 Flash with retry/backoff on transient errors (429/503)."""
    import time
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": max_tokens,
        }
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["temperature"] = 0.5

    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 503, 500) and attempt < 3:
                wait = (2 ** attempt) * 8  # 8s, 16s, 32s backoff
                print(f"[gemini] {e.code} on attempt {attempt+1}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep(5)
                continue
            raise
    if last_err:
        raise last_err


def _ollama_generate(prompt: str, cfg: dict, json_mode: bool = False, timeout: int = 60, max_tokens: int = 8192, model: str = None) -> str:
    """Call an Ollama Cloud model (https://ollama.com) with retry/backoff.
    Uses the standard Ollama /api/chat shape with Bearer auth + format=json."""
    import time
    host = (cfg.get("ollama_host") or "https://ollama.com").rstrip("/")
    # Ollama model names are lowercase; normalize so a config typo (e.g.
    # "GPT-OSS:120B") doesn't 404 as "model not found".
    model = (model or cfg.get("model") or _OLLAMA_DEFAULT_MODEL).strip().lower()
    key = cfg.get("ollama_key", "")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.5 if json_mode else 0.85, "num_predict": max_tokens},
    }
    if json_mode:
        payload["format"] = "json"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(f"{host}/api/chat", data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
                return (result.get("message", {}).get("content", "") or "").strip()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 502, 503, 500) and attempt < 3:
                wait = (2 ** attempt) * 5
                print(f"[ollama_cloud] {e.code} on attempt {attempt+1}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep(5)
                continue
            raise
    if last_err:
        raise last_err


def _parse_json_lenient(text: str) -> dict:
    """Parse JSON, attempting repair if truncated (e.g. hit token limit)."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Attempt repair of truncated JSON: close open string/braces
        repaired = text
        # If ends mid-string, close the quote
        quote_count = repaired.count('"') - repaired.count('\\"')
        if quote_count % 2 == 1:
            repaired += '"'
        # Balance braces/brackets
        open_braces = repaired.count('{') - repaired.count('}')
        open_brackets = repaired.count('[') - repaired.count(']')
        repaired += ']' * max(0, open_brackets)
        repaired += '}' * max(0, open_braces)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # Last resort: extract the largest valid prefix object
            raise


def _get_ddgs():
    """Import DDGS from either the new 'ddgs' package or legacy 'duckduckgo_search'."""
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            return DDGS
        except ImportError:
            return None


def _search_web(query: str, num: int = 8) -> list:
    """DuckDuckGo instant answer search — returns list of {title, url, snippet}."""
    DDGS = _get_ddgs()
    if DDGS is None:
        print("[search] No DDGS package available")
        return []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=num))
        return [{"title": r.get("title", ""), "url": r.get("href", "") or r.get("url", ""), "snippet": r.get("body", "")} for r in results]
    except Exception as e:
        print(f"[search] Error: {e}")
        return []


# ── STAGE 1: TRENDING TOPIC RESEARCH ──────────────────────────────────────────

def research_trending_topics(niche: str, keywords: str, gemini_key: str) -> dict:
    """Find the best trending blog topic for this niche right now."""

    # Web search for trending topics
    search_queries = [
        f"trending topics in {niche} 2025",
        f"{niche} latest news trends {datetime.now().strftime('%B %Y')}",
        f"most searched {niche} questions 2025",
    ]

    all_results = []
    for q in search_queries:
        all_results.extend(_search_web(q, 5))

    # Deduplicate by URL
    seen = set()
    unique = []
    for r in all_results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    # Ask Gemini to pick the best topic
    snippets = "\n".join([f"- [{r['title']}]({r['url']}): {r['snippet'][:200]}" for r in unique[:15]])

    prompt = f"""You are a content strategist for a tech/digital product company.
Niche: {niche}
Additional keywords: {keywords}
Date: {datetime.now().strftime('%B %Y')}

Based on these trending web results:
{snippets}

Pick the SINGLE best blog topic that:
1. Is trending RIGHT NOW
2. Has high search demand
3. Can rank with a well-written article
4. Is relevant to the niche: {niche}

Respond ONLY with JSON:
{{
  "topic": "exact blog topic title",
  "angle": "unique angle or perspective to take",
  "why_trending": "one sentence why this is trending",
  "target_audience": "who this blog is for",
  "search_intent": "informational | transactional | navigational | commercial"
}}"""

    result = _llm(prompt, gemini_key, json_mode=True)
    try:
        return json.loads(result)
    except:
        return {"topic": f"The Future of {niche} in 2025", "angle": "Comprehensive guide", "why_trending": "Growing interest", "target_audience": "professionals", "search_intent": "informational"}


# ── STAGE 2: SEO KEYWORD RESEARCH ─────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "with", "by",
    "is", "are", "this", "that", "your", "you", "how", "what", "why", "best",
    "top", "guide", "future", "2024", "2025", "2026",
}


def _keyword_from_topic(topic: str) -> str:
    """Derive a meaningful 2-3 word keyword from a topic (drops stopwords)."""
    words = re.findall(r"[A-Za-z0-9]+", topic or "")
    kept = [w for w in words if w.lower() not in _STOPWORDS]
    return " ".join(kept[:3]) if kept else (topic or "technology").strip()


def _clean_primary_keyword(kw: str, topic: str) -> str:
    """Reject empty/stopword/garbage primary keywords; fall back to the topic."""
    kw = (kw or "").strip().strip(".,;:!?\"'")
    words = kw.split()
    if not kw or len(kw) < 3 or len(words) > 5 or all(w.lower() in _STOPWORDS for w in words):
        return _keyword_from_topic(topic)
    return kw


def _clean_keyword_list(items) -> list:
    """Trim, de-punctuate, drop empties, and de-dupe (case-insensitive)."""
    out, seen = [], set()
    for it in (items or []):
        k = str(it).strip().strip(".,;:!?\"'")
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def _clamp_summary(text: str, hi: int = 160) -> str:
    """Trim a meta description/summary to the SEO sweet spot (<=160 chars),
    cutting on a word boundary with an ellipsis."""
    s = re.sub(r'\s+', ' ', (text or '').strip())
    if len(s) <= hi:
        return s
    cut = s[:hi - 1]
    if ' ' in cut:
        cut = cut[:cut.rfind(' ')]
    return cut.rstrip(' .,;:') + '…'


# Internal-link targets, matched by keyword in the anchor text (else round-robin).
_INTERNAL_LINK_MAP = [
    (("agent forge", "agentforge", "agent-forge", "scaffold", "build saas", "saas builder"), "/products/agent-forge"),
    (("district 21", "district21", "ticketing", "event ticket"), "/products/district-21"),
    (("hotel", "hospitality"), "/products/hotel-os"),
    (("school", "education", "admission"), "/products/school-os"),
    (("contact", "get in touch", "talk to", "consultation", "hire", "work with"), "/contact"),
    (("about", "team", "who we are", "our story"), "/about"),
    (("blog", "article", "read more", "insights", "guide"), "/blog"),
]
_INTERNAL_LINK_ROTATION = ["/contact", "/products/agent-forge", "/blog", "/about"]


def _resolve_content_placeholders(content: str) -> str:
    """Clean up the LLM's leftover markers: drop [IMAGE: ...] (images are
    embedded as <figure>s) and turn [INTERNAL: text] into real internal links."""
    if not content:
        return content
    import itertools
    content = re.sub(r'\[IMAGE:[^\]]*\]', '', content, flags=re.I)
    rot = itertools.cycle(_INTERNAL_LINK_ROTATION)

    def _link(m):
        text = m.group(1).strip().strip('|').strip()
        low = text.lower()
        for kws, url in _INTERNAL_LINK_MAP:
            if any(k in low for k in kws):
                return f'<a href="{url}">{text}</a>'
        return f'<a href="{next(rot)}">{text}</a>'

    content = re.sub(r'\[INTERNAL:\s*([^\]]+)\]', _link, content, flags=re.I)
    # Strip any other stray [PLACEHOLDER: ...] markers that slipped through.
    content = re.sub(r'\[[A-Z][A-Z _-]{2,}:[^\]]*\]', '', content)
    return content


def do_keyword_research(topic: str, niche: str, gemini_key: str) -> dict:
    """Research primary + secondary + LSI keywords for SEO."""

    # Search for related queries
    search_results = _search_web(f"{topic} guide complete tutorial", 5)
    search_results += _search_web(f"best {topic} tips strategies", 5)

    snippets = "\n".join([f"- {r['title']}: {r['snippet'][:150]}" for r in search_results[:10]])

    prompt = f"""You are an expert SEO keyword researcher.
Topic: {topic}
Niche: {niche}

Related search results found:
{snippets}

Conduct keyword research and respond ONLY with JSON:
{{
  "primary_keyword": "main keyword (1-3 words, highest volume)",
  "secondary_keywords": ["keyword2", "keyword3", "keyword4", "keyword5"],
  "lsi_keywords": ["lsi1", "lsi2", "lsi3", "lsi4", "lsi5"],
  "search_volume": "estimated monthly searches (e.g. 10K-50K)",
  "keyword_difficulty": "low | medium | high",
  "people_also_ask": [
    "question people ask 1",
    "question people ask 2",
    "question people ask 3",
    "question people ask 4",
    "question people ask 5"
  ],
  "meta_title": "SEO title 50-60 chars with primary keyword",
  "meta_description": "SEO description 140-160 chars with primary keyword and CTA"
}}"""

    # Keyword research drives the whole article's SEO targeting — use the strong
    # writing model (not the tiny fast one, which produced garbage keywords).
    result = _llm(prompt, gemini_key, json_mode=True, task="writing")
    try:
        data = json.loads(result)
    except Exception:
        data = {
            "primary_keyword": _keyword_from_topic(topic),
            "secondary_keywords": [niche, topic],
            "lsi_keywords": [],
            "search_volume": "1K-10K",
            "keyword_difficulty": "medium",
            "people_also_ask": [],
            "meta_title": topic[:60],
            "meta_description": f"Learn about {topic} in this comprehensive guide.",
        }
    # Sanitize regardless of source: never let a stopword/garbage keyword through.
    data["primary_keyword"] = _clean_primary_keyword(data.get("primary_keyword", ""), topic)
    data["secondary_keywords"] = _clean_keyword_list(data.get("secondary_keywords"))
    data["lsi_keywords"] = _clean_keyword_list(data.get("lsi_keywords"))
    return data


# ── STAGE 3: WEB RESEARCH & FACT GATHERING ────────────────────────────────────

def research_topic_facts(topic: str, keywords: dict, gemini_key: str) -> dict:
    """Gather real facts, statistics, and references from the web."""

    primary_kw = keywords.get("primary_keyword", topic)

    research_queries = [
        f"{topic} statistics data 2024 2025",
        f"{topic} research findings studies",
        f"{primary_kw} expert insights examples",
        f"{topic} case studies results",
    ]

    all_snippets = []
    all_refs = []

    for q in research_queries:
        results = _search_web(q, 4)
        for r in results:
            if r.get("snippet"):
                all_snippets.append(f"Source: {r['title']} ({r['url']})\n{r['snippet']}")
            if r.get("url") and r.get("title"):
                all_refs.append({"title": r["title"], "url": r["url"]})

    research_text = "\n\n".join(all_snippets[:12])

    prompt = f"""You are a research analyst. Extract and organize key facts for a blog post.

Topic: {topic}
Primary Keyword: {primary_kw}

Web research gathered:
{research_text[:4000]}

Organize this into a research brief. Respond ONLY with JSON:
{{
  "key_statistics": [
    "Stat 1 with specific number and source context",
    "Stat 2 with specific number and source context",
    "Stat 3 with specific number and source context",
    "Stat 4 with specific number and source context"
  ],
  "key_facts": [
    "Important fact 1",
    "Important fact 2",
    "Important fact 3",
    "Important fact 4",
    "Important fact 5"
  ],
  "expert_insights": [
    "Expert insight or quote 1",
    "Expert insight or quote 2"
  ],
  "blog_outline": [
    {{
      "section": "Introduction",
      "key_points": ["hook", "problem statement", "what reader will learn"]
    }},
    {{
      "section": "H2 Section Title 1",
      "key_points": ["point1", "point2", "point3"]
    }},
    {{
      "section": "H2 Section Title 2",
      "key_points": ["point1", "point2", "point3"]
    }},
    {{
      "section": "H2 Section Title 3",
      "key_points": ["point1", "point2", "point3"]
    }},
    {{
      "section": "H2 Section Title 4 with primary keyword",
      "key_points": ["point1", "point2", "point3"]
    }},
    {{
      "section": "Conclusion",
      "key_points": ["summary", "call to action"]
    }}
  ]
}}"""

    result = _llm(prompt, gemini_key, json_mode=True, timeout=45)
    try:
        research = json.loads(result)
        research["references"] = all_refs[:8]
        return research
    except:
        return {
            "key_statistics": [],
            "key_facts": [],
            "expert_insights": [],
            "blog_outline": [
                {"section": "Introduction", "key_points": []},
                {"section": f"Understanding {topic}", "key_points": []},
                {"section": f"Key Benefits of {topic}", "key_points": []},
                {"section": f"How to Get Started with {topic}", "key_points": []},
                {"section": "Conclusion", "key_points": []}
            ],
            "references": all_refs[:6]
        }


# ── STAGE 4: LONGFORM HUMAN-LIKE BLOG WRITING ─────────────────────────────────

def write_longform_blog(topic: str, angle: str, keywords: dict, research: dict, target_audience: str, gemini_key: str) -> dict:
    """Write a full longform human-like blog post with all SEO elements."""

    primary_kw = keywords.get("primary_keyword", topic)
    secondary_kws = ", ".join(keywords.get("secondary_keywords", []))
    lsi_kws = ", ".join(keywords.get("lsi_keywords", []))
    paa = "\n".join([f"- {q}" for q in keywords.get("people_also_ask", [])])

    stats = "\n".join([f"• {s}" for s in research.get("key_statistics", [])])
    facts = "\n".join([f"• {f}" for f in research.get("key_facts", [])])
    expert_insights = "\n".join([f"• {e}" for e in research.get("expert_insights", [])])

    outline_text = ""
    for section in research.get("blog_outline", []):
        outline_text += f"\n**{section['section']}**\n"
        for pt in section.get("key_points", []):
            outline_text += f"  - {pt}\n"

    refs = research.get("references", [])
    refs_text = "\n".join([f"- [{r['title']}]({r['url']})" for r in refs[:6]])

    faq_questions = keywords.get("people_also_ask", [])

    prompt = f"""You are a senior content writer with 10+ years of experience writing for major tech publications.
Write a COMPREHENSIVE, LONGFORM blog post (2000-3000 words) that reads like it was written by a human expert — NOT like AI.

TOPIC: {topic}
ANGLE: {angle}
TARGET AUDIENCE: {target_audience}
PRIMARY KEYWORD: {primary_kw}
SECONDARY KEYWORDS: {secondary_kws}
LSI KEYWORDS: {lsi_kws}

RESEARCH DATA TO USE:
Statistics:
{stats}

Key Facts:
{facts}

Expert Insights:
{expert_insights}

OUTLINE TO FOLLOW:
{outline_text}

FAQ QUESTIONS TO ANSWER:
{paa}

REFERENCES TO CITE:
{refs_text}

HUMAN WRITING RULES (follow strictly):
1. Start with a compelling personal hook or real-world scenario — NOT "In today's world" or "In this article"
2. Use "you" directly — address the reader personally
3. Vary sentence length dramatically: mix short punchy sentences with longer, nuanced ones
4. Use contractions naturally (it's, you'll, don't, they're)
5. Include personal opinions and hedged claims ("In my experience...", "This is worth noting...")
6. Use transition phrases between sections ("Here's the thing...", "But wait...", "What's fascinating is...")
7. Include specific numbers, percentages, year-stamped data
8. Break up text with callout boxes, tips, key takeaways
9. Write each H2 section as a mini-article with real depth
10. Avoid: "game-changer", "revolutionize", "leverage", "delve", "in conclusion", "firstly"

SEO RULES:
1. Primary keyword "{primary_kw}" must appear in: title, first 100 words, at least 2 H2 headings, meta description
2. Use secondary keywords naturally — never force them
3. Every H2 must be actionable or curiosity-driving
4. Add 2-4 internal links as real relative anchors to relevant Lumynor pages, e.g. <a href="/products/agent-forge">Agent Forge</a>, <a href="/contact">talk to our team</a>, <a href="/blog">more insights</a>. Use natural anchor text in context.
5. Write clean HTML only — do NOT output any "[IMAGE: ...]" or "[INTERNAL: ...]" placeholder markers.

OUTPUT FORMAT — Respond ONLY with this exact JSON structure:
{{
  "title": "SEO optimized title with primary keyword (50-60 chars)",
  "meta_description": "Compelling 140-155 char description with keyword + CTA",
  "summary": "Engaging 2-3 sentence excerpt for blog listing",
  "read_time": "X min read",
  "content_html": "FULL HTML blog post (use <h2>, <h3>, <p>, <ul>, <ol>, <li>, <strong>, <em>, <blockquote>, <div class=\\"callout-tip\\">, <div class=\\"callout-warning\\">)",
  "faq": [
    {{"question": "FAQ question 1", "answer": "Detailed answer 1 (2-3 sentences)"}},
    {{"question": "FAQ question 2", "answer": "Detailed answer 2"}},
    {{"question": "FAQ question 3", "answer": "Detailed answer 3"}},
    {{"question": "FAQ question 4", "answer": "Detailed answer 4"}},
    {{"question": "FAQ question 5", "answer": "Detailed answer 5"}}
  ],
  "image_prompts": [
    {{"placement": "cover", "prompt": "detailed AI image prompt for cover image", "alt": "alt text for SEO"}},
    {{"placement": "section_1", "prompt": "detailed AI image prompt for first section illustration", "alt": "alt text"}},
    {{"placement": "section_2", "prompt": "detailed AI image prompt for second section illustration", "alt": "alt text"}}
  ],
  "references": [
    {{"title": "Reference title", "url": "https://reference-url.com"}},
    {{"title": "Reference title 2", "url": "https://reference-url2.com"}}
  ],
  "primary_keyword": "{primary_kw}",
  "secondary_keywords": "{secondary_kws}",
  "tags": ["tag1", "tag2", "tag3", "tag4"]
}}"""

    result = _llm(prompt, gemini_key, json_mode=True, timeout=180, max_tokens=32768, task="writing")
    try:
        blog = _parse_json_lenient(result)
        # Inject references into the HTML content
        if refs and blog.get("content_html"):
            ref_html = "<h2>References &amp; Sources</h2><ol>"
            for r in refs[:6]:
                ref_html += f'<li><a href="{r["url"]}" target="_blank" rel="noopener noreferrer">{r["title"]}</a></li>'
            ref_html += "</ol>"
            blog["content_html"] = blog["content_html"].rstrip() + "\n" + ref_html
        return blog
    except Exception as e:
        print(f"[write_blog] parse error: {e}, result[:200]={result[:200]}")
        raise


# ── STAGE 5: IMAGE SOURCING (web search + optional AI gen) ─────────────────────

def _image_query(img: dict) -> str:
    """Build a concise web-search query from an image prompt's alt/prompt."""
    q = (img.get("alt") or img.get("prompt") or "").strip()
    # Strip AI-prompt filler words to get a clean photo query
    for junk in ["detailed", "illustration of", "an image of", "a photo of",
                 "photorealistic", "high quality", "digital art", "3d render",
                 "vector", "minimalist", "concept art"]:
        q = q.replace(junk, "")
    q = re.sub(r'\s+', ' ', q).strip()
    return q[:80] or "technology abstract"


# Stock-photo APIs (Pexels especially) reject the default Python-urllib
# User-Agent with HTTP 403, so every image request must set a real UA.
_IMG_UA = "LumynorBlog/1.0 (+https://lumynor.com)"


# Unsplash app name — must match the registered app; used in the required UTM
# attribution links per the Unsplash API Guidelines.
_UNSPLASH_APP_NAME = "Lumynor"
_UNSPLASH_UTM = f"utm_source={_UNSPLASH_APP_NAME}&utm_medium=referral"


def _trigger_unsplash_download(download_location: str, key: str) -> None:
    """Unsplash Guidelines require pinging a photo's download_location endpoint
    whenever the photo is used in the app. Fire-and-forget; non-fatal."""
    if not download_location:
        return
    try:
        sep = "&" if "?" in download_location else "?"
        req = urllib.request.Request(
            f"{download_location}{sep}client_id={key}",
            headers={"User-Agent": _IMG_UA},
        )
        urllib.request.urlopen(req, timeout=8).close()
    except Exception as e:
        print(f"[unsplash] download trigger failed: {e}")


def _search_unsplash(query: str, key: str):
    """Unsplash API → {url, attribution_html} or None.

    Compliant with the Unsplash API Guidelines: hotlinks the Unsplash CDN URL
    (no re-hosting), triggers the required download event, and builds the
    required 'Photo by <name> on Unsplash' attribution with UTM-tagged links.
    """
    try:
        url = (f"https://api.unsplash.com/search/photos?query={urllib.parse.quote(query)}"
               "&per_page=1&orientation=landscape&content_filter=high")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Client-ID {key}", "User-Agent": _IMG_UA, "Accept-Version": "v1",
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        results = data.get("results", [])
        if not results:
            return None
        photo = results[0]
        img_url = photo.get("urls", {}).get("regular") or photo.get("urls", {}).get("full")
        if not img_url:
            return None
        # Required: register the download.
        _trigger_unsplash_download(photo.get("links", {}).get("download_location", ""), key)
        # Required: attribution with UTM-tagged links to photographer + Unsplash.
        user = photo.get("user", {}) or {}
        name = user.get("name") or "Unsplash"
        username = user.get("username", "")
        photog = (f"https://unsplash.com/@{username}?{_UNSPLASH_UTM}" if username
                  else f"https://unsplash.com/?{_UNSPLASH_UTM}")
        attribution_html = (
            f'Photo by <a href="{photog}" target="_blank" rel="noopener noreferrer">{name}</a> '
            f'on <a href="https://unsplash.com/?{_UNSPLASH_UTM}" target="_blank" rel="noopener noreferrer">Unsplash</a>'
        )
        return {"url": img_url, "attribution_html": attribution_html}
    except Exception as e:
        print(f"[unsplash] {e}")
    return None


def _search_pexels(query: str, key: str) -> str:
    """Pexels API — free, commercial-safe license. Needs PEXELS_API_KEY."""
    try:
        url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(query)}&per_page=1&orientation=landscape"
        req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": _IMG_UA})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
            photos = data.get("photos", [])
            if photos:
                return photos[0]["src"].get("large") or photos[0]["src"].get("original")
    except Exception as e:
        print(f"[pexels] {e}")
    return None


def _search_openverse(query: str) -> str:
    """Openverse — Creative Commons licensed images, NO API key needed. Safe to publish."""
    try:
        url = f"https://api.openverse.org/v1/images/?q={urllib.parse.quote(query)}&page_size=1&license_type=commercial&mature=false"
        req = urllib.request.Request(url, headers={"User-Agent": "LumynorBlog/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
            results = data.get("results", [])
            if results:
                return results[0].get("url") or results[0].get("thumbnail")
    except Exception as e:
        print(f"[openverse] {e}")
    return None


# ── IMAGE VETTING GATEWAY ──────────────────────────────────────────────────────
# Every candidate image URL must pass _vet_image_url() before it can be used on
# the blog. Two independent blocklists + an optional trusted-host allowlist.

# Adult / NSFW sources — never publish.
_NSFW_URL_MARKERS = (
    "xhcdn", "xhamster", "pornhub", "phncdn", "xvideos", "xnxx", "redtube",
    "youporn", "spankbang", "tnaflix", "rule34", "e621", "onlyfans",
    "/porn", "porn.", "-porn", "nsfw", "sex.com", "escort", "hentai", "xxx",
)

# Paid-stock preview hosts — these serve WATERMARKED, licensed images we can't use.
_WATERMARK_STOCK_MARKERS = (
    "ftcdn.net", "stock.adobe", "adobestock", "shutterstock", "dreamstime",
    "istockphoto", "media.istockphoto", "gettyimages", "alamy", "123rf",
    "depositphotos", "vecteezy", "stockphoto", "watermark", "bigstock",
    "canstockphoto", "agefotostock", "stocklib", "shutter_stock",
)

# Hosts we trust to serve clean, license-clear, watermark-free images.
_TRUSTED_IMAGE_HOSTS = (
    "images.unsplash.com", "unsplash.com",
    "images.pexels.com", "pexels.com",
    "upload.wikimedia.org", "commons.wikimedia.org",
    "live.staticflickr.com", "staticflickr.com",
    "placehold.co",
)

def _vet_image_url(url: str, require_trusted: bool = False) -> bool:
    """The image gateway. Reject empty, NSFW, and watermarked-stock URLs.
    When require_trusted=True, also require the host to be on the allowlist
    (used for untrusted sources like web image search)."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    u = url.lower()
    if any(m in u for m in _NSFW_URL_MARKERS):
        return False
    if any(m in u for m in _WATERMARK_STOCK_MARKERS):
        return False
    if require_trusted and not any(h in u for h in _TRUSTED_IMAGE_HOSTS):
        return False
    return True


def _search_ddg_images(query: str) -> str:
    """DuckDuckGo image search — last-resort. Strict safesearch AND the image
    must come from a trusted host, since web search otherwise returns NSFW,
    watermarked-stock, and copyrighted results unfit for a company blog."""
    DDGS = _get_ddgs()
    if DDGS is None:
        return None
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=12, safesearch="on"))
        for r in results:
            url = r.get("image")
            if _vet_image_url(url, require_trusted=True):
                return url
    except Exception as e:
        print(f"[ddg_images] {e}")
    return None


def generate_blog_images(
    image_prompts: list,
    nanobanana_key: str = "",
    nanobanana_url: str = "",
    image_source: str = "web",
    unsplash_key: str = "",
    pexels_key: str = "",
) -> list:
    """
    Source images for the blog.
    image_source: "web" (search Unsplash/Pexels/Openverse) or "ai" (Nanobanana).
    Priority: AI (if image_source='ai' + key) → Unsplash → Pexels → Openverse → DDG → placeholder.
    """
    unsplash_key = unsplash_key or os.getenv("UNSPLASH_ACCESS_KEY", "")
    pexels_key = pexels_key or os.getenv("PEXELS_API_KEY", "")
    generated = []

    for img in image_prompts:
        placement = img.get("placement", "cover")
        prompt_text = img.get("prompt", "")
        alt = img.get("alt", "")
        query = _image_query(img)
        image_url = None
        source_used = None
        attribution = ""   # required for Unsplash; empty for other sources

        # 1. AI generation (only if explicitly chosen and configured)
        if image_source == "ai" and nanobanana_key and nanobanana_url:
            try:
                api_endpoint = nanobanana_url.rstrip("/")
                payload = json.dumps({
                    "prompt": prompt_text,
                    "width": 1200 if placement == "cover" else 800,
                    "height": 630 if placement == "cover" else 450,
                    "style": "photorealistic",
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"{api_endpoint}/generate", data=payload,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {nanobanana_key}"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    rd = json.loads(resp.read().decode())
                    image_url = rd.get("url") or rd.get("image_url") or rd.get("data", {}).get("url")
                    source_used = "nanobanana"
            except Exception as e:
                print(f"[image_gen] Nanobanana error: {e}")

        # AI result must also pass the gateway.
        if image_url and not _vet_image_url(image_url):
            print(f"[image_gateway] rejected AI image: {image_url}")
            image_url, source_used = None, None

        # 2. Web image search chain — each result passes through the gateway;
        #    rejected ones fall through to the next source.
        if not image_url and unsplash_key:
            cand = _search_unsplash(query, unsplash_key)
            if cand and _vet_image_url(cand["url"]):
                image_url, source_used, attribution = cand["url"], "unsplash", cand["attribution_html"]
        if not image_url and pexels_key:
            cand = _search_pexels(query, pexels_key)
            if cand and _vet_image_url(cand):
                image_url, source_used = cand, "pexels"
        if not image_url:
            cand = _search_openverse(query)
            if cand and _vet_image_url(cand):
                image_url, source_used = cand, "openverse"
        if not image_url:
            # Web image search (DDG) is the riskiest source — only accept
            # trusted-host results. Often returns nothing, which is fine.
            cand = _search_ddg_images(query)
            if cand and _vet_image_url(cand, require_trusted=True):
                image_url, source_used = cand, "ddg"

        # 3. Placeholder fallback — a clean branded placeholder always beats
        #    an unsafe or watermarked image.
        if not image_url:
            encoded = urllib.parse.quote(query[:60])
            image_url = f"https://placehold.co/1200x630/0a0e1a/00f0ff?text={encoded}"
            source_used = "placeholder"

        generated.append({
            "placement": placement,
            "url": image_url,
            "alt": alt,
            "prompt": prompt_text,
            "query": query,
            "source": source_used,
            "attribution": attribution,
        })

    return generated


# ── STAGE 6: SEO VALIDATION ────────────────────────────────────────────────────

def validate_seo(blog: dict) -> dict:
    """Score the blog with the SAME rubric as the on-page SEO checker in the
    admin (Settings.jsx getSeoReport), so the stored score matches what the user
    sees. Starts at 100 and deducts per failed check (exact-phrase keyword,
    strict title/summary lengths, FAQ/References headings in the HTML)."""
    title = blog.get("title", "") or ""
    summary = blog.get("summary", "") or blog.get("meta_description", "") or ""
    content = blog.get("content_html", "") or ""
    primary = (blog.get("primary_keyword", "") or "").strip()

    sec_raw = blog.get("secondary_keywords", "")
    if isinstance(sec_raw, str):
        secondary = [k.strip() for k in sec_raw.split(",") if k.strip()]
    else:
        secondary = [str(k).strip() for k in (sec_raw or []) if str(k).strip()]

    clean = re.sub(r'<[^>]*>', ' ', content)
    clean = re.sub(r'\s+', ' ', clean).strip()
    words = [w for w in clean.split(' ') if w]
    word_count = len(words)

    if not primary:
        return {"score": 0, "grade": "F", "word_count": word_count,
                "issues": ["No primary keyword set"], "fixes": []}

    score = 100
    issues = []
    p = primary.lower()

    def deduct(pts, msg):
        nonlocal score
        score -= pts
        issues.append(f"-{pts}: {msg}")

    if p not in title.lower():
        deduct(15, "primary keyword missing from title")
    if not (30 <= len(title) <= 60):
        deduct(10, f"title length {len(title)} (need 30-60)")
    if p not in summary.lower():
        deduct(10, "primary keyword missing from summary")
    if not (120 <= len(summary) <= 160):
        deduct(10, f"summary length {len(summary)} (need 120-160)")
    if word_count < 600:
        deduct(10, f"word count {word_count} (<600)")

    occ = len(re.findall(re.escape(primary), content, re.I))
    density = (occ * 100 / word_count) if word_count else 0
    if not (0.6 <= density <= 2.2):
        deduct(15, f"keyword density {density:.1f}% ({occ}x, need 0.6-2.2%)")

    intro = ' '.join(words[:200]).lower()
    if p not in intro:
        deduct(10, "primary keyword not in first paragraph")

    if secondary:
        found = [s for s in secondary if s.lower() in content.lower()]
        if not found:
            deduct(15, "no secondary keywords present")
        elif len(found) < len(secondary):
            deduct(5 * (len(secondary) - len(found)), f"secondary keywords {len(found)}/{len(secondary)}")

    headings = re.findall(r'<h[23][^>]*>(.*?)</h[23]>', content, re.I | re.S)
    if not any(p in h.lower() for h in headings):
        deduct(10, "primary keyword not in any H2/H3 subheading")

    if not re.search(r'<h[23][^>]*>[^<]*(FAQ|Frequently Asked Questions)', content, re.I):
        deduct(10, "no FAQ section heading in content")
    if not re.search(r'<h[23][^>]*>[^<]*(References|Sources|Citations)', content, re.I):
        deduct(10, "no References section heading in content")
    if not re.search(r'<a\s+[^>]*href=["\']https?://', content, re.I):
        deduct(5, "no external/citation links")

    score = max(0, min(100, score))
    return {
        "score": score,
        "grade": "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D",
        "word_count": word_count,
        "issues": issues,
        "fixes": issues,
    }


# ── STAGE 6b: SEO REFINEMENT (auto-fix to hit 90+) ────────────────────────────

def refine_blog_seo(blog: dict, seo_report: dict, gemini_key: str) -> dict:
    """
    Fix the cheap, high-impact SEO issues (title + meta description) WITHOUT
    rewriting the longform body — protects word count and content quality.
    Heavier content issues are nudged via lightweight insertion, not full rewrite.
    """
    primary_kw = blog.get("primary_keyword", "")
    content = blog.get("content_html", "")

    # 1) Fix title + meta description via a short, low-risk Gemini call
    prompt = f"""You are an SEO metadata specialist. Improve ONLY the title and meta description for this blog.

PRIMARY KEYWORD: {primary_kw}
CURRENT TITLE: {blog.get('title', '')}
CURRENT META DESCRIPTION: {blog.get('meta_description', '')}
FIRST 300 CHARS OF ARTICLE: {re.sub('<[^>]+>', '', content)[:300]}

RULES:
- Title: 45-60 characters, MUST start with or contain "{primary_kw}", compelling and click-worthy
- Meta description: EXACTLY 140-158 characters, MUST contain "{primary_kw}", end with a call-to-action

Respond ONLY with JSON:
{{"title": "...", "meta_description": "..."}}"""

    try:
        result = _llm(prompt, gemini_key, json_mode=True, timeout=40, max_tokens=1024)
        fixed = _parse_json_lenient(result)
        kw_words = [w for w in primary_kw.lower().split() if len(w) > 2]
        new_title = fixed.get("title", "")
        title_has_kw = primary_kw.lower() in new_title.lower() or (kw_words and all(w in new_title.lower() for w in kw_words))
        if new_title and title_has_kw and len(new_title) <= 70:
            blog["title"] = new_title
        if fixed.get("meta_description"):
            md = fixed["meta_description"].strip()
            # Hard-enforce length cap
            if len(md) > 160:
                md = md[:157].rstrip() + "..."
            blog["meta_description"] = md
    except Exception as e:
        print(f"[refine_seo] metadata fix error: {e}")

    # 2) Programmatic safety net: trim meta description if still too long
    md = blog.get("meta_description", "")
    if len(md) > 160:
        blog["meta_description"] = md[:157].rstrip() + "..."

    # 3) Ensure primary keyword appears in first paragraph (length-safe)
    first_chunk = re.sub('<[^>]+>', '', content)[:400].lower()
    if primary_kw and primary_kw.lower() not in first_chunk and "<p>" in content:
        lead = f'<p><strong>{primary_kw.capitalize()}</strong> is changing the way modern teams build and ship.</p>\n'
        content = content.replace("<p>", lead + "<p>", 1)
        blog["content_html"] = content

    return blog


# ── MASTER PIPELINE ───────────────────────────────────────────────────────────

def run_auto_blog_pipeline(settings: dict, gemini_key: str) -> dict:
    """
    Full auto-blog pipeline:
    1. Research trending topic
    2. Keyword research
    3. Web research + outline
    4. Write longform blog
    5. Generate images
    6. SEO validate
    7. Return complete blog object ready to save
    """
    niche = settings.get("niche", "Technology")
    keywords_hint = settings.get("keywords", "")
    auto_publish = settings.get("auto_publish", False)
    author = settings.get("author", "Lumynor Team")
    category = settings.get("category", "Technology")
    nanobanana_key = settings.get("nanobanana_key", "") or os.getenv("NANOBANANA_API_KEY", "")
    nanobanana_url = settings.get("nanobanana_url", "") or os.getenv("NANOBANANA_API_URL", "")
    # Image source: "web" (search Unsplash/Pexels/Openverse) or "ai" (Nanobanana)
    image_source = settings.get("image_source", "web")
    unsplash_key = settings.get("unsplash_key", "") or os.getenv("UNSPLASH_ACCESS_KEY", "")
    pexels_key = settings.get("pexels_key", "") or os.getenv("PEXELS_API_KEY", "")

    # Resolve the LLM provider once; every stage receives this config. Prefers
    # Ollama Cloud when a key is configured, else Gemini.
    gemini_key = _build_llm_cfg(settings, gemini_key)

    log = []
    if gemini_key.get("provider") in ("ollama_cloud", "ollama"):
        log.append(f"🧠 LLM: ollama_cloud · writing={'/'.join(gemini_key.get('writing_models', []))} · fast={gemini_key.get('fast_model')}")
    else:
        log.append("🧠 LLM: gemini (gemini-2.5-flash)")

    # Stage 1: Trending topic
    log.append("🔍 Researching trending topics...")
    topic_data = research_trending_topics(niche, keywords_hint, gemini_key)
    topic = topic_data.get("topic", f"AI Trends in {niche}")
    angle = topic_data.get("angle", "Comprehensive guide")
    target_audience = topic_data.get("target_audience", "professionals")
    log.append(f"📌 Topic selected: {topic}")

    # Stage 2: Keyword research
    log.append("🔑 Running SEO keyword research...")
    keywords = do_keyword_research(topic, niche, gemini_key)
    log.append(f"🎯 Primary keyword: {keywords.get('primary_keyword')}")

    # Stage 3: Web research
    log.append("📚 Gathering research and facts...")
    research = research_topic_facts(topic, keywords, gemini_key)
    log.append(f"📊 Found {len(research.get('key_statistics', []))} statistics, {len(research.get('references', []))} references")

    # Stage 4: Write blog (retry once on parse failure)
    log.append("✍️ Writing longform blog post...")
    try:
        blog_content = write_longform_blog(topic, angle, keywords, research, target_audience, gemini_key)
    except Exception as e:
        log.append(f"⚠️ First write attempt failed ({str(e)[:60]}), retrying...")
        blog_content = write_longform_blog(topic, angle, keywords, research, target_audience, gemini_key)
    word_count = len(re.sub('<[^>]+>', '', blog_content.get("content_html", "")).split())
    log.append(f"📝 Blog written: {word_count} words")

    # Stage 5: Source images (web search by default, AI if configured)
    image_prompts = blog_content.get("image_prompts", [])
    log.append(f"🖼️ Sourcing {len(image_prompts)} images (mode: {image_source})...")
    images = generate_blog_images(
        image_prompts, nanobanana_key, nanobanana_url,
        image_source=image_source, unsplash_key=unsplash_key, pexels_key=pexels_key,
    )
    blog_content["images"] = images  # so SEO validator sees them
    sources = ", ".join(sorted(set(i.get("source", "?") for i in images)))
    log.append(f"   Image sources: {sources}")

    # Set cover image + embed section images into the article body
    cover_image = ""
    cover_attribution = ""
    section_imgs = []
    for img in images:
        if img["placement"] == "cover":
            cover_image = img["url"]
            cover_attribution = img.get("attribution", "")
        else:
            section_imgs.append(img)

    # Embed each non-cover image after successive H2 headings (graphic illustration in-body)
    content_html = blog_content.get("content_html", "")
    if section_imgs and "<h2" in content_html:
        parts = re.split(r'(<h2[^>]*>.*?</h2>)', content_html, flags=re.DOTALL)
        out, h2_seen, img_idx = [], 0, 0
        for seg in parts:
            out.append(seg)
            if seg.strip().lower().startswith("<h2") and img_idx < len(section_imgs):
                h2_seen += 1
                # Skip the very first H2 (intro) — place images deeper in the article
                if h2_seen >= 2:
                    im = section_imgs[img_idx]
                    caption = im.get("alt", "")
                    attr = im.get("attribution", "")
                    if attr:
                        caption = f'{caption} <span class="blog-credit">{attr}</span>' if caption else attr
                    out.append(
                        f'<figure class="blog-figure"><img src="{im["url"]}" alt="{im.get("alt","")}" '
                        f'loading="lazy" style="width:100%;height:auto;border-radius:0;" />'
                        f'<figcaption>{caption}</figcaption></figure>'
                    )
                    img_idx += 1
        blog_content["content_html"] = "".join(out)

    # Clean LLM markers: strip [IMAGE: ...] and convert [INTERNAL: ...] to real
    # internal links (better SEO + no placeholder text leaking to readers).
    blog_content["content_html"] = _resolve_content_placeholders(blog_content.get("content_html", ""))

    # Embed the FAQ as a real <h2> section in the article body — good for SEO and
    # so the on-page SEO checker credits it. The separate faq array is cleared in
    # the final object so the Q&A doesn't render twice on the page.
    faq_items = blog_content.get("faq", []) or []
    ch = blog_content.get("content_html", "").rstrip()
    if faq_items and not re.search(r'<h[23][^>]*>[^<]*(FAQ|Frequently Asked)', ch, re.I):
        faq_html = "<h2>Frequently Asked Questions</h2>"
        for it in faq_items:
            q = (it.get("question") or "").strip()
            a = (it.get("answer") or "").strip()
            if q and a:
                faq_html += f"<h3>{q}</h3><p>{a}</p>"
        blog_content["content_html"] = ch + "\n" + faq_html

    # Enforce a <=160 char meta description / summary (SEO sweet spot).
    summary = _clamp_summary(blog_content.get("summary") or blog_content.get("meta_description") or "")
    blog_content["summary"] = summary
    if not blog_content.get("meta_description"):
        blog_content["meta_description"] = summary

    # Stage 6: SEO validation + refinement loop (target 90+)
    seo_report = validate_seo(blog_content)
    log.append(f"📊 Initial SEO Score: {seo_report['score']}/100 (Grade {seo_report['grade']})")

    if seo_report["score"] < 90 and seo_report.get("issues"):
        log.append(f"🔧 Refining SEO ({len(seo_report['issues'])} issues to fix)...")
        blog_content = refine_blog_seo(blog_content, seo_report, gemini_key)
        # Re-inject references if refinement stripped them
        refs = research.get("references", [])
        if refs and "References" not in blog_content.get("content_html", ""):
            ref_html = "<h2>References &amp; Sources</h2><ol>"
            for r in refs[:6]:
                ref_html += f'<li><a href="{r["url"]}" target="_blank" rel="noopener noreferrer">{r["title"]}</a></li>'
            ref_html += "</ol>"
            blog_content["content_html"] = blog_content["content_html"].rstrip() + "\n" + ref_html
        word_count = len(re.sub('<[^>]+>', '', blog_content.get("content_html", "")).split())
        seo_report = validate_seo(blog_content)
        log.append(f"✅ Refined SEO Score: {seo_report['score']}/100 (Grade {seo_report['grade']})")
    else:
        log.append(f"✅ SEO Score: {seo_report['score']}/100 (Grade {seo_report['grade']})")

    # Build final blog object
    now = datetime.utcnow()
    slug = re.sub(r'[^a-z0-9]+', '-', blog_content.get("title", topic).lower()).strip('-')[:80]

    blog_object = {
        "title": blog_content.get("title", topic),
        "slug": slug,
        "category": category,
        "author": author,
        "summary": blog_content.get("summary", blog_content.get("meta_description", "")),
        "content": blog_content.get("content_html", ""),
        "published": auto_publish,
        "coverImage": cover_image,
        "coverImageAttribution": cover_attribution,
        "primaryKeyword": keywords.get("primary_keyword", ""),
        "secondaryKeywords": ", ".join(keywords.get("secondary_keywords", [])),
        "metaTitle": blog_content.get("title", topic),
        "metaDescription": blog_content.get("meta_description", ""),
        "readTime": blog_content.get("read_time", f"{max(1, word_count // 200)} min read"),
        "tags": blog_content.get("tags", [niche.lower(), category.lower()]),
        "faq": [],   # FAQ is embedded in content HTML above; keep this empty to avoid double-render
        "images": images,
        "references": blog_content.get("references", research.get("references", [])),
        "seoScore": seo_report["score"],
        "seoGrade": seo_report["grade"],
        "wordCount": word_count,
        "generatedAt": now.isoformat(),
        "date": now.strftime("%B %d, %Y"),
        "topicData": topic_data,
        "pipelineLog": log
    }

    return blog_object
