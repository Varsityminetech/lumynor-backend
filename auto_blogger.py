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
        # Best-quality model chain — all tasks use this, no fast/small model.
        # Override via OLLAMA_WRITING_MODELS env (comma-separated, best first).
        writing_models = [m.strip() for m in (os.getenv("OLLAMA_WRITING_MODELS", "") or "").split(",") if m.strip()] \
            or ["glm-4.7", "nemotron-3-super", "gpt-oss:120b", "devstral-2:123b"]
        return {
            "provider": "ollama_cloud",
            "model": writing_models[0],
            "writing_models": writing_models,
            "ollama_key": ollama_key,
            "ollama_host": settings.get("llmBaseUrl") or os.getenv("OLLAMA_HOST") or "https://ollama.com",
        }
    # Check both GEMINI_API_KEY and GOOGLE_API_KEY — Railway may set either.
    _gkey = (gemini_key or settings.get("llmApiKey", "") or
             os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""))
    return {"provider": "gemini", "gemini_key": _gkey}


def _llm(prompt: str, llm_cfg, json_mode: bool = False, timeout: int = 60, max_tokens: int = 8192) -> str:
    """Dispatch a text-generation call to the configured provider.
    For Ollama: always uses the best writing-quality model chain — no fast/small
    model is ever used. Quality over speed."""
    if isinstance(llm_cfg, str):  # back-compat: bare Gemini key
        llm_cfg = {"provider": "gemini", "gemini_key": llm_cfg}
    if llm_cfg.get("provider") in ("ollama_cloud", "ollama"):
        # Always use the quality chain for every task — time is not a constraint.
        chain = llm_cfg.get("writing_models") or [llm_cfg.get("model")]
        last_err = None
        for m in [x for x in chain if x]:
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
            # 404 included: cloud models are occasionally "not found" transiently
            # (cold/unavailable); a quick retry usually resolves it.
            if e.code in (404, 429, 500, 502, 503) and attempt < 3:
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


# ── TAVILY SEARCH + TRUSTED SOURCE TIERS ─────────────────────────────────────
# Tavily gives full page content (not just snippets) and date-aware freshness
# filtering. Falls back to DuckDuckGo when TAVILY_API_KEY is not set.

_TAVILY_URL = "https://api.tavily.com/search"

_TIER1_DOMAINS = (
    "openai.com", "anthropic.com", "deepmind.google", "blog.google",
    "ai.meta.com", "mistral.ai", "huggingface.co", "nvidia.com",
    "blogs.microsoft.com", "research.microsoft.com",
)
_TIER2_DOMAINS = (
    "techcrunch.com", "venturebeat.com", "theverge.com", "wired.com",
    "technologyreview.mit.edu", "arstechnica.com", "zdnet.com",
    "the-decoder.com",
)
_TIER3_DOMAINS = (
    "news.ycombinator.com", "reddit.com", "github.com", "producthunt.com",
)


def _tavily_search(query: str, key: str, num: int = 8,
                   depth: str = "basic", domains: list = None, days: int = 7) -> list:
    """Tavily AI-powered search — returns full page content, not just snippets."""
    payload = {
        "api_key": key, "query": query, "search_depth": depth,
        "max_results": num, "include_raw_content": False, "include_answer": False,
    }
    if domains:
        payload["include_domains"] = domains
    if days:
        payload["days"] = days
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _TAVILY_URL, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()).get("results", [])
    except Exception as e:
        print(f"[tavily] {e}")
        return []


def _source_tier(url: str) -> int:
    """Return 1 / 2 / 3 for known source tiers, 0 for unknown."""
    u = (url or "").lower()
    if any(d in u for d in _TIER1_DOMAINS):
        return 1
    if any(d in u for d in _TIER2_DOMAINS):
        return 2
    if any(d in u for d in _TIER3_DOMAINS):
        return 3
    return 0


# ── STAGE 1: TRENDING TOPIC RESEARCH ──────────────────────────────────────────

def research_trending_topics(niche: str, keywords: str, llm_cfg, recent_topics: list = None, tavily_key: str = "") -> dict:
    """Stage 1: Crawl Tier 1/2 trusted sources, cluster stories into topics,
    score each cluster on 5 factors, and return the best topic for today."""
    tavily_key = tavily_key or os.getenv("TAVILY_API_KEY", "")
    month_year = datetime.now().strftime("%B %Y")
    today = datetime.now().strftime("%B %d, %Y")
    all_articles = []

    if tavily_key:
        # Tier 1 + Tier 2 crawl via Tavily (full content, date-filtered)
        tier_queries = [
            f"latest AI news announcements {month_year}",
            f"new AI model release agent announcement {month_year}",
            f"AI automation SaaS update {month_year}",
            f"{niche} trends news {month_year}",
            f"OpenAI Anthropic Google DeepMind Microsoft latest update",
        ]
        for q in tier_queries:
            for r in _tavily_search(q, tavily_key, num=5, depth="basic",
                                    domains=list(_TIER1_DOMAINS) + list(_TIER2_DOMAINS), days=7):
                r["_tier"] = _source_tier(r.get("url", ""))
                r["snippet"] = r.get("content") or r.get("snippet", "")
                all_articles.append(r)
        # Tier 2 niche-specific search (14-day window for more context)
        for q in [f"AI SaaS developer news {month_year}", f"agentic AI business impact {month_year}"]:
            for r in _tavily_search(q, tavily_key, num=4, depth="basic",
                                    domains=list(_TIER2_DOMAINS), days=14):
                r["_tier"] = _source_tier(r.get("url", ""))
                r["snippet"] = r.get("content") or r.get("snippet", "")
                all_articles.append(r)
    else:
        # DDG fallback — general searches
        for q in [
            f"latest AI news {month_year}", f"new AI model release {month_year}",
            f"{niche} trends {month_year}", f"AI agents automation business {month_year}",
            "OpenAI Anthropic Google DeepMind latest updates",
        ]:
            for r in _search_web(q, 5):
                r["_tier"] = _source_tier(r.get("url", ""))
                all_articles.append(r)

    # Deduplicate by URL
    seen, unique = set(), []
    for r in all_articles:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u)
            unique.append(r)

    # Build annotated article list for the LLM
    articles_text = ""
    for r in unique[:28]:
        tier = r.get("_tier", 0)
        label = f"[T{tier}]" if tier else "[T?]"
        snip = (r.get("snippet") or "")[:220]
        articles_text += f"{label} {r.get('title','')} | {r.get('url','')} | {snip}\n"

    _avoid_block = ""
    if recent_topics:
        _avoid_block = "ALREADY PUBLISHED — do NOT repeat or closely overlap:\n" + "".join(
            "- " + t + "\n" for t in recent_topics[:15]
        )

    prompt = f"""You are a content strategist for Lumynor Systems — a digital product studio specialising in agentic AI, SaaS development, and AI automation.

Today: {today}  |  Niche: {niche}  |  Keywords: {keywords}

TRUSTED AI NEWS ({len(unique)} articles):
(T1 = Official source e.g. OpenAI/Anthropic/Google | T2 = Tech media e.g. TechCrunch/Wired | T? = Other)
{articles_text}

{_avoid_block}

INSTRUCTIONS:
1. Group articles about the same story into topic clusters
2. Score each cluster (out of 100) using these EXACT weights:
   - Freshness        (0-20): News from last 7 days = higher
   - Source Authority (0-20): T1 coverage = 20, T2 only = 12, T? = 5
   - Lumynor Relevance(0-25): Helps SaaS builders/agentic AI devs/digital product teams?
   - SEO Potential    (0-20): High search demand, rankable long-form guide?
   - Novelty          (0-15): Fresh angle not widely covered yet?
3. Pick the SINGLE highest-scoring topic Lumynor should write about TODAY
4. Do NOT pick gossip, viral junk, or duplicates of already-published topics

Respond ONLY with valid JSON:
{{
  "clusters": [
    {{
      "topic": "short label",
      "sources": ["url1"],
      "scores": {{"freshness": 18, "authority": 15, "relevance": 22, "seo": 17, "novelty": 12}},
      "total_score": 84
    }}
  ],
  "best_topic": {{
    "topic": "exact blog topic title",
    "angle": "unique angle Lumynor should take",
    "why_trending": "one sentence on why this matters now",
    "why_best_for_lumynor": "why this fits Lumynor's SaaS/AI audience",
    "target_audience": "who will read this",
    "search_intent": "informational | commercial | transactional",
    "cluster_sources": ["url1", "url2"],
    "total_score": 84
  }}
}}"""

    result = _llm(prompt, llm_cfg, json_mode=True)
    try:
        data = json.loads(result)
        return data.get("best_topic", data)
    except Exception:
        return {
            "topic": f"How AI Agents Are Transforming {niche} in 2026",
            "angle": "Practical guide for SaaS builders",
            "why_trending": "AI agent adoption accelerating across industries",
            "target_audience": "SaaS developers and digital product teams",
            "search_intent": "informational",
            "cluster_sources": [],
        }


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

    result = _llm(prompt, gemini_key, json_mode=True)
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


# ── STAGE 3b: DEEP RESEARCH ───────────────────────────────────────────────────

def deep_research_topic(topic: str, cluster_sources: list, llm_cfg, tavily_key: str = "") -> dict:
    """Deep-dive research on the chosen topic from multiple angles.
    Tavily advanced search gives full page content; falls back to DDG."""
    tavily_key = tavily_key or os.getenv("TAVILY_API_KEY", "")

    angles = [
        f"{topic} official announcement explanation",
        f"{topic} business impact SaaS developers teams",
        f"{topic} technical details how it works",
        f"{topic} real world examples use cases 2025 2026",
        f"{topic} limitations risks challenges concerns",
        f"{topic} vs alternatives comparison",
        f"{topic} frequently asked questions",
    ]

    all_content, all_refs = [], []

    # Pull from the original cluster sources first — they are the primary evidence
    if cluster_sources and tavily_key:
        src_domains = list({u.split("/")[2] for u in cluster_sources if "/" in u})[:4]
        if src_domains:
            for r in _tavily_search(topic, tavily_key, num=4, depth="advanced",
                                    domains=src_domains, days=30):
                body = r.get("content") or ""
                if body:
                    all_content.append(f"[PRIMARY SOURCE: {r.get('title','')}]({r.get('url','')})\n{body[:600]}")
                if r.get("url") and r.get("title"):
                    all_refs.append({"title": r["title"], "url": r["url"]})

    for q in angles:
        if tavily_key:
            for r in _tavily_search(q, tavily_key, num=3, depth="advanced", days=30):
                body = r.get("content") or r.get("raw_content") or ""
                if body:
                    all_content.append(f"[{r.get('title','')}]({r.get('url','')})\n{body[:500]}")
                if r.get("url") and r.get("title"):
                    all_refs.append({"title": r["title"], "url": r["url"]})
        else:
            for r in _search_web(q, 4):
                if r.get("snippet"):
                    all_content.append(f"[{r['title']}]({r['url']})\n{r['snippet']}")
                if r.get("url") and r.get("title"):
                    all_refs.append({"title": r["title"], "url": r["url"]})

    # Deduplicate refs
    seen, unique_refs = set(), []
    for ref in all_refs:
        if ref["url"] not in seen:
            seen.add(ref["url"])
            unique_refs.append(ref)

    research_text = "\n\n".join(all_content[:14])

    prompt = f"""You are a research analyst preparing facts for a blog post.
TOPIC: {topic}

COLLECTED RESEARCH:
{research_text[:6000]}

Extract and organise. Respond ONLY with JSON:
{{
  "official_explanation": "What this is per primary sources (1-2 paragraphs)",
  "business_impact": "How this affects SaaS teams and digital product builders",
  "technical_details": "Key technical aspects or mechanism",
  "real_examples": ["concrete example 1", "concrete example 2", "concrete example 3"],
  "limitations_risks": ["limitation or risk 1", "limitation 2"],
  "key_statistics": ["stat with number and source 1", "stat 2", "stat 3"],
  "key_facts": ["important fact 1", "fact 2", "fact 3", "fact 4", "fact 5"],
  "expert_insights": ["expert quote or insight 1", "insight 2"],
  "faqs": [
    {{"question": "question 1", "answer": "2-3 sentence answer"}},
    {{"question": "question 2", "answer": "2-3 sentence answer"}},
    {{"question": "question 3", "answer": "2-3 sentence answer"}},
    {{"question": "question 4", "answer": "2-3 sentence answer"}},
    {{"question": "question 5", "answer": "2-3 sentence answer"}}
  ],
  "claims_to_avoid": ["unverified claim 1", "hype statement to skip"],
  "blog_outline": [
    {{"section": "Introduction", "key_points": ["hook", "problem", "what reader learns"]}},
    {{"section": "H2 section title 1", "key_points": ["point1", "point2", "stat or example"]}},
    {{"section": "H2 section title 2", "key_points": ["point1", "point2"]}},
    {{"section": "H2 section title 3 with primary keyword", "key_points": ["point1", "point2"]}},
    {{"section": "H2 section title 4", "key_points": ["point1", "lumynor angle"]}},
    {{"section": "Frequently Asked Questions", "key_points": []}},
    {{"section": "Conclusion", "key_points": ["summary", "CTA"]}}
  ],
  "references": [{{"title": "source title", "url": "https://..."}}]
}}"""

    result = _llm(prompt, llm_cfg, json_mode=True, timeout=60, max_tokens=4096)
    try:
        deep = json.loads(result)
        if not deep.get("references"):
            deep["references"] = unique_refs[:8]
        # Normalise so old code that reads `expert_insights` still works
        if not deep.get("expert_insights"):
            deep["expert_insights"] = deep.get("expert_quotes", [])
        return deep
    except Exception as e:
        print(f"[deep_research] parse error: {e}")
        return {
            "official_explanation": "", "business_impact": "", "technical_details": "",
            "real_examples": [], "limitations_risks": [],
            "key_statistics": [], "key_facts": [], "expert_insights": [],
            "faqs": [], "claims_to_avoid": [],
            "blog_outline": [
                {"section": "Introduction", "key_points": []},
                {"section": f"Understanding {topic}", "key_points": []},
                {"section": f"Impact of {topic}", "key_points": []},
                {"section": "Conclusion", "key_points": []},
            ],
            "references": unique_refs[:8],
        }


# ── STAGE 3c: RESEARCH BRIEF ──────────────────────────────────────────────────

def generate_research_brief(topic: str, angle: str, keywords: dict,
                             deep_research: dict, niche: str, llm_cfg) -> dict:
    """Generate the formal research brief — source of truth before the blog is written.
    Locks in the Lumynor perspective, banned phrases, internal links, and exact outline."""
    primary_kw = keywords.get("primary_keyword", topic)
    secondary_kws = keywords.get("secondary_keywords", [])

    facts_block = "".join("- " + f + "\n" for f in deep_research.get("key_facts", [])[:6])
    stats_block = "".join("- " + s + "\n" for s in deep_research.get("key_statistics", [])[:4])
    examples_block = "".join("- " + e + "\n" for e in deep_research.get("real_examples", [])[:4])
    risks_block = "".join("- " + r + "\n" for r in deep_research.get("limitations_risks", [])[:4])
    avoid_block = "".join("- " + c + "\n" for c in deep_research.get("claims_to_avoid", [])[:5])
    outline_block = "".join(
        f"  {s['section']}: {', '.join(s.get('key_points', []))}\n"
        for s in deep_research.get("blog_outline", [])
    )

    prompt = f"""You are a senior content strategist at Lumynor Systems — a digital product studio that builds agentic AI and SaaS platforms.

Create a RESEARCH BRIEF (source of truth) for this blog post.

TOPIC: {topic}
ANGLE: {angle}
PRIMARY KEYWORD: {primary_kw}
SECONDARY KEYWORDS: {', '.join(secondary_kws[:5])}
NICHE: {niche}

VERIFIED RESEARCH DATA:
Official Explanation: {(deep_research.get('official_explanation') or '')[:400]}
Business Impact: {(deep_research.get('business_impact') or '')[:350]}
Technical Details: {(deep_research.get('technical_details') or '')[:300]}
Key Statistics:
{stats_block}
Key Facts:
{facts_block}
Real Examples:
{examples_block}
Limitations/Risks:
{risks_block}
Claims to Avoid:
{avoid_block}
Suggested Outline:
{outline_block}

Lumynor's products: Agent Forge (SaaS builder), District 21 (event ticketing), Hotel OS, School OS.
Lumynor's audience: SaaS founders, developers, digital product teams, CTOs.
Lumynor's voice: Technically confident, no hype, original analysis, speaks to builders.

Respond ONLY with valid JSON:
{{
  "topic": "{topic}",
  "primary_keyword": "{primary_kw}",
  "secondary_keywords": {json.dumps(secondary_kws[:5])},
  "search_intent": "informational | commercial | transactional",
  "target_audience": "specific description",
  "core_angle": "the unique angle Lumynor takes",
  "lumynor_perspective": "2-3 sentences — Lumynor's original take, tied to Agent Forge or the company expertise",
  "business_relevance": "why this matters specifically for SaaS builders and digital product teams",
  "main_facts": ["verified fact 1", "verified fact 2", "verified fact 3", "verified fact 4", "verified fact 5"],
  "key_statistics": ["stat with source 1", "stat 2", "stat 3"],
  "claims_to_avoid": ["unverified claim 1", "hype phrase 2"],
  "banned_phrases": ["In today's fast-paced digital world", "game-changer", "revolutionize", "leverage", "delve into", "In conclusion", "Firstly", "It is worth noting"],
  "suggested_outline": [
    {{"section": "Introduction (hook — never start with clichés)", "key_points": ["hook line", "problem statement", "what reader learns"]}},
    {{"section": "H2: [specific meaningful title]", "key_points": ["point 1", "stat or example", "insight"]}},
    {{"section": "H2: [specific meaningful title]", "key_points": ["point 1", "point 2", "real example"]}},
    {{"section": "H2: [title containing primary keyword]", "key_points": ["point 1", "lumynor angle"]}},
    {{"section": "H2: Frequently Asked Questions", "key_points": []}},
    {{"section": "Conclusion + CTA", "key_points": ["key takeaway", "link to Agent Forge or /contact"]}}
  ],
  "internal_links": [
    {{"anchor": "Agent Forge", "url": "/products/agent-forge", "context": "when discussing SaaS builders or scaffolding"}},
    {{"anchor": "talk to our team", "url": "/contact", "context": "CTA or recommendations"}},
    {{"anchor": "more AI insights", "url": "/blog", "context": "when referencing other articles"}}
  ],
  "faqs": [
    {{"question": "specific FAQ 1", "answer": "clear 2-3 sentence answer"}},
    {{"question": "specific FAQ 2", "answer": "clear 2-3 sentence answer"}},
    {{"question": "specific FAQ 3", "answer": "clear 2-3 sentence answer"}},
    {{"question": "specific FAQ 4", "answer": "clear 2-3 sentence answer"}},
    {{"question": "specific FAQ 5", "answer": "clear 2-3 sentence answer"}}
  ],
  "external_references": [{{"title": "ref title", "url": "https://..."}}]
}}"""

    result = _llm(prompt, llm_cfg, json_mode=True, timeout=60, max_tokens=4096)
    try:
        brief = json.loads(result)
        if not brief.get("external_references") or len(brief.get("external_references", [])) < 2:
            brief["external_references"] = deep_research.get("references", [])[:8]
        return brief
    except Exception as e:
        print(f"[research_brief] parse error: {e}")
        return {
            "topic": topic, "primary_keyword": primary_kw,
            "secondary_keywords": secondary_kws,
            "core_angle": angle,
            "lumynor_perspective": "Lumynor builds agentic AI platforms that help digital product teams ship faster.",
            "main_facts": deep_research.get("key_facts", []),
            "claims_to_avoid": deep_research.get("claims_to_avoid", []),
            "banned_phrases": ["In today's fast-paced digital world", "game-changer", "revolutionize", "leverage", "delve"],
            "suggested_outline": deep_research.get("blog_outline", []),
            "internal_links": [
                {"anchor": "Agent Forge", "url": "/products/agent-forge"},
                {"anchor": "talk to our team", "url": "/contact"},
            ],
            "faqs": deep_research.get("faqs", []),
            "external_references": deep_research.get("references", [])[:8],
        }


# ── STAGE 4: LONGFORM HUMAN-LIKE BLOG WRITING ─────────────────────────────────

def write_longform_blog(topic: str, angle: str, keywords: dict, research: dict,
                         target_audience: str, gemini_key: str,
                         quality_hints: str = None, research_brief: dict = None) -> dict:
    """Write a full longform human-like blog post driven by the research brief."""

    primary_kw = (research_brief or keywords).get("primary_keyword") or keywords.get("primary_keyword", topic)
    secondary_kws_list = keywords.get("secondary_keywords", [])
    secondary_kws = ", ".join(secondary_kws_list)
    lsi_kws = ", ".join(keywords.get("lsi_keywords", []))

    # ── Build context blocks ──────────────────────────────────────────────────
    if research_brief:
        core_angle = research_brief.get("core_angle") or angle
        lumynor_pov = research_brief.get("lumynor_perspective", "")
        business_rel = research_brief.get("business_relevance", "")
        banned = research_brief.get("banned_phrases", [])
        avoid_claims = research_brief.get("claims_to_avoid", [])

        facts_block = "".join("• " + f + "\n" for f in research_brief.get("main_facts", []))
        stats_block = "".join("• " + s + "\n" for s in research_brief.get("key_statistics", []))

        outline_text = ""
        for s in research_brief.get("suggested_outline", []):
            outline_text += f"\n**{s['section']}**\n"
            for pt in s.get("key_points", []):
                outline_text += f"  - {pt}\n"

        faq_items = research_brief.get("faqs", [])
        faq_block = "\n".join(f"Q: {f['question']}\nA: {f['answer']}" for f in faq_items[:5])

        int_links = research_brief.get("internal_links", [])
        links_block = "\n".join(
            f'  <a href="{l["url"]}">{l["anchor"]}</a> — {l.get("context","")}'
            for l in int_links
        )

        refs = research_brief.get("external_references") or research.get("references", [])
        expert_block = "".join("• " + e + "\n" for e in research.get("expert_insights", []))
        examples_block = "".join("• " + e + "\n" for e in research.get("real_examples", []))

        banned_str = ", ".join(f'"{p}"' for p in (banned or [
            "In today's fast-paced digital world", "game-changer", "revolutionize",
            "leverage", "delve", "in conclusion", "firstly",
        ]))
        avoid_str = "\n".join(f"  ✗ {c}" for c in avoid_claims[:5])
    else:
        core_angle = angle
        lumynor_pov = ""
        business_rel = ""
        banned_str = '"In today\'s world", "game-changer", "revolutionize", "leverage", "delve", "in conclusion", "firstly"'
        avoid_str = ""
        facts_block = "".join("• " + f + "\n" for f in research.get("key_facts", []))
        stats_block = "".join("• " + s + "\n" for s in research.get("key_statistics", []))
        expert_block = "".join("• " + e + "\n" for e in research.get("expert_insights", []))
        examples_block = ""
        outline_text = ""
        for section in research.get("blog_outline", []):
            outline_text += f"\n**{section['section']}**\n"
            for pt in section.get("key_points", []):
                outline_text += f"  - {pt}\n"
        faq_items = keywords.get("people_also_ask", [])
        faq_block = "\n".join(f"- {q}" for q in faq_items[:5])
        int_links = []
        links_block = '<a href="/products/agent-forge">Agent Forge</a>, <a href="/contact">talk to our team</a>, <a href="/blog">more insights</a>'
        refs = research.get("references", [])

    refs_text = "\n".join(f"- [{r['title']}]({r['url']})" for r in refs[:6])

    prompt = f"""You are a senior writer at Lumynor Systems — a digital product studio building agentic AI and SaaS platforms.
Write a COMPREHENSIVE LONGFORM blog post (2000-3000 words) with real depth, original analysis, and a strong Lumynor perspective.
This must read like it was written by a human expert who builds SaaS products — NOT like generic AI output.

═══ RESEARCH BRIEF ═══════════════════════════════════════════════════════════
TOPIC: {topic}
ANGLE: {core_angle}
TARGET AUDIENCE: {target_audience}
PRIMARY KEYWORD: {primary_kw}
SECONDARY KEYWORDS: {secondary_kws}
LSI KEYWORDS: {lsi_kws}
{("LUMYNOR PERSPECTIVE: " + lumynor_pov) if lumynor_pov else ""}
{("BUSINESS RELEVANCE: " + business_rel) if business_rel else ""}

VERIFIED FACTS:
{facts_block or "(use research data below)"}
KEY STATISTICS:
{stats_block}
EXPERT INSIGHTS:
{expert_block}
REAL EXAMPLES:
{examples_block}

EXACT OUTLINE TO FOLLOW:
{outline_text}

FAQ TO ANSWER (use these exact questions):
{faq_block}

INTERNAL LINKS TO USE (embed naturally in context):
{links_block}

EXTERNAL REFERENCES TO CITE:
{refs_text}

{("CLAIMS TO AVOID — do NOT include these:" + ("" if not avoid_str else (" " + avoid_str))) if avoid_str else ""}

═══ WRITING RULES ════════════════════════════════════════════════════════════
1. BANNED PHRASES — never write: {banned_str}
2. NEVER open with a cliché. Start with a specific scenario, surprising stat, or bold claim
3. Address the reader as "you" throughout
4. Mix short punchy sentences with longer analytical ones — vary rhythm constantly
5. Use contractions (it's, you'll, don't, they're) — sound human
6. Add hedged original opinions ("In practice...", "Worth noting is...", "The overlooked part is...")
7. Use transitions between sections ("Here's the thing...", "What changes everything here is...")
8. Every H2 section must have real depth — 200-350 words, specific data, at least one example
9. Include <div class="callout-tip"> boxes for key takeaways and tips
10. Write about what builders and developers ACTUALLY need to know — not surface-level content

═══ SEO RULES ════════════════════════════════════════════════════════════════
1. Primary keyword "{primary_kw}" MUST appear in: title, first 100 words, ≥2 H2 headings, meta description
2. Use secondary keywords naturally — never force them
3. Every H2 title must be specific and actionable, not generic
4. Use only clean HTML — no [IMAGE: ...] or [INTERNAL: ...] placeholder markers

OUTPUT — Respond ONLY with this exact JSON:
{{
  "title": "SEO title with primary keyword (45-60 chars)",
  "meta_description": "140-155 char description with {primary_kw} + CTA",
  "summary": "Engaging 2-3 sentence excerpt for blog listing",
  "read_time": "X min read",
  "content_html": "FULL HTML (h2/h3/p/ul/ol/li/strong/em/blockquote/div.callout-tip/div.callout-warning)",
  "faq": [
    {{"question": "FAQ 1", "answer": "2-3 sentence answer"}},
    {{"question": "FAQ 2", "answer": "2-3 sentence answer"}},
    {{"question": "FAQ 3", "answer": "2-3 sentence answer"}},
    {{"question": "FAQ 4", "answer": "2-3 sentence answer"}},
    {{"question": "FAQ 5", "answer": "2-3 sentence answer"}}
  ],
  "image_prompts": [
    {{"placement": "cover", "prompt": "photorealistic cover image concept", "alt": "SEO alt text"}},
    {{"placement": "section_1", "prompt": "illustration for section 1", "alt": "alt text"}},
    {{"placement": "section_2", "prompt": "illustration for section 2", "alt": "alt text"}}
  ],
  "references": [{{"title": "...", "url": "https://..."}}],
  "primary_keyword": "{primary_kw}",
  "secondary_keywords": "{secondary_kws}",
  "tags": ["tag1", "tag2", "tag3", "tag4"]
}}"""

    if quality_hints:
        prompt += f"\n\nPREVIOUS DRAFT ISSUES — FIX ALL OF THESE IN THIS REWRITE:\n{quality_hints}\n"

    result = _llm(prompt, gemini_key, json_mode=True, timeout=280, max_tokens=32768)
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

def run_auto_blog_pipeline(settings: dict, gemini_key: str, recent_topics: list = None) -> dict:
    """
    Full auto-blog pipeline (guide-aligned):
    1. Crawl Tier 1/2 trusted sources, cluster + score topics → pick best
    2. SEO keyword research
    3a. Deep research (multi-angle Tavily/DDG dive on chosen topic)
    3b. Generate formal research brief (Lumynor perspective, banned phrases, outline)
    4. Write longform blog from brief (quality retry loop)
    5. Source images (once; reused on rewrite)
    6. SEO validate + refine + quality rewrite if needed
    """
    niche = settings.get("niche", "Technology")
    keywords_hint = (settings.get("keywords", "") or "")[:300]
    auto_publish = settings.get("auto_publish", False)
    author = settings.get("author", "Lumynor Team")
    category = settings.get("category", "Technology")
    nanobanana_key = settings.get("nanobanana_key", "") or os.getenv("NANOBANANA_API_KEY", "")
    nanobanana_url = settings.get("nanobanana_url", "") or os.getenv("NANOBANANA_API_URL", "")
    image_source = settings.get("image_source", "web")
    unsplash_key = settings.get("unsplash_key", "") or os.getenv("UNSPLASH_ACCESS_KEY", "")
    pexels_key = settings.get("pexels_key", "") or os.getenv("PEXELS_API_KEY", "")
    tavily_key = settings.get("tavily_key", "") or os.getenv("TAVILY_API_KEY", "")

    llm_cfg = _build_llm_cfg(settings, gemini_key)
    # Use llm_cfg everywhere; keep name gemini_key as alias for internal helpers
    gemini_key = llm_cfg

    log = []
    if llm_cfg.get("provider") in ("ollama_cloud", "ollama"):
        log.append(f"🧠 LLM: ollama_cloud · models={'/'.join(llm_cfg.get('writing_models', []))} (best-quality chain, no fast model)")
    else:
        log.append("🧠 LLM: gemini (gemini-2.5-flash)")
    log.append(f"🔎 Research: {'Tavily (advanced)' if tavily_key else 'DuckDuckGo (fallback)'}")

    # Stage 1: Crawl trusted sources, cluster stories, score + pick best topic
    log.append("🔍 Crawling trusted AI sources and scoring topics...")
    topic_data = research_trending_topics(
        niche, keywords_hint, llm_cfg,
        recent_topics=recent_topics, tavily_key=tavily_key,
    )
    topic = topic_data.get("topic", f"AI Trends in {niche}")
    angle = topic_data.get("angle", "Comprehensive guide")
    target_audience = topic_data.get("target_audience", "SaaS developers and digital product teams")
    cluster_sources = topic_data.get("cluster_sources", [])
    log.append(f"📌 Topic selected: {topic} (score: {topic_data.get('total_score','?')})")

    # Stage 2: SEO keyword research
    log.append("🔑 Running SEO keyword research...")
    keywords = do_keyword_research(topic, niche, llm_cfg)
    log.append(f"🎯 Primary keyword: {keywords.get('primary_keyword')}")

    # Stage 3a: Deep research — multi-angle dive with Tavily or DDG
    log.append("🔬 Running deep research on topic...")
    research = deep_research_topic(topic, cluster_sources, llm_cfg, tavily_key=tavily_key)
    log.append(
        f"📊 Deep research: {len(research.get('key_facts', []))} facts, "
        f"{len(research.get('key_statistics', []))} stats, "
        f"{len(research.get('references', []))} references"
    )

    # Stage 3b: Generate research brief — source of truth before writing
    log.append("📋 Generating research brief...")
    research_brief = generate_research_brief(topic, angle, keywords, research, niche, llm_cfg)
    log.append(f"📋 Brief ready: {len(research_brief.get('suggested_outline', []))} sections, "
               f"{len(research_brief.get('faqs', []))} FAQs")

    # Stage 4: Write blog from brief — quality retry loop, best model only
    log.append("✍️ Writing longform blog from research brief...")
    _min_words = int(os.getenv("BLOG_MIN_WORD_COUNT", "1200"))
    blog_content = None
    for _attempt in range(1, 4):
        try:
            _draft = write_longform_blog(
                topic, angle, keywords, research, target_audience, llm_cfg,
                research_brief=research_brief,
            )
            _wc = len(re.sub('<[^>]+>', '', _draft.get("content_html", "")).split())
            blog_content = _draft
            if _wc >= _min_words or _attempt == 3:
                log.append(f"📝 Blog written: {_wc} words" + (f" (attempt {_attempt})" if _attempt > 1 else ""))
                break
            log.append(f"⚠️ Attempt {_attempt}: only {_wc} words (min {_min_words}) — rewriting...")
        except Exception as e:
            if _attempt == 3:
                raise
            log.append(f"⚠️ Write attempt {_attempt} failed ({str(e)[:60]}), retrying...")
    word_count = len(re.sub('<[^>]+>', '', blog_content.get("content_html", "")).split())

    # Stage 5: Source images — done ONCE and reused on any quality rewrite below.
    image_prompts = blog_content.get("image_prompts", [])
    log.append(f"🖼️ Sourcing {len(image_prompts)} images (mode: {image_source})...")
    images = generate_blog_images(
        image_prompts, nanobanana_key, nanobanana_url,
        image_source=image_source, unsplash_key=unsplash_key, pexels_key=pexels_key,
    )
    blog_content["images"] = images
    sources = ", ".join(sorted(set(i.get("source", "?") for i in images)))
    log.append(f"   Image sources: {sources}")

    cover_image = ""
    cover_attribution = ""
    section_imgs = []
    for img in images:
        if img["placement"] == "cover":
            cover_image = img["url"]
            cover_attribution = img.get("attribution", "")
        else:
            section_imgs.append(img)

    def _finalize_draft(draft):
        """Embed images, inject FAQ, resolve placeholders, clamp meta, SEO-validate + refine.
        Returns (processed_draft, seo_report). Captures section_imgs/research/gemini_key."""
        # Embed section images after successive H2 headings
        ch = draft.get("content_html", "")
        if section_imgs and "<h2" in ch:
            parts = re.split(r'(<h2[^>]*>.*?</h2>)', ch, flags=re.DOTALL)
            out, h2_seen, img_idx = [], 0, 0
            for seg in parts:
                out.append(seg)
                if seg.strip().lower().startswith("<h2") and img_idx < len(section_imgs):
                    h2_seen += 1
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
            draft["content_html"] = "".join(out)

        draft["content_html"] = _resolve_content_placeholders(draft.get("content_html", ""))

        faq_items = draft.get("faq", []) or []
        ch2 = draft.get("content_html", "").rstrip()
        if faq_items and not re.search(r'<h[23][^>]*>[^<]*(FAQ|Frequently Asked)', ch2, re.I):
            faq_html = "<h2>Frequently Asked Questions</h2>"
            for it in faq_items:
                q = (it.get("question") or "").strip()
                a = (it.get("answer") or "").strip()
                if q and a:
                    faq_html += f"<h3>{q}</h3><p>{a}</p>"
            draft["content_html"] = ch2 + "\n" + faq_html

        summary = _clamp_summary(draft.get("summary") or draft.get("meta_description") or "")
        draft["summary"] = summary
        if not draft.get("meta_description"):
            draft["meta_description"] = summary

        # SEO validate + refine (target 90+)
        seo = validate_seo(draft)
        if seo["score"] < 90 and seo.get("issues"):
            draft = refine_blog_seo(draft, seo, gemini_key)
            refs = research.get("references", [])
            if refs and "References" not in draft.get("content_html", ""):
                ref_html = "<h2>References &amp; Sources</h2><ol>"
                for r in refs[:6]:
                    ref_html += f'<li><a href="{r["url"]}" target="_blank" rel="noopener noreferrer">{r["title"]}</a></li>'
                ref_html += "</ol>"
                draft["content_html"] = draft["content_html"].rstrip() + "\n" + ref_html
            seo = validate_seo(draft)
        return draft, seo

    # First finalization pass
    blog_content, seo_report = _finalize_draft(blog_content)
    log.append(f"📊 SEO Score: {seo_report['score']}/100 (Grade {seo_report['grade']})")

    # Quality rewrite: if SEO is still weak after refinement, rewrite the full article
    # body once — passing the specific issues back into the LLM so it can address them.
    # Images are already sourced above and are reused in _finalize_draft.
    _min_quality = int(os.getenv("BLOG_MIN_QUALITY_SCORE", "75"))
    if seo_report["score"] < _min_quality and seo_report.get("issues"):
        hints = " | ".join(i.lstrip("-0123456789: ") for i in seo_report["issues"])
        log.append(f"🔄 Quality below {_min_quality} — rewriting with {len(seo_report['issues'])} SEO fixes targeted...")
        try:
            _rewrite = write_longform_blog(
                topic, angle, keywords, research, target_audience, gemini_key,
                quality_hints=hints, research_brief=research_brief,
            )
            _rewrite["image_prompts"] = blog_content.get("image_prompts", [])
            _rewrite, _seo_rewrite = _finalize_draft(_rewrite)
            if _seo_rewrite["score"] >= seo_report["score"]:
                blog_content, seo_report = _rewrite, _seo_rewrite
                log.append(f"✅ Rewrite improved SEO: {seo_report['score']}/100 (Grade {seo_report['grade']})")
            else:
                log.append(f"⚠️ Rewrite SEO {_seo_rewrite['score']} not better — keeping original {seo_report['score']}")
        except Exception as e:
            log.append(f"⚠️ Quality rewrite failed ({str(e)[:60]}), keeping original")
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
        "researchBrief": {
            "core_angle": research_brief.get("core_angle", ""),
            "lumynor_perspective": research_brief.get("lumynor_perspective", ""),
            "sections": len(research_brief.get("suggested_outline", [])),
        },
        "pipelineLog": log
    }

    return blog_object
