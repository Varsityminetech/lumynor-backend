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
            or ["cogito-2.1:671b", "qwen3-coder:480b", "nemotron-3-super", "gpt-oss:120b", "devstral-2:123b"]
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
                msg = result.get("message", {})
                content = (msg.get("content") or "").strip()
                # Some models (e.g. minimax-m2, glm-4.6) are "thinking" models that
                # put their reasoning in 'thinking' and the final answer in 'content'.
                # When content is empty (token budget consumed by thinking), fall back
                # to the thinking text so the pipeline gets something usable.
                if not content:
                    content = (msg.get("thinking") or "").strip()
                return content
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
    """Bing web search (primary) with DuckDuckGo fallback. Returns list of {title, url, snippet}."""
    # Primary: Bing — broader index, fresher news/blog content, better for research
    try:
        import requests as _req, re as _re
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        r = _req.get(
            "https://www.bing.com/search",
            params={"q": query, "count": num},
            headers=headers, timeout=8,
        )
        if r.ok:
            results = []
            titles   = _re.findall(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text)
            snippets = _re.findall(r'<p class="b_algoSlug"[^>]*>(.*?)</p>', r.text)
            for i, (url, title) in enumerate(titles[:num]):
                if url.startswith("http") and "bing.com" not in url and "microsoft.com" not in url:
                    snip = _re.sub(r'<[^>]+>', '', snippets[i] if i < len(snippets) else "")
                    results.append({
                        "title":   _re.sub(r'<[^>]+>', '', title),
                        "url":     url,
                        "snippet": snip,
                    })
            if results:
                return results
    except Exception as e:
        print(f"[search] Bing error: {e}")

    # Fallback: DuckDuckGo
    DDGS = _get_ddgs()
    if DDGS is None:
        return []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=num))
        return [{"title": r.get("title", ""), "url": r.get("href", "") or r.get("url", ""), "snippet": r.get("body", "")} for r in results]
    except Exception as e:
        print(f"[search] DDG fallback error: {e}")
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

    result = _llm(prompt, llm_cfg, json_mode=True, timeout=120)
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

    result = _llm(prompt, gemini_key, json_mode=True, timeout=90)
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
                url  = r.get("url", "")
                tier = _source_tier(url)
                if tier == 0:
                    print(f"[research] Skipping untrusted source: {url[:80]}")
                    continue
                body = r.get("content") or r.get("raw_content") or ""
                if body:
                    all_content.append(f"[{r.get('title','')}]({url})\n{body[:500]}")
                if url and r.get("title"):
                    all_refs.append({"title": r["title"], "url": url})
        else:
            for r in _search_web(q, 4):
                url  = r.get("url", "")
                if _source_tier(url) == 0:
                    continue
                if r.get("snippet"):
                    all_content.append(f"[{r['title']}]({url})\n{r['snippet']}")
                if url and r.get("title"):
                    all_refs.append({"title": r["title"], "url": url})

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

    result = _llm(prompt, llm_cfg, json_mode=True, timeout=120, max_tokens=4096)
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

    result = _llm(prompt, llm_cfg, json_mode=True, timeout=120, max_tokens=4096)
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


def write_roundup_blog(niche: str, month_year: str, llm_cfg, tavily_key: str = "") -> dict:
    """Write a 'Top 10 AI News Headlines with Analysis' roundup post.
    Pulls real headlines from Tavily/DDG, then asks the LLM to write 200-250 words
    of practitioner analysis per story. Returns the same dict shape as write_longform_blog."""
    raw = []
    if tavily_key:
        for q in [
            f"biggest AI news announcements {month_year}",
            f"new AI model release product launch {month_year}",
            f"AI research breakthrough startup funding {month_year}",
            f"{niche} news highlights {month_year}",
        ]:
            for r in _tavily_search(q, tavily_key, num=5, depth="basic", days=7):
                if r.get("title") and r.get("url"):
                    raw.append(r)
    else:
        for q in [f"top AI news {month_year}", f"latest AI model release {month_year}",
                  f"{niche} breakthroughs this week"]:
            for r in _search_web(q, 5):
                if r.get("title") and r.get("url"):
                    raw.append(r)

    # Deduplicate by URL; then require each story to be confirmed by 3+ independent sources.
    # We cluster by normalised title similarity (first 6 significant words) so that the same
    # story reported on multiple outlets counts as 3-source confirmed.
    def _story_key(title: str) -> str:
        words = re.sub(r'[^a-z0-9 ]', ' ', title.lower()).split()
        significant = [w for w in words if len(w) > 3][:6]
        return " ".join(significant)

    # Group all raw results by story key (counts independent source URLs per story)
    story_groups: dict = {}
    for h in raw:
        u = h.get("url", "")
        t = h.get("title", "")
        if not u or not t:
            continue
        key = _story_key(t)
        if key not in story_groups:
            story_groups[key] = {"headline": h, "urls": set(), "snippets": []}
        story_groups[key]["urls"].add(u)
        snippet = (h.get("content") or h.get("snippet") or "")[:200]
        if snippet and snippet not in story_groups[key]["snippets"]:
            story_groups[key]["snippets"].append(snippet)

    # Keep only stories confirmed by 3+ independent sources
    confirmed = [v for v in story_groups.values() if len(v["urls"]) >= 3]
    print(f"[roundup] {len(story_groups)} unique stories found; {len(confirmed)} confirmed by 3+ sources")

    # Fallback: if fewer than 6 confirmed stories, relax to 2+ sources
    if len(confirmed) < 6:
        confirmed = [v for v in story_groups.values() if len(v["urls"]) >= 2]
        print(f"[roundup] Relaxed to 2+ source confirmation: {len(confirmed)} stories")

    if not confirmed:
        raise RuntimeError("No headlines found with sufficient source confirmation — check Tavily key or network access")

    top = [v["headline"] for v in confirmed[:12]]

    refs = [{"title": h.get("title", ""), "url": h.get("url", "")} for h in top if h.get("url")]
    refs_json = json.dumps(refs[:10])
    slug_date = month_year.lower().replace(" ", "-")
    # Primary keyword must be an exact substring of the title (no colon separating them)
    primary_kw = f"Top 10 AI News {month_year}"

    headlines_block = ""
    for i, h in enumerate(top, 1):
        # Show all confirmed source URLs for each story so the LLM can cite them
        grp       = story_groups[_story_key(h.get("title", ""))]
        src_urls  = " | ".join(list(grp["urls"])[:3])
        snippet   = " ".join(grp["snippets"])[:350]
        headlines_block += (
            f"{i}. TITLE: {h.get('title', '')}\n"
            f"   CONFIRMED SOURCES ({len(grp['urls'])}): {src_urls}\n"
            f"   INFO:  {snippet}\n\n"
        )

    prompt = f"""You are a senior AI analyst at Lumynor Systems — a digital product studio building agentic AI and SaaS products.

Write a comprehensive "Top 10 AI News {month_year}" roundup blog post.
Every story MUST be confirmed by multiple sources — cite the sources provided, do not fabricate URLs.
Cover each story with real technical depth — what happened, technical implications for SaaS and AI product builders, and the Lumynor perspective.

═══ NEWS STORIES TO COVER (all confirmed by 2-3+ independent sources) ═════════
{headlines_block}
════════════════════════════════════════════════════════════════════════════════

STRUCTURE:
- Intro paragraph (100-150 words): frame why this batch matters
- For each story: <h2>N. Story Headline</h2> → 200-250 words of original analysis (synthesize from the sources, do NOT copy verbatim) → cite with inline <a href="SOURCE_URL" target="_blank" rel="noopener noreferrer">source name</a> → end with one sentence: "Builder impact: [concrete implication]."
- <h2>Key Takeaways for AI Builders</h2> → 200-word conclusion with Lumynor angle and a call to action linking to <a href="/contact">talk to the Lumynor team</a>
- 3-5 FAQ pairs about the news

RULES:
- 2500-3500 total words — every section needs real original analysis, not summaries or copy-paste
- Each story analysis must synthesize from the confirmed sources — no verbatim copying
- HTML only inside content_html — NO markdown
- BANNED: "game-changer", "revolutionize", "leverage", "delve", "In today's world", "in conclusion", "it's worth noting", "What this means for builders" (repeated phrase — vary the phrasing each time)
- Tone: opinionated, technical, practitioner — not generic journalism

Return ONLY valid JSON (no markdown wrapper):
{{
  "title": "Top 10 AI News {month_year}: What Builders Actually Need to Know",
  "slug": "top-10-ai-news-{slug_date}",
  "summary": "A practitioner breakdown of the 10 biggest AI news stories in {month_year} — what they mean for SaaS and agentic product builders.",
  "meta_description": "Top 10 AI News {month_year}: 10 biggest stories analyzed for SaaS and AI product builders by Lumynor Systems.",
  "content_html": "<p>intro...</p><h2>1. First Headline</h2><p>analysis...</p>...",
  "primary_keyword": "{primary_kw}",
  "secondary_keywords": ["AI headlines {month_year}", "artificial intelligence news this week", "AI model releases {month_year}", "latest AI updates", "agentic AI news"],
  "tags": ["AI News", "Weekly Roundup", "Machine Learning", "Agentic AI", "SaaS"],
  "faq": [
    {{"question": "What were the biggest AI news stories in {month_year}?", "answer": "..."}},
    {{"question": "How will these AI developments affect SaaS products?", "answer": "..."}},
    {{"question": "What should developers watch in AI right now?", "answer": "..."}}
  ],
  "image_prompts": [
    {{"placement": "cover", "prompt": "futuristic AI news room with glowing holographic headlines and neural networks", "alt": "Top 10 AI news headlines {month_year}"}},
    {{"placement": "section_1", "prompt": "abstract AI data streams and connected neural network nodes", "alt": "artificial intelligence developments {month_year}"}}
  ],
  "references": {refs_json}
}}"""

    raw_result = _llm(prompt, llm_cfg, json_mode=True, timeout=280, max_tokens=16384)
    blog = _parse_json_lenient(raw_result)
    return blog


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
    """
    100-point SEO audit across 10 weighted categories (matches the guide checklist).

    Category weights:
      Topic & Intent        10   Research Quality      15   Keyword Usage     10
      Title/Meta/Slug       10   Content Structure     10   Helpful Content   15
      Human-Like Writing    10   Internal/Ext Links    10   FAQ/Schema/Image   5
      Readability            5
    """
    title   = (blog.get("title")          or "").strip()
    summary = (blog.get("meta_description") or blog.get("summary") or "").strip()
    # Accept both "content_html" (pipeline drafts) and "content" (stored blogs)
    content = (blog.get("content_html") or blog.get("content") or "").strip()
    # Accept both "primary_keyword" (pipeline) and "primaryKeyword" (stored/API)
    primary = (blog.get("primary_keyword") or blog.get("primaryKeyword") or "").strip()
    cover   = (blog.get("coverImage")      or blog.get("cover_image") or "").strip()
    references = blog.get("references") or []
    research_brief = blog.get("research_brief") or {}

    # Accept both "secondary_keywords" (pipeline) and "secondaryKeywords" (stored/API)
    sec_raw = blog.get("secondary_keywords") or blog.get("secondaryKeywords") or ""
    secondary = ([k.strip() for k in sec_raw.split(",") if k.strip()]
                 if isinstance(sec_raw, str)
                 else [str(k).strip() for k in (sec_raw or []) if str(k).strip()])

    clean = re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', content)).strip()
    words = clean.split()
    word_count = len(words)
    clean_lower = clean.lower()

    if not primary:
        return {"score": 0, "grade": "F", "status": "Hard Fail — No primary keyword",
                "word_count": word_count, "issues": ["No primary keyword set"],
                "passed": [], "fixes": [], "hard_fail_reasons": ["No primary keyword set"],
                "category_scores": {}}

    p = primary.lower()

    # Per-category point buckets (start at max, deduct inside each)
    cat = {
        "topic_intent":     10,
        "research_quality": 15,
        "keyword_usage":    10,
        "title_meta":       10,
        "structure":        10,
        "helpful_content":  15,
        "human_writing":    10,
        "links":            10,
        "faq_schema_image":  5,
        "readability":       5,
    }
    issues, passed, fixes, hard_fails = [], [], [], []

    def lose(category, pts, issue, fix=""):
        cat[category] = max(0, cat[category] - pts)
        issues.append(issue)
        if fix:
            fixes.append(fix)

    def ok(msg):
        passed.append(msg)

    # ── 1. Topic & Intent (10 pts) ────────────────────────────────────────────
    NICHE_KW = {"ai", "saas", "automation", "startup", "agent", "llm", "software",
                "business", "digital", "tech", "product", "developer", "model"}
    title_lower = title.lower()
    if NICHE_KW & (set(title_lower.split()) | set(p.split())):
        ok("Topic relevant to Lumynor's niche (AI / SaaS / automation)")
    else:
        lose("topic_intent", 4,
             "Topic may not connect to Lumynor's audience (AI/SaaS/startups/digital products)",
             "Ensure topic covers AI, SaaS, automation, business, or digital product themes")

    if word_count >= 1200:
        ok(f"Word count {word_count} — sufficient for a complete answer")
    else:
        lose("topic_intent", 4,
             f"Word count {word_count} is too low for a complete, helpful article (target 1200+)",
             "Expand article to at least 1200 words to properly cover the topic")

    if research_brief:
        ok("Research brief present — unique Lumynor angle baked in")
    else:
        lose("topic_intent", 2, "No research brief found — unique angle may be missing")

    # ── 2. Research Quality (15 pts) ─────────────────────────────────────────
    ext_links = re.findall(r'href=["\']https?://[^"\']+["\']', content, re.I)
    TRUSTED = (
        # AI / model labs
        "openai.com", "anthropic.com", "deepmind.google", "nvidia.com",
        "huggingface.co", "mistral.ai", "cohere.com", "stability.ai",
        # Big tech
        "google.com", "microsoft.com", "meta.com", "amazon.com", "aws.amazon.com",
        "research.microsoft.com", "blogs.microsoft.com", "ai.meta.com",
        "blog.google", "cloud.google.com",
        # Tech journalism
        "techcrunch.com", "venturebeat.com", "theverge.com", "wired.com",
        "arstechnica.com", "zdnet.com", "the-decoder.com", "semafor.com",
        # Business / finance press
        "reuters.com", "bloomberg.com", "forbes.com", "wsj.com",
        "ft.com", "businessinsider.com", "cnbc.com",
        # Academic / research
        "technologyreview.mit.edu", "arxiv.org", "mit.edu", "stanford.edu",
        "nature.com", "science.org", "acm.org", "ieee.org",
        # Developer / SaaS docs
        "stripe.com", "github.com", "docs.github.com", "cloudflare.com",
        "vercel.com", "supabase.com", "planetscale.com", "neon.tech",
        "aws.amazon.com", "cloud.google.com", "azure.microsoft.com",
        "kubernetes.io", "docker.com", "postgresql.org",
    )
    # Combine inline links + reference URLs for trusted-source check
    ref_urls = {r.get("url", "") for r in (references or []) if r.get("url")}
    all_ext_urls = set(ext_links) | ref_urls
    trusted_ext = [l for l in all_ext_urls if any(d in l for d in TRUSTED)]
    # Adding references to the metadata must never hurt the src_count — use max()
    ref_count = len(references) if references else 0
    src_count = max(ref_count, len(ext_links))

    if src_count >= 3:
        ok(f"Source count: {src_count} references/links")
    elif src_count >= 1:
        lose("research_quality", 5,
             f"Only {src_count} source(s) — minimum 3 required",
             "Add at least 3 reliable external sources (official announcements, trusted publications)")
    else:
        lose("research_quality", 10,
             "No sources found — all claims are unsupported",
             "Add references from official sources and trusted tech publications")
        hard_fails.append("Sources missing")

    if trusted_ext:
        ok(f"Trusted external source(s) linked: {len(trusted_ext)}")
    else:
        lose("research_quality", 5,
             "No links to trusted/primary sources (official sites, major publications)",
             "Add at least 1 link to an official or reputed source (e.g. OpenAI blog, TechCrunch)")

    # Unsupported stats heuristic: numbers with % or $ but no external sources
    stat_hits = re.findall(r'\b\d+(?:\.\d+)?%|\$[\d\.]+[BMKbmk]?\b', clean)
    if stat_hits and not all_ext_urls:
        lose("research_quality", 5,
             f"{len(stat_hits)} statistic(s) used but no external sources linked — potential unsupported claims",
             "Back all statistics with a linked source or remove them")
    else:
        ok("Statistics appear source-backed")

    # ── 3. Keyword Usage (10 pts) ─────────────────────────────────────────────
    # Use clean text (HTML stripped) so keyword hits inside href/alt/class attrs
    # don't inflate the count — only visible readable text is graded.
    occ = len(re.findall(re.escape(p), clean, re.I))
    density = (occ * 100 / word_count) if word_count else 0

    if p in title_lower:
        ok("Primary keyword in title")
    else:
        lose("keyword_usage", 3,
             "Primary keyword missing from title",
             f"Add '{primary}' to the title naturally")

    intro_text = " ".join(words[:150]).lower()
    if p in intro_text:
        ok("Primary keyword in intro (first 150 words)")
    else:
        lose("keyword_usage", 3,
             "Primary keyword not in first 150 words",
             f"Mention '{primary}' naturally within the opening paragraph")

    h_texts = " ".join(re.sub(r'<[^>]+>', '', h)
                       for h in re.findall(r'<h[23][^>]*>.*?</h[23]>', content, re.I | re.S)).lower()
    if p in h_texts:
        ok("Primary keyword in at least one H2/H3 heading")
    else:
        lose("keyword_usage", 2,
             "Primary keyword not found in any H2/H3 heading",
             "Include the primary keyword in at least one section heading")

    if 0.5 <= density <= 2.5:
        ok(f"Keyword density {density:.1f}% — natural ({occ}x)")
    elif density > 2.5:
        lose("keyword_usage", 2,
             f"Keyword density {density:.1f}% ({occ}x) — possible keyword stuffing",
             "Reduce repetition; use synonyms and related terms instead")
        hard_fails.append("Keyword stuffing detected")
    else:
        lose("keyword_usage", 2,
             f"Keyword density {density:.1f}% ({occ}x) — keyword underused",
             "Increase natural usage of the primary keyword throughout the article")

    # ── 4. Title / Meta / Slug (10 pts) ──────────────────────────────────────
    tlen = len(title)
    if 45 <= tlen <= 65:
        ok(f"Title length {tlen} chars — good")
    elif 30 <= tlen < 45:
        lose("title_meta", 2,
             f"Title length {tlen} — slightly short (target 50-60 chars)",
             "Expand the title to be more descriptive (50-60 characters)")
    elif tlen > 65:
        lose("title_meta", 3,
             f"Title length {tlen} — too long, will be truncated in search results (target 50-60)",
             "Shorten title to under 60 characters")
    else:
        lose("title_meta", 5, "Title too short or missing", "Write a descriptive 50-60 character title")

    mlen = len(summary)
    if 130 <= mlen <= 162:
        ok(f"Meta description length {mlen} chars — good")
    else:
        lose("title_meta", 3,
             f"Meta description length {mlen} (target 140-160 chars)",
             "Write a meta description of exactly 140-160 characters with a call-to-action")

    if p in summary.lower():
        ok("Primary keyword in meta description")
    else:
        lose("title_meta", 2,
             "Primary keyword missing from meta description",
             f"Add '{primary}' to the meta description")

    # No clickbait heuristic: title matches topic
    CLICKBAIT = ["will change everything", "you won't believe", "shocking", "secret revealed",
                 "this one trick", "the truth about", "forever changed"]
    if any(c in title_lower for c in CLICKBAIT):
        lose("title_meta", 2,
             "Title appears clickbait — may not match actual content",
             "Rewrite title to accurately reflect the article's content")
    else:
        ok("Title appears accurate and non-clickbait")

    # ── 5. Content Structure (10 pts) ─────────────────────────────────────────
    h1s = re.findall(r'<h1[^>]*>.*?</h1>', content, re.I | re.S)
    h2s = re.findall(r'<h2[^>]*>.*?</h2>', content, re.I | re.S)
    h3s = re.findall(r'<h3[^>]*>.*?</h3>', content, re.I | re.S)

    if len(h1s) <= 1:
        ok(f"H1 count: {len(h1s)} — correct (title is the H1)")
    else:
        lose("structure", 2,
             f"Multiple H1 tags ({len(h1s)}) — only one H1 allowed per page",
             "Remove extra H1 tags; the blog post title serves as the H1")

    if len(h2s) >= 5:
        ok(f"H2 structure: {len(h2s)} sections — strong logical flow")
    elif len(h2s) >= 3:
        ok(f"H2 structure: {len(h2s)} sections")
    elif len(h2s) >= 2:
        lose("structure", 2,
             f"Only {len(h2s)} H2 sections — needs more structure",
             "Add H2 headings for: What/Why/Business Impact/Use Cases/Risks/Conclusion")
    else:
        lose("structure", 4,
             f"Only {len(h2s)} H2 section(s) — article lacks structure",
             "Structure with at least 5 H2 sections following the recommended outline")

    if h3s:
        ok(f"H3 sub-sections present: {len(h3s)}")
    else:
        lose("structure", 2,
             "No H3 sub-sections — missing depth in structure",
             "Add H3 sub-headings under major H2 sections for detail")

    if re.search(r'<h[23][^>]*>[^<]*(conclusion|summary|takeaway|key points|what this means|wrap)',
                 content, re.I):
        ok("Conclusion / key takeaways section present")
    else:
        lose("structure", 2,
             "No conclusion or key takeaways section found",
             "Add a 'Conclusion' or 'Key Takeaways' H2 section at the end")

    # ── 6. Helpful Content (15 pts) ───────────────────────────────────────────
    BANNED = [
        "in today's fast-paced", "in today's fast paced", "game-changer", "game changer",
        "revolutionize", "unlock the power", "seamlessly", "cutting-edge technology",
        "leveraging ai", "leverage ai", "leveraging the power", "delve into", "delve in",
        "at the end of the day", "it's worth noting", "it is worth noting",
        "as an ai language model", "as an ai,", "this groundbreaking", "this revolutionary",
        "the future is here", "the landscape is evolving", "the world is changing",
        "in conclusion,", "in summary,", "needless to say",
    ]
    found_banned = [b for b in BANNED if b in clean_lower]
    if not found_banned:
        ok("No banned/generic phrases detected")
    elif len(found_banned) <= 2:
        lose("helpful_content", 5,
             f"Banned phrases found: {found_banned}",
             f"Remove or rewrite these generic phrases: {', '.join(found_banned)}")
    else:
        lose("helpful_content", 10,
             f"Multiple banned phrases ({len(found_banned)}): {found_banned[:5]}",
             "Rewrite sections with specific, concrete language — avoid marketing fluff")
        hard_fails.append("Article contains multiple banned generic phrases")

    if word_count >= 1500:
        ok(f"Content comprehensive — {word_count} words")
    elif word_count >= 1000:
        lose("helpful_content", 3,
             f"Article at {word_count} words — target 1500+ for comprehensive long-form",
             "Expand each section with detail, examples, and business context")
    else:
        lose("helpful_content", 8,
             f"Article at {word_count} words — far too short for long-form SEO value",
             "Rewrite with full sections: intro/what/why/impact/use cases/risks/FAQ/conclusion")
        hard_fails.append("Article too short")

    example_count = len(re.findall(
        r'\b(for example|for instance|such as|consider |case study|in practice|'
        r'real-world|imagine |like when|take [\w]+ as)',
        clean_lower))
    if example_count >= 3:
        ok(f"Practical examples present ({example_count} instances)")
    elif example_count >= 1:
        lose("helpful_content", 3,
             f"Only {example_count} example(s) — needs 3+ concrete examples",
             "Add real-world business examples and use cases in each major section")
    else:
        lose("helpful_content", 5,
             "No practical examples found — article is too abstract",
             "Add specific business examples, product names, or use-case scenarios")
        hard_fails.append("No original analysis or practical examples")

    # ── 7. Human-Like Writing (10 pts) ────────────────────────────────────────
    ROBOTIC = ["furthermore,", "moreover,", "additionally,", "nevertheless,",
               "consequently,", "notwithstanding,", "it is important to note",
               "it should be noted", "one must consider", "it is worth mentioning",
               "on the other hand,", "in other words,"]
    robotic_hits = sum(1 for t in ROBOTIC if t in clean_lower)
    if robotic_hits == 0:
        ok("No robotic transitions detected")
    elif robotic_hits <= 2:
        lose("human_writing", 3,
             f"Robotic transitions detected ({robotic_hits}x) — reads like a template",
             "Replace 'Furthermore/Moreover/Additionally' with natural connectors or new sentences")
    else:
        lose("human_writing", 6,
             f"Heavy use of robotic transitions ({robotic_hits}x) — clearly AI-generated tone",
             "Rewrite transitions; use short punchy sentences instead of academic connectors")

    GENERIC_OPENER = ["in today", "the world of", "in the ever-", "in recent years,",
                      "with the rise of", "technology is changing", "we live in a world",
                      "artificial intelligence is transform", "the rapid advancement",
                      "in the age of"]
    intro_lower = " ".join(words[:200]).lower()
    generic_hits = [g for g in GENERIC_OPENER if g in intro_lower]
    if not generic_hits:
        ok("Intro starts with a specific insight or event — not generic")
    else:
        lose("human_writing", 4,
             f"Generic intro opener detected: '{generic_hits[0]}'",
             "Rewrite intro to start with a specific fact, event, question, or contrarian insight")

    # ── 8. Internal & External Links (10 pts) ────────────────────────────────
    int_links = re.findall(
        r'href=["\'](?:https?://(?:www\.)?lumynor\.com|/)[^"\']*["\']', content, re.I)
    if len(int_links) >= 3:
        ok(f"Internal links: {len(int_links)} — good")
    elif len(int_links) >= 1:
        lose("links", 5,
             f"Only {len(int_links)} internal link(s) — target 3-5",
             "Add links to relevant Lumynor pages (services, products, related blog posts)")
    else:
        lose("links", 7,
             "No internal links — missing key on-page SEO and engagement signal",
             "Add 3-5 internal links to Lumynor service/product pages and related blog posts")

    bad_anchors = re.findall(r'<a\s[^>]*>\s*(?:click here|read more|here|this link)\s*</a>',
                             content, re.I)
    if bad_anchors:
        lose("links", 3,
             f"Bad anchor text: '{bad_anchors[0]}' — use descriptive text",
             "Replace generic anchors ('click here', 'here') with descriptive link text")
    elif ext_links:
        ok(f"External links: {len(ext_links)} with descriptive anchors")

    # ── 9. FAQ / Schema / Image SEO (5 pts) ──────────────────────────────────
    if re.search(r'<h[23][^>]*>[^<]*(FAQ|Frequently Asked|Common Questions)', content, re.I):
        ok("FAQ section present — eligible for rich results")
    else:
        lose("faq_schema_image", 2,
             "No FAQ section — missing schema opportunity and search snippet eligibility",
             "Add a 'Frequently Asked Questions' H2 with 4-6 relevant questions and concise answers")

    if re.search(r'application/ld\+json', content, re.I):
        ok("Structured data (JSON-LD) present — eligible for rich results")
    else:
        lose("faq_schema_image", 1,
             "No structured data (JSON-LD) found — missing schema.org markup",
             "Add Article and FAQPage JSON-LD schema for Google rich results eligibility")

    if re.search(r'<h[23][^>]*>[^<]*(References|Sources|Citations)', content, re.I):
        ok("References / Sources section present")
    else:
        lose("faq_schema_image", 1,
             "No References section",
             "Add a 'References & Sources' H2 section at the bottom of the article")

    has_real_image = bool(cover and "placehold.co" not in cover)
    if has_real_image:
        ok("Cover image present")
        imgs_no_alt = [img for img in re.findall(r'<img[^>]+>', content, re.I)
                       if not re.search(r'alt=["\'][^"\']{10,}["\']', img, re.I)]
        if not imgs_no_alt:
            ok("Image alt text is descriptive")
        else:
            lose("faq_schema_image", 1,
                 f"{len(imgs_no_alt)} image(s) missing descriptive alt text",
                 "Add descriptive alt text (10+ chars) to all images — describe what the image shows")
    else:
        lose("faq_schema_image", 2,
             "No cover image — missing visual SEO signal",
             "Add a relevant cover image from Pexels/Unsplash")

    # ── 10. Readability (5 pts) ───────────────────────────────────────────────
    paras = re.findall(r'<p[^>]*>(.*?)</p>', content, re.I | re.S)
    if paras:
        para_lens = [len(re.sub(r'<[^>]+>', '', p).split()) for p in paras]
        long_paras = [l for l in para_lens if l > 100]
        if not long_paras:
            ok(f"Paragraph length good (avg {sum(para_lens)//len(para_lens)} words)")
        else:
            lose("readability", 2,
                 f"{len(long_paras)} paragraph(s) over 100 words — hurts readability",
                 "Break long paragraphs into 2-4 sentence chunks")

    if secondary:
        found_sec = [s for s in secondary if s.lower() in clean_lower]
        ratio = len(found_sec) / len(secondary)
        if ratio >= 0.6:
            ok(f"Secondary keywords: {len(found_sec)}/{len(secondary)} present")
        else:
            lose("readability", 3,
                 f"Secondary keywords sparse: {len(found_sec)}/{len(secondary)} used",
                 f"Naturally include more of: {', '.join(secondary[:4])}")
    else:
        ok("Secondary keywords not set — skipped")

    # ── Hard fail overrides ────────────────────────────────────────────────────
    total = max(0, min(100, sum(cat.values())))

    if hard_fails:
        status = "Hard Fail — Do Not Publish"
    elif total >= 90:
        status = "Publish"
    elif total >= 80:
        status = "Publish after minor edits"
    elif total >= 70:
        status = "Needs revision"
    else:
        status = "Reject / Regenerate"

    grade = ("A" if total >= 90 else "B" if total >= 80 else
             "C" if total >= 70 else "D" if total >= 60 else "F")

    return {
        "score": total,
        "grade": grade,
        "status": status,
        "word_count": word_count,
        "category_scores": cat,
        "passed": passed,
        "issues": issues,
        "fixes": fixes,
        "recommended_fixes": fixes,
        "hard_fail_reasons": hard_fails,
    }


# ── CREDIBILITY AUDIT + SURGICAL REWRITE ─────────────────────────────────────
#
# Separate from SEO — credibility audits factual accuracy, source trustworthiness,
# claim confidence language, originality, and repetition. Threshold: 90/100.
# revise_blog_credibility() loops up to max_loops times, exiting early at 90+.

_CREDIBLE_DOMAINS = (
    # Official lab / company blogs
    "openai.com", "anthropic.com", "deepmind.google", "blog.google", "ai.meta.com",
    "mistral.ai", "huggingface.co", "nvidia.com", "blogs.microsoft.com",
    "research.microsoft.com", "aws.amazon.com", "cloud.google.com", "azure.microsoft.com",
    # Tier-1 tech journalism
    "techcrunch.com", "venturebeat.com", "theverge.com", "wired.com",
    "technologyreview.mit.edu", "arstechnica.com", "zdnet.com", "the-decoder.com",
    "reuters.com", "bloomberg.com", "wsj.com", "ft.com", "apnews.com",
    # Academic / research
    "arxiv.org", "nature.com", "science.org", "ieee.org", "acm.org",
    "mit.edu", "stanford.edu",
    # Developer-community sources
    "github.com", "stackoverflow.com", "cloudflare.com", "vercel.com",
)

_HEDGE_PHRASES = (
    "according to", "reports suggest", "as reported by", "sources indicate",
    "based on", "per ", "citing ", "the company said", "announced that",
)

_DEFINITIVE_RED_FLAGS = (
    "100% accurate", "guaranteed", "definitely will", "certainly will",
    "it is a fact that", "undeniably", "without question", "it is proven",
    "studies show that ai will", "experts agree that ai will",
)


def _sentence_fingerprints(text: str) -> list:
    """Return normalised sentence strings (lowercased, punctuation stripped) for dedup."""
    raw = re.split(r'(?<=[.!?])\s+', re.sub(r'<[^>]+>', ' ', text))
    return [re.sub(r'[^a-z0-9 ]', '', s.lower()).strip() for s in raw if len(s.strip()) > 40]


def _find_repeated_sentences(content: str) -> list:
    """Return list of sentences (cleaned) that appear more than once in the article."""
    fps = _sentence_fingerprints(content)
    seen, dupes = {}, []
    for fp in fps:
        if fp in seen:
            if fp not in dupes:
                dupes.append(fp)
        else:
            seen[fp] = 1
    return dupes


def validate_credibility(blog: dict) -> dict:
    """
    100-point credibility audit across 5 categories:
      Source trustworthiness  25   Claim accuracy / hedging  25
      Originality             20   Multi-source confirmation  20
      Editorial standards     10

    Returns the same shape as validate_seo (score, grade, status, issues, fixes,
    hard_fail_reasons) so it can be fed into revise_blog_credibility().
    """
    _ck     = "content_html" if "content_html" in blog else "content"
    content = (blog.get(_ck) or "").strip()
    clean   = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', content)).strip()
    clean_l = clean.lower()

    cat = {
        "source_trust":      25,
        "claim_accuracy":    25,
        "originality":       20,
        "multi_source":      20,
        "editorial":         10,
    }
    issues, passed, fixes, hard_fails = [], [], [], []

    def lose(category, pts, issue, fix=""):
        cat[category] = max(0, cat[category] - pts)
        issues.append(issue)
        if fix:
            fixes.append(fix)

    def ok(msg):
        passed.append(msg)

    # ── 1. Source trustworthiness (25 pts) ────────────────────────────────────
    ext_links = re.findall(r'href=["\']https?://([^/"\']+)', content, re.I)
    credible_links = [d for d in ext_links if any(c in d for c in _CREDIBLE_DOMAINS)]
    non_credible   = [d for d in ext_links if not any(c in d for c in _CREDIBLE_DOMAINS)
                      and d and "lumynor" not in d]

    if len(credible_links) >= 3:
        ok(f"Credible sources linked: {len(credible_links)}")
    elif len(credible_links) >= 1:
        lose("source_trust", 10, f"Only {len(credible_links)} credible source(s) — need 3+",
             "Add citations from official announcements, major tech journals, or peer-reviewed papers")
    else:
        lose("source_trust", 20, "No links to credible sources — all claims are unsupported",
             "Every claim must link to an official or tier-1 journalistic source")
        hard_fails.append("No credible sources linked")

    if non_credible:
        lose("source_trust", min(5, len(non_credible) * 2),
             f"Non-credible domain(s) linked: {', '.join(set(non_credible[:4]))}",
             "Remove links to unknown/non-tier sources; replace with official or Tier-1 press links")
    else:
        ok("No non-credible domain links detected")

    # ── 2. Claim accuracy / hedging (25 pts) ──────────────────────────────────
    # Stats with no inline source attribution nearby
    stat_hits = re.findall(r'\b\d+(?:\.\d+)?%|\$[\d\.]+[BMKbmk]?\b|\b\d{4}\s+(?:billion|million|thousand)\b', clean)
    stat_count = len(stat_hits)
    if stat_count == 0 or credible_links:
        ok("Statistics appear source-backed or no floating stats")
    elif stat_count <= 3:
        lose("claim_accuracy", 8, f"{stat_count} statistic(s) with no inline source citation",
             "Link every statistic to its primary source inline in the text")
    else:
        lose("claim_accuracy", 15, f"{stat_count} statistics with no credible inline citations — high fabrication risk",
             "Back each stat with a source hyperlink; remove any that cannot be verified")
        hard_fails.append("Multiple unsupported statistics — high fabrication risk")

    red_flag_hits = [rf for rf in _DEFINITIVE_RED_FLAGS if rf in clean_l]
    if not red_flag_hits:
        ok("No overconfident / unverified definitive claims detected")
    elif len(red_flag_hits) <= 2:
        lose("claim_accuracy", 8, f"Overconfident claims: {red_flag_hits}",
             "Replace with hedged language: 'according to X', 'research suggests', 'X reported that'")
    else:
        lose("claim_accuracy", 15, f"Multiple definitive unverifiable claims ({len(red_flag_hits)}): {red_flag_hits[:3]}",
             "Rewrite all flagged claims with proper attribution or hedged language")
        hard_fails.append("Multiple overconfident definitive claims without citations")

    hedge_hits = sum(1 for hp in _HEDGE_PHRASES if hp in clean_l)
    if hedge_hits >= 3:
        ok(f"Appropriate hedging language used ({hedge_hits} instances)")
    elif hedge_hits >= 1:
        lose("claim_accuracy", 5, "Insufficient hedging language — article reads as asserting unconfirmed facts",
             "Add attribution phrases: 'according to', 'as reported by', 'X stated that'")
    else:
        lose("claim_accuracy", 10, "No hedging language at all — every claim asserted as absolute fact",
             "Use 'according to [source]', 'reports indicate', 'X announced that' throughout")

    # ── 3. Originality / no repetition (20 pts) ───────────────────────────────
    repeated = _find_repeated_sentences(content)
    if not repeated:
        ok("No repeated sentences detected — original synthesis throughout")
    elif len(repeated) <= 2:
        lose("originality", 8, f"{len(repeated)} sentence(s) repeated verbatim in the article",
             "Rewrite the duplicate sentences — each idea should appear exactly once")
    else:
        lose("originality", 15, f"{len(repeated)} repeated sentences — article reads as templated / copy-pasted",
             "Audit the full article for copy-paste paragraphs; every sentence must appear only once")
        hard_fails.append("Excessive sentence repetition — likely verbatim copy from sources")

    # Repeated structural phrases (e.g. roundup ending formulas)
    structural_repeats = []
    for phrase in ("what this means for builders", "builder impact:", "key takeaway:", "in summary,"):
        count = clean_l.count(phrase)
        if count > 3:
            structural_repeats.append(f"'{phrase}' x{count}")
    if structural_repeats:
        lose("originality", 5, f"Repeated structural phrases: {structural_repeats}",
             "Vary closing sentences per section — avoid templated endings")
    else:
        ok("No overused structural phrases detected")

    # ── 4. Multi-source confirmation (20 pts) ─────────────────────────────────
    unique_credible_domains = set(
        d for d in credible_links if any(c in d for c in _CREDIBLE_DOMAINS)
    )
    if len(unique_credible_domains) >= 3:
        ok(f"Claims backed by {len(unique_credible_domains)} distinct credible domains")
    elif len(unique_credible_domains) == 2:
        lose("multi_source", 8, "Only 2 distinct credible domains cited — need 3+ for multi-source confidence",
             "Add citations from a third independent credible source to key claims")
    elif len(unique_credible_domains) == 1:
        lose("multi_source", 15, "Single-source article — all credible links from one domain",
             "Cross-reference key claims with at least 2 more independent credible sources")
    else:
        lose("multi_source", 20, "Zero distinct credible domains — no multi-source verification",
             "Research and cite 3+ independent credible sources for primary claims")
        hard_fails.append("No multi-source verification — single-point-of-failure reporting")

    # ── 5. Editorial standards (10 pts) ───────────────────────────────────────
    # Check for fabrication signals: quotes with no attributed speaker, "experts say" without naming them
    # Use clean (HTML-stripped) text so HTML attribute values don't count as quotes
    anon_expert = len(re.findall(r'"[^"]{20,}"(?!\s*(?:—|–|,)\s*\w)', clean))
    if anon_expert > 3:
        lose("editorial", 5, f"{anon_expert} unattributed quote(s) in visible text — unclear who said this",
             "Every quotation must name the speaker and source")
    else:
        ok("Quotes appear attributed or are not present")

    vague_expert = len(re.findall(r'\bexperts?\s+(?:say|agree|believe|suggest|warn|predict)\b', clean_l))
    if vague_expert > 2:
        lose("editorial", 5, f"'experts say/agree/believe' used {vague_expert}x without naming them",
             "Name the specific expert, institution, or link to the actual statement")
    else:
        ok("No anonymous 'experts say' generalizations detected")

    # ── Final scoring ──────────────────────────────────────────────────────────
    total = max(0, min(100, sum(cat.values())))
    if hard_fails:
        status = "Hard Fail — Do Not Publish"
    elif total >= 90:
        status = "Credible — Publish"
    elif total >= 75:
        status = "Minor credibility issues — review before publishing"
    else:
        status = "Credibility below standard — revise required"

    grade = ("A" if total >= 90 else "B" if total >= 80 else
             "C" if total >= 70 else "D" if total >= 60 else "F")

    return {
        "score": total,
        "grade": grade,
        "status": status,
        "category_scores": cat,
        "passed": passed,
        "issues": issues,
        "fixes": fixes,
        "hard_fail_reasons": hard_fails,
        "repeated_sentences": repeated,
    }


def check_plagiarism(content: str, tavily_key: str = "") -> dict:
    """
    Sample distinctive sentences from the visible blog text and search for them
    verbatim on the web. Any sentence found on a non-Lumynor external source is
    flagged as potential plagiarism.

    Returns:
      score        — 0-100 (100 = fully original, 0 = everything found externally)
      checked      — number of sentences sampled
      flagged      — list of {sentence, found_at, source_title}
      status       — "Original" / "Likely plagiarised" / "Review needed"
    """
    # Work on clean text only — no HTML
    clean = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', content)).strip()

    # Split into sentences on . ! ? boundaries
    raw_sentences = re.split(r'(?<=[.!?])\s+', clean)

    # Keep sentences that are substantive and specific (40-200 chars)
    GENERIC_OPENERS = (
        "in conclusion", "in summary", "in this article", "it is important",
        "one of the most", "when it comes to", "as mentioned", "as we have seen",
        "the following", "there are many", "it is worth noting",
    )
    candidates = [
        s.strip() for s in raw_sentences
        if 40 < len(s.strip()) < 200
        and not any(s.strip().lower().startswith(g) for g in GENERIC_OPENERS)
        and sum(1 for c in s if c.isupper()) < len(s) * 0.4  # skip all-caps headings
    ]

    if not candidates:
        return {"score": 100, "checked": 0, "flagged": [], "flagged_count": 0,
                "status": "No checkable sentences found"}

    # Sample evenly: up to 8 sentences spread across the article
    step     = max(1, len(candidates) // 8)
    samples  = candidates[::step][:8]
    tavily_key = tavily_key or os.getenv("TAVILY_API_KEY", "")
    flagged  = []

    for sentence in samples:
        # Use first 80 chars of sentence as the quoted search query
        query = f'"{sentence[:80]}"'
        results = []
        if tavily_key:
            results = _tavily_search(query, tavily_key, num=3, depth="basic", days=3650)
        else:
            results = _search_web(query, 3)

        for r in results:
            url   = (r.get("url") or "").lower()
            title = r.get("title") or ""
            if not url:
                continue
            # Skip our own site and empty hits
            if "lumynor" in url:
                continue
            # Check if the result content actually contains the sentence verbatim
            snippet = (r.get("content") or r.get("snippet") or "").lower()
            # Require at least 60% of the sentence words to appear in the snippet
            # (exact match is unreliable due to encoding; word-overlap is more robust)
            sentence_words = set(re.sub(r'[^a-z0-9 ]', '', sentence.lower()).split())
            snippet_words  = set(re.sub(r'[^a-z0-9 ]', '', snippet).split())
            significant    = {w for w in sentence_words if len(w) > 4}
            if significant and len(significant & snippet_words) / len(significant) >= 0.6:
                flagged.append({
                    "sentence":     sentence[:120],
                    "found_at":     r.get("url", ""),
                    "source_title": title[:80],
                    "overlap_pct":  round(len(significant & snippet_words) / len(significant) * 100),
                })
                break  # One confirmed hit per sentence is enough

    n_clean  = len(samples) - len(flagged)
    score    = round(n_clean / len(samples) * 100) if samples else 100

    if score >= 90:
        status = "Original"
    elif score >= 70:
        status = "Review needed — some passages match external sources"
    else:
        status = "Likely plagiarised — multiple passages found on external sites"

    return {
        "score":        score,
        "checked":      len(samples),
        "flagged_count": len(flagged),
        "flagged":      flagged,
        "status":       status,
    }


def revise_blog_credibility(blog: dict, cred_report: dict, llm_cfg, max_loops: int = 3) -> dict:
    """
    Surgical credibility rewrite — loop up to max_loops times:
      1. If hard fail → return immediately (unfixable without fresh research)
      2. Fix repeated sentences (LLM rewrites all dupes in one pass)
      3. Fix unsupported stats — add hedge language
      4. Fix unattributed quotes / 'experts say' → attributed or removed
      5. Fix overconfident claims → hedged language
      6. Re-audit; exit early if score >= 90
    Returns {"revised_blog": dict, "new_credibility_score": int, "notes": list}
    """
    _ck    = "content_html" if "content_html" in blog else "content"
    notes  = []
    current = dict(blog)
    score_progression = [cred_report.get("score", 0)]

    for loop in range(1, max_loops + 1):
        score_before = cred_report.get("score", 0)
        notes.append(f"\n── Credibility Loop {loop}/{max_loops} (score: {score_before}/100) ──")

        hard_fails = cred_report.get("hard_fail_reasons", [])
        if hard_fails:
            notes.append(f"  ✗ Hard fail — stopping: {hard_fails}")
            break

        issues_text = " | ".join(cred_report.get("issues", []))
        repeated    = cred_report.get("repeated_sentences", [])
        content     = current.get(_ck, "")

        # Build a single LLM revision call that addresses all detected issues at once
        fixes_needed = []
        if repeated:
            fixes_needed.append(
                f"REPEATED SENTENCES: The following sentences appear more than once — "
                f"rewrite all duplicate occurrences with fresh phrasing: "
                + " | ".join(f'"{s[:80]}"' for s in repeated[:6])
            )
        if any("statistic" in i.lower() or "unsupported" in i.lower() for i in cred_report.get("issues", [])):
            fixes_needed.append(
                "UNSUPPORTED STATISTICS: Add 'according to [source]' or 'as reported by [publication]' "
                "before each numeric statistic, or soften to 'estimates suggest roughly X'."
            )
        if any("overconfident" in i.lower() or "definitive" in i.lower() for i in cred_report.get("issues", [])):
            fixes_needed.append(
                "OVERCONFIDENT CLAIMS: Replace absolutist language ('definitely', 'certainly', "
                "'100%', 'it is proven') with 'research suggests', 'evidence indicates', 'may', 'likely'."
            )
        if any("unattributed" in i.lower() or "'experts say'" in i.lower() for i in cred_report.get("issues", [])):
            fixes_needed.append(
                "UNATTRIBUTED QUOTES / 'EXPERTS SAY': Either name the expert + link the source, "
                "or rephrase as 'industry observers note' without a fake quote."
            )
        if any("structural" in i.lower() for i in cred_report.get("issues", [])):
            fixes_needed.append(
                "REPEATED STRUCTURAL PHRASES: Vary section-closing sentences — "
                "don't end every paragraph the same way."
            )

        if not fixes_needed:
            notes.append("  No actionable fixes — stopping early")
            break

        prompt = f"""You are a credibility editor. Apply ONLY the listed surgical fixes to the article HTML.
Do NOT change the meaning, add new facts, invent sources, or alter the overall structure.
Return ONLY the revised full HTML (the content_html value) — no JSON wrapper, no markdown.

FIXES TO APPLY:
{chr(10).join(f"  {i+1}. {f}" for i, f in enumerate(fixes_needed))}

ALL OTHER ISSUES (for awareness only): {issues_text[:300]}

ARTICLE HTML:
{content[:12000]}"""

        try:
            revised_html = _llm(prompt, llm_cfg, json_mode=False, timeout=120, max_tokens=8000)
            # Strip any accidental markdown code fences
            revised_html = re.sub(r'^```html?\s*', '', revised_html.strip(), flags=re.I)
            revised_html = re.sub(r'\s*```$', '', revised_html.strip())
            if len(revised_html) > 500:
                current[_ck] = revised_html
                notes.append(f"  ✓ Applied {len(fixes_needed)} fix category(ies)")
            else:
                notes.append("  ⚠ LLM returned suspiciously short content — keeping previous")
        except Exception as e:
            notes.append(f"  ⚠ LLM revision failed: {str(e)[:80]}")

        # Re-audit
        cred_report = validate_credibility(current)
        score_after = cred_report.get("score", 0)
        score_progression.append(score_after)
        notes.append(f"  📊 Score after loop {loop}: {score_after}/100")

        if score_after >= 90 and not cred_report.get("hard_fail_reasons"):
            notes.append("  ✅ 90+ reached — stopping early")
            break

    final_score = cred_report.get("score", score_progression[0])
    return {
        "revised_blog":          current,
        "new_credibility_score": final_score,
        "new_credibility_grade": cred_report.get("grade", "F"),
        "credibility_status":    cred_report.get("status", ""),
        "score_progression":     score_progression,
        "notes":                 notes,
        "hard_fail_reasons":     cred_report.get("hard_fail_reasons", []),
    }


# ── STAGE 6b: SEO REFINEMENT (auto-fix to hit 90+) ────────────────────────────

def refine_blog_seo(blog: dict, seo_report: dict, gemini_key: str) -> dict:
    """
    Programmatic + lightweight-LLM fixes for the most impactful SEO issues.
    Does NOT rewrite the longform body — protects word count and content quality.
    Works with both 'content_html' (pipeline drafts) and 'content' (stored blogs).
    """
    # Accept both key conventions used across the codebase
    primary_kw = (blog.get("primary_keyword") or blog.get("primaryKeyword") or "").strip()
    _ck = "content_html" if "content_html" in blog else "content"
    content = blog.get(_ck, "")
    issues_text = " | ".join(seo_report.get("issues", []))

    # 1) Fix title + meta description via a short LLM call
    prompt = f"""You are an SEO metadata specialist. Fix ONLY the title and meta description.

PRIMARY KEYWORD: {primary_kw}
CURRENT TITLE: {blog.get('title', '')}
CURRENT META DESCRIPTION: {blog.get('meta_description') or blog.get('summary', '')}
ARTICLE OPENING (first 300 chars): {re.sub('<[^>]+>', '', content)[:300]}
SEO ISSUES TO FIX: {issues_text[:300]}

RULES:
- Title: 50-60 characters, must contain "{primary_kw}", specific and click-worthy (no clickbait)
- Meta: exactly 140-158 characters, must contain "{primary_kw}", end with a call-to-action
- Title must accurately match article content

Respond ONLY with JSON: {{"title": "...", "meta_description": "..."}}"""

    try:
        result = _llm(prompt, gemini_key, json_mode=True, timeout=60, max_tokens=512)
        fixed = _parse_json_lenient(result)
        kw_words = [w for w in primary_kw.lower().split() if len(w) > 2]
        new_title = (fixed.get("title") or "").strip()
        title_has_kw = (
            (primary_kw and primary_kw.lower() in new_title.lower()) or
            (kw_words and all(w in new_title.lower() for w in kw_words))
        )
        if new_title and title_has_kw and 30 <= len(new_title) <= 70:
            blog["title"] = new_title
        if fixed.get("meta_description"):
            md = fixed["meta_description"].strip()
            if len(md) > 160:
                md = md[:157].rstrip() + "..."
            blog["meta_description"] = md
            blog["summary"] = _clamp_summary(md)
    except Exception as e:
        print(f"[refine_seo] metadata fix error: {e}")

    # 2) Hard-enforce meta description length
    md = blog.get("meta_description") or blog.get("summary", "")
    if len(md) > 160:
        blog["meta_description"] = md[:157].rstrip() + "..."

    # 3) Ensure primary keyword appears in first paragraph
    content = blog.get(_ck, "")
    first_chunk = re.sub('<[^>]+>', '', content)[:400].lower()
    if primary_kw and primary_kw.lower() not in first_chunk and "<p>" in content:
        lead = f'<p><strong>{primary_kw.capitalize()}</strong> is reshaping how modern teams build and ship digital products.</p>\n'
        content = content.replace("<p>", lead + "<p>", 1)
        blog[_ck] = content

    # 4) Remove stray H1 tags inside content body (title is the page H1)
    content = blog.get(_ck, "")
    if len(re.findall(r'<h1[^>]*>', content, re.I)) > 1:
        content = re.sub(r'<h1([^>]*)>(.*?)</h1>', r'<h2\1>\2</h2>', content, count=10, flags=re.I | re.S)
        blog[_ck] = content

    # 5) Inject internal links if fewer than 3 are present
    content = blog.get(_ck, "")
    int_link_count = len(re.findall(
        r'href=["\'](?:https?://(?:www\.)?lumynor\.com|/)[^"\']*["\']', content, re.I))
    if int_link_count < 3 and "</p>" in content:
        internal_block = (
            '<p>Looking to put these ideas into practice? Explore how '
            '<a href="/products">Lumynor\'s AI products</a> help startups automate faster, '
            'or <a href="/contact">talk to our team</a> about building an agentic workflow '
            'for your business. Read more on our <a href="/blog">AI &amp; SaaS insights blog</a>.</p>'
        )
        last_p = content.rfind("</p>")
        if last_p > 0:
            content = content[:last_p + 4] + "\n" + internal_block + content[last_p + 4:]
            blog[_ck] = content

    return blog


# ── CONTENT REVISION AUTOMATION ───────────────────────────────────────────────
#
# Targeted revision — the system classifies audit issues into buckets and applies
# the minimum surgical fix for each bucket. It never blindly rewrites the whole
# article. Source issues are fixed by Tavily re-research only; the LLM is never
# asked to fabricate citations. The loop runs at most max_loops times and exits
# early once score ≥ 90.

# Issue text fragments → revision bucket (ALL matches collected per issue).
_REVISION_PATTERNS = (
    ("expand_content",      ("word count", "too short", "far too short", "words — target", "expand")),
    ("rewrite_intro",       ("generic intro", "opener detected", "generic opening", "cliché",
                             "intro starts with")),
    ("fix_title_meta",      ("title length", "title too", "meta description", "clickbait",
                             "primary keyword missing from title",
                             "primary keyword missing from meta")),
    ("add_internal_links",  ("internal link",)),
    ("add_faq",             ("faq", "frequently asked", "schema opportunity")),
    ("humanize_writing",    ("robotic", "transition", "template", "clearly ai",
                             "banned phrase", "banned/generic", "generic phrase")),
    ("fix_keywords",        ("primary keyword not", "keyword not in", "keyword missing from",
                             "keyword density", "keyword underused")),
    ("needs_research",      ("source", "no links to trusted", "external sources",
                             "unsupported claims", "statistics used but no")),
    ("add_conclusion",      ("no conclusion", "conclusion", "key takeaways", "takeaway")),
    ("fix_secondary_kw",    ("secondary keyword", "secondary keywords sparse",
                             "lsi keyword", "related keyword")),
)

# Hard fail strings the revision system CAN attempt to resolve.
# Anything not matching these → unfixable → reject immediately.
_FIXABLE_HARD_FAIL_PATTERNS = (
    "too short", "sources missing", "banned", "generic phrase",
    "no original analysis", "practical examples", "keyword stuffing",
)


def _classify_audit_issues(seo_report: dict) -> dict:
    """
    Map SEO audit issues into targeted revision buckets.

    Every issue is matched against ALL patterns. primary_bucket = first match;
    secondary_buckets = any further matches. Both are logged so mixed-issue cases
    are visible in the loop diff, even though only the primary drives the action.

    Hard fails are split into:
      _fixable_hard_fails   — revision will attempt these
      _unfixable_hard_fails — reject immediately; don't waste loop budget

    Returns a flat dict for backward-compat bucket access (buckets[name]),
    plus _classified, _fixable_hard_fails, _unfixable_hard_fails keys.
    """
    issues    = seo_report.get("issues", [])
    hard_fails = seo_report.get("hard_fail_reasons", [])
    buckets   = {k: [] for k, _ in _REVISION_PATTERNS}
    buckets["hard_fail"] = list(hard_fails)
    classified = []
    for issue in issues:
        il      = issue.lower()
        matched = [bk for bk, pats in _REVISION_PATTERNS if any(p in il for p in pats)]
        primary    = matched[0] if matched else "unclassified"
        secondary  = matched[1:] if len(matched) > 1 else []
        confidence = "high" if len(matched) == 1 else ("medium" if matched else "low")
        if primary != "unclassified":
            buckets[primary].append(issue)
        classified.append({
            "issue":            issue,
            "primary_bucket":   primary,
            "secondary_buckets": secondary,
            "confidence":       confidence,
        })
    buckets["_classified"]           = classified
    buckets["_fixable_hard_fails"]   = [
        h for h in hard_fails
        if any(p in h.lower() for p in _FIXABLE_HARD_FAIL_PATTERNS)
    ]
    buckets["_unfixable_hard_fails"] = [
        h for h in hard_fails if h not in buckets["_fixable_hard_fails"]
    ]
    return buckets


def _fetch_supporting_sources(topic: str, tavily_key: str) -> list:
    """Fetch trusted sources for a topic via Tavily or DDG.
    This is the ONLY approved fix for missing-source issues.
    The LLM is NEVER asked to invent citations."""
    TRUSTED = (
        "openai.com", "anthropic.com", "deepmind.google", "nvidia.com",
        "huggingface.co", "mistral.ai", "google.com", "microsoft.com", "meta.com",
        "techcrunch.com", "venturebeat.com", "theverge.com", "wired.com",
        "arstechnica.com", "zdnet.com", "the-decoder.com",
        "reuters.com", "bloomberg.com", "forbes.com",
        "technologyreview.mit.edu", "arxiv.org", "mit.edu", "stanford.edu",
        "github.com", "stripe.com", "vercel.com", "supabase.com",
        "docker.com", "aws.amazon.com", "cloud.google.com", "azure.microsoft.com",
    )
    new_refs = []
    if tavily_key:
        for q in [
            f"{topic} statistics data research report 2025",
            f"{topic} official announcement guide",
        ]:
            for r in _tavily_search(q, tavily_key, num=5, depth="basic", days=120):
                url = r.get("url", "")
                if url and any(d in url for d in TRUSTED):
                    new_refs.append({"title": r.get("title", url), "url": url})
    else:
        for r in _search_web(f"{topic} statistics research 2025", 8):
            url = r.get("url", "")
            if url and any(d in url for d in TRUSTED):
                new_refs.append({"title": r.get("title", url), "url": url})
    seen, unique = set(), []
    for ref in new_refs:
        if ref["url"] not in seen:
            seen.add(ref["url"])
            unique.append(ref)
    return unique[:6]


def _find_related_blog_links(current_blog: dict, published_blogs: list, n: int = 2) -> list:
    """Return up to n published blogs most related to current_blog by keyword overlap.
    Returns list of dicts: {title, slug, url, anchor}"""
    kw = (current_blog.get("primaryKeyword") or current_blog.get("primary_keyword") or "").lower()
    title_words = set(re.sub(r'[^a-z0-9 ]', '', (current_blog.get("title") or "").lower()).split()) - {
        "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by",
        "how", "what", "why", "is", "are", "was", "will", "can", "do", "does", "this", "that",
    }
    current_id = current_blog.get("id", "")
    scored = []
    for b in published_blogs:
        if not b.get("published") or b.get("id") == current_id:
            continue
        slug = b.get("slug", "")
        if not slug:
            continue
        b_kw  = (b.get("primaryKeyword") or "").lower()
        b_title_words = set(re.sub(r'[^a-z0-9 ]', '', (b.get("title") or "").lower()).split())
        overlap = len(title_words & b_title_words)
        kw_match = int(bool(kw and b_kw and (kw in b_kw or b_kw in kw)))
        score = overlap * 2 + kw_match * 3
        if score > 0:
            scored.append((score, b))
    scored.sort(key=lambda x: x[0], reverse=True)
    result = []
    for _, b in scored[:n]:
        slug = b.get("slug", "")
        title = b.get("title", slug)
        result.append({"title": title, "slug": slug,
                        "url": f"/blog/{slug}", "anchor": title[:50]})
    return result


def revise_blog_from_audit(
    blog: dict,
    audit: dict,
    research_brief: dict,
    llm_cfg,
    tavily_key: str = "",
    max_loops: int = 2,
    published_blogs: list = None,
) -> dict:
    """
    Targeted content revision — classifies issues, applies minimum surgical fix
    per bucket, re-audits, and loops up to max_loops.

    Per loop:
      1. Classify issues (primary + secondary buckets; fixable vs unfixable hard fails)
      2. Unfixable hard fail → reject immediately, break
      3. Source issues → Tavily re-research + floating-claim paragraph attribution (LLM)
      4. Content tasks (expand/intro/humanize/keywords) → single LLM revision call
      5. Internal links → programmatic injection
      6. FAQ → from research brief or short LLM call
      7. Title/meta → runs after ANY content change OR when fix_title_meta issues exist
      8. Re-audit → build structured loop diff; exit early at 90+

    Score thresholds after all loops:
      ≥ 90       → publish
      80-89      → human_review
      < 80       → manual_revision
      hard fail  → reject
    """
    tavily_key = tavily_key or os.getenv("TAVILY_API_KEY", "")
    notes      = []
    loop_diffs = []
    _ck        = "content_html" if "content_html" in blog else "content"
    current    = dict(blog)
    if research_brief and not current.get("research_brief"):
        current["research_brief"] = {"_present": True}

    score_progression = [audit.get("score", 0)]

    for loop in range(1, max_loops + 1):
        score_before        = audit.get("score", 0)
        content_was_changed = False
        sources_added_count = 0
        links_added         = False
        faq_added           = False
        actions_taken       = []

        notes.append(
            f"\n── Revision Loop {loop}/{max_loops} "
            f"(score before: {score_before}/100) ──────────────"
        )
        buckets = _classify_audit_issues(audit)

        # Log multi-bucket issues for visibility
        for c in buckets.get("_classified", []):
            if c["secondary_buckets"]:
                notes.append(
                    f"  ℹ Multi-bucket: '{c['issue'][:60]}' "
                    f"→ primary={c['primary_bucket']}, "
                    f"also={c['secondary_buckets']} [{c['confidence']}]"
                )

        # ── 1. Hard-fail triage ───────────────────────────────────────────────
        unfixable = buckets.get("_unfixable_hard_fails", [])
        fixable   = buckets.get("_fixable_hard_fails", [])
        if unfixable:
            notes.append(f"  ✗ UNFIXABLE hard fail — rejecting immediately: {unfixable}")
            loop_diffs.append({
                "loop_number":          loop,
                "issues_before":        [c["issue"] for c in buckets.get("_classified", [])],
                "actions_taken":        ["REJECTED — unfixable hard fail"],
                "sections_changed":     [],
                "sources_added":        0,
                "internal_links_added": False,
                "faq_added":            False,
                "score_before":         score_before,
                "score_after":          score_before,
                "remaining_hard_fails": buckets.get("hard_fail", []),
            })
            break
        if fixable:
            notes.append(f"  ⚠ Fixable hard fail(s) — will attempt: {fixable}")

        # ── 2. Source issues → Tavily (with primary-keyword fallback) ────────────
        if buckets.get("needs_research"):
            notes.append("  🔍 Source issue — fetching trusted sources (Tavily, no LLM fabrication)")
            topic    = ((current.get("topicData") or {}).get("topic") or current.get("title", ""))
            new_refs = _fetch_supporting_sources(topic, tavily_key)
            # Fallback: retry with primary keyword if title query found nothing
            if not new_refs and tavily_key:
                kw = primary_kw or current.get("primary_keyword") or current.get("primaryKeyword", "")
                if kw and kw.lower() not in topic.lower():
                    notes.append(f"  🔄 Retrying Tavily with primary keyword: '{kw}'")
                    new_refs = _fetch_supporting_sources(kw, tavily_key)
            existing = current.get("references", [])
            existing_urls = {r.get("url", "") for r in existing}
            added = [r for r in new_refs if r["url"] not in existing_urls]
            if added:
                current["references"] = existing + added
                sources_added_count   = len(added)
                raw = current.get(_ck, "")
                if "References" not in raw:
                    ref_html = "<h2>References &amp; Sources</h2><ol>"
                    for r in (existing + added)[:6]:
                        ref_html += (f'<li><a href="{r["url"]}" target="_blank"'
                                     f' rel="noopener noreferrer">{r["title"]}</a></li>')
                    ref_html += "</ol>"
                    current[_ck] = raw.rstrip() + "\n" + ref_html
                notes.append(f"  ✓ {len(added)} trusted source(s) added")
                actions_taken.append(f"Added {len(added)} sources via Tavily")

                # Attribute floating statistics to the newly added sources.
                # Send actual HTML <p> paragraphs (not stripped text) so orig can be
                # matched and spliced back into raw_html without re-escaping issues.
                _stat_pats = (r'\b\d+(?:\.\d+)?%', r'\$[\d\.]+[BMKbmk]?\b',
                              r'\b\d+x\b', r'\b\d{1,3}(?:,\d{3})+\b')
                raw_html = current.get(_ck, "")
                stat_paras = [
                    m.group(0) for m in re.finditer(r'<p[^>]*>.*?</p>', raw_html, re.DOTALL | re.I)
                    if any(re.search(p, re.sub(r'<[^>]+>', ' ', m.group(0))) for p in _stat_pats)
                ][:5]
                if stat_paras:
                    src_ctx = "; ".join(f"{r['title']} ({r['url']})" for r in added[:3])
                    cite_prompt = (
                        "You are editing a blog to improve source attribution.\n\n"
                        f"Newly added trusted sources:\n{src_ctx}\n\n"
                        "TASK: For each paragraph below that contains floating statistics with no "
                        "attribution, revise it to naturally credit the relevant source — e.g. "
                        "'According to [source name],...' or '[Domain] reports that...'. "
                        "Do NOT invent new facts. Preserve all HTML tags exactly.\n\n"
                        "Paragraphs:\n" + "\n---\n".join(stat_paras) + "\n\n"
                        'Return ONLY JSON: {"revised_paragraphs": '
                        '[{"original": "exact <p>...</p> as given", "revised": "revised <p>...</p>"}]}'
                    )
                    try:
                        cite_data = _parse_json_lenient(
                            _llm(cite_prompt, llm_cfg, json_mode=True,
                                 timeout=90, max_tokens=2048)
                        )
                        changed = 0
                        for pair in cite_data.get("revised_paragraphs", []):
                            orig = (pair.get("original") or "").strip()
                            repl = (pair.get("revised") or "").strip()
                            # Word-count guard: reject if rewrite shrinks paragraph >25%
                            orig_wc = len(re.sub(r'<[^>]+>', ' ', orig).split())
                            repl_wc = len(re.sub(r'<[^>]+>', ' ', repl).split())
                            if orig and repl and orig in raw_html and repl_wc >= orig_wc * 0.75:
                                raw_html = raw_html.replace(orig, repl, 1)
                                changed += 1
                        if changed:
                            current[_ck] = raw_html
                            notes.append(f"  ✓ {changed} paragraph(s) attributed to sources")
                            actions_taken.append(f"Source attribution: {changed} paragraph(s)")
                    except Exception as e:
                        notes.append(f"  ⚠ Source attribution call failed: {str(e)[:60]}")
            else:
                notes.append("  ⚠ No additional trusted sources found — flagged as remaining risk")

        # ── 3. Surgical content fixes ─────────────────────────────────────────
        # Each issue type extracts only the specific element(s) that need fixing,
        # sends just those to the LLM, and splices the result back. This prevents
        # content truncation (the previous full-article approach caused word count
        # loss when the model only saw [:7000] chars of a long article).
        #
        # Exception: expand_content still needs full-article context — it uses a
        # larger window ([:30000]) plus a word-count guard that rejects the revision
        # if the model returns fewer words than the original.

        primary_kw = current.get("primary_keyword") or current.get("primaryKeyword", "")
        brief_summary = ""
        if research_brief:
            facts = research_brief.get("main_facts", research_brief.get("key_facts", []))[:5]
            stats = research_brief.get("key_statistics", [])[:3]
            avoid = research_brief.get("claims_to_avoid", [])[:3]
            brief_summary = (
                "Verified facts: " + "; ".join(str(f) for f in facts) + "\n"
                "Key statistics: " + "; ".join(str(s) for s in stats) + "\n"
                "Do NOT include: " + "; ".join(str(a) for a in avoid)
            )[:1500]

        # ── 3a. Expand content (full-article, word-count guarded) ─────────────
        if buckets.get("expand_content"):
            wc = audit.get("word_count", 0)
            raw_content = current.get(_ck, "")
            orig_wc = len(re.sub(r'<[^>]+>', ' ', raw_content).split())
            expand_prompt = f"""You are expanding a short SEO blog for Lumynor Systems.

Add content to every existing H2 section until each is 200-350 words.
Do NOT delete or shorten any existing content.
Do NOT invent statistics or company claims.
Use ONLY facts from the research brief below.
Keep tone: human, business-focused, developer-friendly.

BLOG TITLE: {current.get("title", "")}
PRIMARY KEYWORD: {primary_kw}
CURRENT WORD COUNT: {wc} (target: 1500+)

Research Brief:
{brief_summary or "(no brief — expand using only facts already in the article)"}

Full Blog HTML:
{raw_content[:30000]}{"...[truncated — preserve ALL sections including those not shown]" if len(raw_content) > 30000 else ""}

Return ONLY this JSON:
{{
  "revised_content": "COMPLETE expanded HTML with all original sections + new content added",
  "word_count_estimate": 0,
  "sections_expanded": ["section name 1", "section name 2"]
}}"""
            try:
                result  = _llm(expand_prompt, llm_cfg, json_mode=True, timeout=280, max_tokens=32768)
                rev     = _parse_json_lenient(result)
                new_html = (rev.get("revised_content") or "").strip()
                new_wc   = len(re.sub(r'<[^>]+>', ' ', new_html).split())
                if new_html and new_wc >= orig_wc * 0.85:
                    current[_ck]        = new_html
                    content_was_changed  = True
                    notes.append(f"  ✓ Expanded: {orig_wc} → {new_wc} words "
                                 f"(sections: {rev.get('sections_expanded', [])})")
                    actions_taken.append(f"Expanded {orig_wc}→{new_wc} words")
                elif new_html:
                    notes.append(
                        f"  ⚠ Expand rejected — model returned fewer words "
                        f"({new_wc} vs {orig_wc} original), keeping original"
                    )
                else:
                    notes.append("  ✗ Expand returned empty content")
            except Exception as e:
                notes.append(f"  ✗ Expand failed: {str(e)[:100]}")

        # ── 3b. Surgical intro rewrite ────────────────────────────────────────
        if buckets.get("rewrite_intro"):
            html = current.get(_ck, "")
            m = re.search(r'(<p[^>]*>)(.*?)(</p>)', html, re.DOTALL | re.I)
            if m:
                orig_para = m.group(0)
                intro_prompt = (
                    "Rewrite this blog intro paragraph for a B2B SaaS/AI tech blog.\n\n"
                    "Rules:\n"
                    "- Do NOT open with: 'In today's', 'The world of', 'With the rise of', "
                    "'Artificial intelligence is transforming', or any generic preamble.\n"
                    "- Open with a specific data point, contrarian claim, or concrete scenario "
                    "a SaaS founder or developer would immediately recognise.\n"
                    "- Under 100 words. No fluff.\n\n"
                    f"Primary keyword to include: {primary_kw}\n\n"
                    f"Paragraph to rewrite:\n{orig_para}\n\n"
                    'Return ONLY JSON: {"revised": "<p>revised paragraph</p>"}'
                )
                try:
                    r        = _parse_json_lenient(_llm(intro_prompt, llm_cfg,
                                                        json_mode=True, timeout=60, max_tokens=512))
                    new_para = (r.get("revised") or "").strip()
                    if new_para and "<p" in new_para:
                        current[_ck]        = html.replace(orig_para, new_para, 1)
                        content_was_changed  = True
                        notes.append("  ✓ Intro rewritten (surgical)")
                        actions_taken.append("Intro rewritten")
                    else:
                        notes.append("  ⚠ Intro fix returned malformed output, skipped")
                except Exception as e:
                    notes.append(f"  ✗ Intro fix failed: {str(e)[:60]}")

        # ── 3c. Surgical humanize fix ─────────────────────────────────────────
        # Two layers:
        #   Layer 1 — Programmatic: remove/replace known banned single words/phrases
        #             directly in HTML without an LLM call (fast, zero hallucination).
        #   Layer 2 — LLM: rewrite paragraphs containing robotic transition phrases
        #             that need more context to fix naturally.
        if buckets.get("humanize_writing"):
            _BANNED_REPLACEMENTS = {
                "seamlessly":            "smoothly",
                "game-changer":          "significant shift",
                "game changer":          "significant shift",
                "revolutionize":         "transform",
                "unlock the power":      "take advantage",
                "cutting-edge technology": "modern technology",
                "leveraging ai":         "using AI",
                "leveraging the power":  "using the power",
                "delve into":            "explore",
                "delve in":              "look into",
                "it's worth noting":     "notably",
                "it is worth noting":    "notably",
                "this groundbreaking":   "this",
                "this revolutionary":    "this",
                "the future is here":    "this is already happening",
                "needless to say":       "",
            }
            _BANNED_TRANS = (
                "Furthermore,", "Moreover,", "Additionally,",
                "It is important to note", "It should be noted", "Consequently,",
                "In conclusion,", "In summary,", "To summarize,",
            )
            html = current.get(_ck, "")

            # Layer 1: direct string replacement (case-insensitive)
            swaps = 0
            for bad, good in _BANNED_REPLACEMENTS.items():
                pattern = re.compile(re.escape(bad), re.I)
                new_html, n = pattern.subn(good, html)
                if n:
                    html   = new_html
                    swaps += n
            if swaps:
                current[_ck]        = html
                content_was_changed  = True
                notes.append(f"  ✓ Banned words replaced inline ({swaps} substitution(s))")
                actions_taken.append(f"Banned word replacements: {swaps}")

            # Layer 2: LLM rewrite for robotic transition paragraphs
            html = current.get(_ck, "")
            para_iter = re.finditer(r'<p[^>]*>.*?</p>', html, re.DOTALL | re.I)
            bad_paras = [m.group(0) for m in para_iter
                         if any(t in m.group(0) for t in _BANNED_TRANS)][:8]
            if bad_paras:
                humanize_prompt = (
                    "Rewrite each paragraph below to remove robotic AI transitions.\n"
                    "Rules: Remove 'Furthermore,', 'Moreover,', 'Additionally,', "
                    "'It is important to note', 'Consequently,', 'In conclusion,', etc. "
                    "Start sentences with the subject. Keep all facts unchanged. "
                    "Sound like a developer explaining to a peer.\n\n"
                    "Paragraphs:\n" +
                    "\n---\n".join(f"PARA_{i}:\n{p}" for i, p in enumerate(bad_paras)) +
                    '\n\nReturn ONLY JSON: {"revisions": '
                    '[{"original": "exact original paragraph", "revised": "fixed paragraph"}]}'
                )
                try:
                    r    = _parse_json_lenient(_llm(humanize_prompt, llm_cfg,
                                                    json_mode=True, timeout=120, max_tokens=4096))
                    html = current.get(_ck, "")
                    fixed = 0
                    for pair in r.get("revisions", []):
                        orig = (pair.get("original") or "").strip()
                        repl = (pair.get("revised") or "").strip()
                        # Word-count guard: reject if rewrite shrinks paragraph >25%
                        orig_wc = len(re.sub(r'<[^>]+>', ' ', orig).split())
                        repl_wc = len(re.sub(r'<[^>]+>', ' ', repl).split())
                        if orig and repl and orig in html and repl_wc >= orig_wc * 0.75:
                            html   = html.replace(orig, repl, 1)
                            fixed += 1
                    if fixed:
                        current[_ck]        = html
                        content_was_changed  = True
                        notes.append(f"  ✓ Humanized {fixed}/{len(bad_paras)} paragraph(s) (surgical)")
                        actions_taken.append(f"Humanized {fixed} paragraph(s)")
                    else:
                        notes.append("  ⚠ Humanize LLM: no paragraphs matched for splice, skipped")
                except Exception as e:
                    notes.append(f"  ✗ Humanize LLM failed: {str(e)[:60]}")

        # ── 3d. Surgical keyword fix ──────────────────────────────────────────
        # Only runs when expand_content is NOT active (expansion already adds keyword naturally).
        if buckets.get("fix_keywords") and not buckets.get("expand_content") and primary_kw:
            html      = current.get(_ck, "")
            html_head = html[:600]  # first ~100 words
            if primary_kw.lower() not in html_head.lower():
                # Find the first <p> or <h1>/<h2> to inject keyword into
                m = re.search(r'(?:<h[12][^>]*>.*?</h[12]>|<p[^>]*>.*?</p>)',
                              html, re.DOTALL | re.I)
                if m:
                    target = m.group(0)
                    kw_prompt = (
                        f"Naturally add the keyword '{primary_kw}' to this HTML element. "
                        "Only add it if it reads naturally — do NOT force it. "
                        "Do NOT change any other content.\n\n"
                        f"Element:\n{target}\n\n"
                        'Return ONLY JSON: {"revised": "<element with keyword added>"}'
                    )
                    try:
                        r       = _parse_json_lenient(_llm(kw_prompt, llm_cfg,
                                                           json_mode=True, timeout=60, max_tokens=512))
                        new_el  = (r.get("revised") or "").strip()
                        if new_el and primary_kw.lower() in new_el.lower() and new_el != target:
                            current[_ck]        = html.replace(target, new_el, 1)
                            content_was_changed  = True
                            notes.append(f"  ✓ Keyword '{primary_kw}' injected surgically")
                            actions_taken.append(f"Keyword injected into first element")
                        else:
                            notes.append(f"  ⚠ Keyword injection: model didn't add '{primary_kw}', skipped")
                    except Exception as e:
                        notes.append(f"  ✗ Keyword fix failed: {str(e)[:60]}")

        # ── 5. Programmatic internal link injection ───────────────────────────
        if buckets.get("add_internal_links"):
            raw = current.get(_ck, "")
            int_count = len(re.findall(
                r'href=["\'](?:https?://(?:www\.)?lumynor\.com|/)[^"\']*["\']', raw, re.I))
            if int_count < 3 and "</p>" in raw:
                # Build dynamic links: prefer topically related published blogs,
                # fall back to the three hardcoded evergreen pages.
                related = _find_related_blog_links(current, published_blogs or [], n=2) if published_blogs else []
                if related:
                    rel_links = " | ".join(
                        f'<a href="{r["url"]}">{r["anchor"]}</a>' for r in related
                    )
                    block = (
                        f'<p>Want to go deeper? {rel_links} — '
                        f'or <a href="/contact">talk to the Lumynor team</a> about '
                        f'building your own agentic product.</p>'
                    )
                else:
                    block = (
                        '<p>Looking to put these ideas into practice? Explore how '
                        '<a href="/products/agent-forge">Agent Forge</a> helps SaaS teams '
                        'ship agentic products faster, or <a href="/contact">talk to our team</a>. '
                        'More on the <a href="/blog">Lumynor AI &amp; SaaS blog</a>.</p>'
                    )
                lp = raw.rfind("</p>")
                if lp > 0:
                    current[_ck] = raw[:lp + 4] + "\n" + block + raw[lp + 4:]
                    links_added = True
                    rel_note = f" (linked to: {', '.join(r['anchor'][:30] for r in related)})" if related else ""
                    notes.append(f"  ✓ Internal links injected ({int_count} → 3+){rel_note}")
                    actions_taken.append("Internal links injected")
            else:
                notes.append(f"  ℹ Internal links: already {int_count} present, skipping")

        # ── 6. FAQ injection ──────────────────────────────────────────────────
        if buckets.get("add_faq"):
            raw = current.get(_ck, "")
            if not re.search(r'<h[23][^>]*>[^<]*(FAQ|Frequently Asked)', raw, re.I):
                faq_items = (research_brief or {}).get("faqs", [])
                if not faq_items:
                    faq_prompt = (
                        f'Generate 4 FAQ questions and concise answers for a blog titled '
                        f'"{current.get("title", "")}" about '
                        f'"{current.get("primary_keyword") or current.get("primaryKeyword", "")}".\n'
                        'Answers: 2-3 sentences, factual, no invented statistics.\n'
                        'JSON only: {"faqs": [{"question":"...","answer":"..."}]}'
                    )
                    try:
                        faq_items = _parse_json_lenient(
                            _llm(faq_prompt, llm_cfg, json_mode=True,
                                 timeout=90, max_tokens=1024)
                        ).get("faqs", [])
                    except Exception:
                        faq_items = []
                if faq_items:
                    faq_html = "<h2>Frequently Asked Questions</h2>"
                    for it in faq_items[:5]:
                        q, a = (it.get("question") or "").strip(), (it.get("answer") or "").strip()
                        if q and a:
                            faq_html += f"<h3>{q}</h3><p>{a}</p>"
                    current[_ck] = raw.rstrip() + "\n" + faq_html
                    faq_added = True
                    notes.append(f"  ✓ FAQ added ({len(faq_items)} Q&As)")
                    actions_taken.append(f"FAQ added ({len(faq_items)} Q&As)")

        # ── 6b. Conclusion section injection ──────────────────────────────────
        if buckets.get("add_conclusion"):
            raw = current.get(_ck, "")
            if not re.search(r'<h[23][^>]*>[^<]*(conclusion|key takeaway|summary|wrap)', raw, re.I):
                concl_prompt = (
                    f'Write a "Key Takeaways" conclusion section for a blog titled '
                    f'"{current.get("title", "")}" about '
                    f'"{primary_kw}".\n'
                    "Include: 3-4 bullet takeaways and a 1-sentence CTA pointing to Lumynor Systems.\n"
                    "No invented stats. Factual, human tone.\n"
                    'JSON only: {"conclusion_html": "<h2>Key Takeaways</h2><ul><li>...</li></ul><p>CTA</p>"}'
                )
                try:
                    r = _parse_json_lenient(_llm(concl_prompt, llm_cfg, json_mode=True,
                                                 timeout=60, max_tokens=1024))
                    concl_html = (r.get("conclusion_html") or "").strip()
                    if concl_html and "<h2" in concl_html:
                        # Insert before FAQ if present, otherwise append
                        faq_pos = raw.find("<h2>Frequently Asked")
                        if faq_pos > 0:
                            current[_ck] = raw[:faq_pos] + concl_html + "\n" + raw[faq_pos:]
                        else:
                            current[_ck] = raw.rstrip() + "\n" + concl_html
                        content_was_changed = True
                        notes.append("  ✓ Conclusion / Key Takeaways section added")
                        actions_taken.append("Conclusion section added")
                except Exception as e:
                    notes.append(f"  ✗ Conclusion injection failed: {str(e)[:60]}")

        # ── 6c. Secondary keyword injection ───────────────────────────────────
        if buckets.get("fix_secondary_kw"):
            sec_raw = current.get("secondary_keywords") or current.get("secondaryKeywords", "")
            sec_kws = [s.strip() for s in re.split(r"[,;|]", sec_raw) if s.strip()][:3]
            if sec_kws:
                html = current.get(_ck, "")
                # Find H2 headings that don't yet contain any secondary keyword
                h2_matches = list(re.finditer(r'<h2[^>]*>.*?</h2>', html, re.DOTALL | re.I))
                inject_targets = [m for m in h2_matches
                                  if not any(kw.lower() in m.group(0).lower() for kw in sec_kws)][:3]
                if inject_targets and len(inject_targets) >= len(sec_kws):
                    kw_prompt = (
                        "Naturally add one secondary keyword to each H2 heading listed below. "
                        "Only add if it reads naturally — do NOT force it.\n\n" +
                        "\n".join(f"H2_{i} + keyword '{kw}':\n{m.group(0)}"
                                  for i, (m, kw) in enumerate(zip(inject_targets, sec_kws))) +
                        '\n\nReturn ONLY JSON: {"revisions": '
                        '[{"original": "exact h2", "revised": "h2 with keyword"}]}'
                    )
                    try:
                        r    = _parse_json_lenient(_llm(kw_prompt, llm_cfg,
                                                        json_mode=True, timeout=60, max_tokens=1024))
                        html = current.get(_ck, "")
                        fixed = 0
                        for pair in r.get("revisions", []):
                            orig = (pair.get("original") or "").strip()
                            repl = (pair.get("revised") or "").strip()
                            if orig and repl and orig in html and orig != repl:
                                html   = html.replace(orig, repl, 1)
                                fixed += 1
                        if fixed:
                            current[_ck]        = html
                            content_was_changed  = True
                            notes.append(f"  ✓ {fixed} secondary keyword(s) injected into H2s")
                            actions_taken.append(f"Secondary keywords: {fixed} H2s updated")
                    except Exception as e:
                        notes.append(f"  ✗ Secondary keyword injection failed: {str(e)[:60]}")

        # ── 7. Title/meta — always after content changes OR when issues found ─
        # Running after a content expansion ensures meta reflects the new angle.
        if buckets.get("fix_title_meta") or content_was_changed:
            try:
                current = refine_blog_seo(current, audit, llm_cfg)
                notes.append("  ✓ Title/meta refined (post-content pass)")
                actions_taken.append("Title/meta refined")
            except Exception as e:
                notes.append(f"  ✗ Title/meta fix failed: {str(e)[:60]}")

        # ── 8. Re-audit + structured diff ────────────────────────────────────
        audit_input = {
            "title":              current.get("title", ""),
            "meta_description":   current.get("meta_description") or current.get("summary", ""),
            "content_html":       current.get(_ck, ""),
            "primary_keyword":    current.get("primary_keyword") or current.get("primaryKeyword", ""),
            "secondary_keywords": current.get("secondary_keywords") or current.get("secondaryKeywords", ""),
            "coverImage":         current.get("coverImage", ""),
            "references":         current.get("references", []),
            "research_brief":     {"_present": True} if research_brief else {},
        }
        audit       = validate_seo(audit_input)
        score_after = audit["score"]
        score_progression.append(score_after)

        _non_meta_buckets = {"hard_fail", "_classified",
                             "_fixable_hard_fails", "_unfixable_hard_fails"}
        loop_diffs.append({
            "loop_number":          loop,
            "issues_before":        [c["issue"] for c in buckets.get("_classified", [])],
            "actions_taken":        actions_taken,
            "sections_changed":     [
                bk for bk in buckets
                if buckets.get(bk) and isinstance(buckets[bk], list)
                and bk not in _non_meta_buckets
            ],
            "sources_added":        sources_added_count,
            "internal_links_added": links_added,
            "faq_added":            faq_added,
            "score_before":         score_before,
            "score_after":          score_after,
            "remaining_hard_fails": audit.get("hard_fail_reasons", []),
        })
        notes.append(
            f"  📊 Score after loop {loop}: {score_after}/100 "
            f"Grade {audit['grade']} — {audit['status']}"
        )
        if score_after >= 90 and not audit.get("hard_fail_reasons"):
            notes.append("  ✅ Threshold 90+ reached — stopping early")
            break

    # ── Final verdict ─────────────────────────────────────────────────────────
    final_score  = audit.get("score", 0)
    remaining_hf = audit.get("hard_fail_reasons", [])
    if remaining_hf:
        recommendation = "reject"
        verdict = (f"Reject — hard fail remains after {max_loops} loop(s): "
                   f"{' | '.join(remaining_hf)}")
    elif final_score >= 90:
        recommendation = "publish"
        verdict = f"Publish-ready — {final_score}/100 Grade {audit.get('grade', 'A')}"
    elif final_score >= 80:
        recommendation = "human_review"
        verdict = f"Human review — {final_score}/100 (80-89, close but not auto-publish ready)"
    else:
        recommendation = "manual_revision"
        verdict = f"Manual revision needed — {final_score}/100, too many issues remain"

    notes.append(f"\n📋 FINAL VERDICT: {verdict}")

    final_blog = dict(current)
    if _ck == "content_html":
        final_blog["content"] = final_blog.get("content_html", "")

    return {
        "revised_blog":           final_blog,
        "revision_notes":         notes,
        "loop_diffs":             loop_diffs,
        "score_progression":      score_progression,
        "new_seo_score":          final_score,
        "new_seo_grade":          audit.get("grade", "F"),
        "publish_recommendation": recommendation,
        "verdict":                verdict,
        "remaining_issues":       audit.get("issues", []),
        "hard_fail_reasons":      remaining_hf,
    }


# ── MASTER PIPELINE ───────────────────────────────────────────────────────────

def run_auto_blog_pipeline(settings: dict, gemini_key: str, recent_topics: list = None,
                           published_blogs: list = None) -> dict:
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

    # Stage 1: Cluster queue → pick pending topic, or crawl for a new pillar topic.
    # If settings has pending_cluster_topics, consume the first one instead of
    # running full trending-topic research. This implements a pillar+cluster strategy:
    # each pillar run generates subtopics that are queued for the next N runs.
    pending_clusters = settings.get("pending_cluster_topics", [])
    if pending_clusters:
        queued = pending_clusters[0]
        topic          = queued.get("topic", "")
        angle          = queued.get("angle", "Deep-dive supporting post")
        target_audience = queued.get("target_audience", "SaaS developers and digital product teams")
        topic_data     = {"topic": topic, "angle": angle, "target_audience": target_audience,
                          "total_score": "queued", "cluster_sources": []}
        settings["pending_cluster_topics"] = pending_clusters[1:]
        log.append(f"📌 Using queued cluster topic: {topic}")
    else:
        log.append("🔍 Crawling trusted AI sources and scoring topics...")
        topic_data = research_trending_topics(
            niche, keywords_hint, llm_cfg,
            recent_topics=recent_topics, tavily_key=tavily_key,
        )
        topic          = topic_data.get("topic", f"AI Trends in {niche}")
        angle          = topic_data.get("angle", "Comprehensive guide")
        target_audience = topic_data.get("target_audience", "SaaS developers and digital product teams")
        log.append(f"📌 Pillar topic selected: {topic} (score: {topic_data.get('total_score','?')})")

        # Generate 3 cluster subtopics for the next 3 runs (stored in settings).
        try:
            cluster_prompt = (
                f"You just chose '{topic}' as a pillar blog topic for a SaaS/AI blog.\n"
                "Generate 3 specific supporting (cluster) subtopic ideas that:\n"
                "- Each covers ONE narrow aspect of the pillar\n"
                "- Together form a content cluster that internally links to the pillar\n"
                "- Would rank for long-tail keywords\n\n"
                'Return ONLY JSON: {"cluster_topics": ['
                '{"topic": "...", "angle": "...", "target_audience": "..."}]}'
            )
            _ct = _parse_json_lenient(_llm(cluster_prompt, llm_cfg, json_mode=True,
                                           timeout=60, max_tokens=512))
            new_clusters = _ct.get("cluster_topics", [])[:3]
            if new_clusters:
                settings["pending_cluster_topics"] = (
                    settings.get("pending_cluster_topics", []) + new_clusters
                )
                log.append(f"🗂 Queued {len(new_clusters)} cluster subtopics for future runs")
        except Exception as _ce:
            log.append(f"⚠ Cluster topic generation failed: {_ce}")

    cluster_sources = topic_data.get("cluster_sources", [])

    # Near-duplicate guard: warn if chosen topic strongly overlaps with recent titles.
    _topic_words = {w for w in re.sub(r'[^a-z0-9 ]', ' ', topic.lower()).split() if len(w) > 4}
    _dup_titles = [t for t in (recent_topics or []) if isinstance(t, str)
                   and len(_topic_words & {w for w in re.sub(r'[^a-z0-9 ]', ' ', t.lower()).split()
                                           if len(w) > 4}) >= 3]
    if _dup_titles:
        log.append(f"⚠ Near-duplicate: topic shares 3+ keywords with '{_dup_titles[0][:60]}'")

    blog_format = settings.get("blog_format", "deep_dive")
    month_year  = datetime.now().strftime("%B %Y")

    if blog_format == "roundup":
        # Roundup path: skip stages 2-4; gather real headlines and write a Top-10 post.
        log.append(f"📰 Format: ROUNDUP — collecting top 10 AI headlines for {month_year}...")
        try:
            blog_content = write_roundup_blog(niche, month_year, gemini_key, tavily_key=tavily_key)
            _sec = blog_content.get("secondary_keywords", [])
            keywords = {
                "primary_keyword": blog_content.get("primary_keyword", f"AI news {month_year}"),
                "secondary_keywords": _sec if isinstance(_sec, list) else [_sec],
                "lsi_keywords": [],
                "people_also_ask": [],
            }
            research       = {"references": blog_content.get("references", []),
                              "key_facts": [], "key_statistics": [], "expert_insights": [], "real_examples": []}
            research_brief = {"core_angle": f"Top 10 AI headlines roundup for {month_year}",
                              "lumynor_perspective": "What these AI developments mean for SaaS and agentic product builders",
                              "suggested_outline": [], "faqs": [], "_present": True}
            word_count = len(re.sub(r'<[^>]+>', ' ', blog_content.get("content_html", "")).split())
            log.append(f"📝 Roundup written: {word_count} words")
        except Exception as _re:
            log.append(f"⚠️ Roundup failed ({str(_re)[:80]}) — falling back to deep-dive")
            blog_format = "deep_dive"

    if blog_format != "roundup":
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
        _min_words = int(os.getenv("BLOG_MIN_WORD_COUNT", "1800"))
        blog_content = None
        _wc_hint = None
        for _attempt in range(1, 4):
            try:
                _draft = write_longform_blog(
                    topic, angle, keywords, research, target_audience, llm_cfg,
                    quality_hints=_wc_hint,
                    research_brief=research_brief,
                )
                _wc = len(re.sub('<[^>]+>', '', _draft.get("content_html", "")).split())
                blog_content = _draft
                if _wc >= _min_words or _attempt == 3:
                    log.append(f"📝 Blog written: {_wc} words" + (f" (attempt {_attempt})" if _attempt > 1 else ""))
                    break
                log.append(f"⚠️ Attempt {_attempt}: only {_wc} words (min {_min_words}) — rewriting with length hint...")
                _wc_hint = (f"CRITICAL: Your previous draft was only {_wc} words. "
                            f"You MUST write at least {_min_words} words. "
                            "Every H2 section must be 250-350 words. Expand all sections with more detail, "
                            "examples, statistics, and business context. Do not summarise — elaborate.")
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

        # Inject research_brief so validate_seo can award the "unique angle" check.
        # The full brief is too large for the blog object — pass a lightweight sentinel.
        if research_brief and not draft.get("research_brief"):
            draft["research_brief"] = {"_present": True}

        # Inject dynamic internal links to published sibling posts (pillar ↔ cluster).
        if published_blogs:
            _related = _find_related_blog_links(draft, published_blogs, n=2)
            _ch3 = draft.get("content_html", "")
            if _related and not any(r["url"] in _ch3 for r in _related):
                rel_links = " | ".join(f'<a href="{r["url"]}">{r["anchor"]}</a>' for r in _related)
                il_block = (
                    f'<p>Want to go deeper? {rel_links} — '
                    f'or <a href="/contact">talk to the Lumynor team</a> about '
                    f'building your own agentic product.</p>'
                )
                draft["content_html"] = _ch3 + "\n" + il_block

        # Inject schema.org Article + FAQPage JSON-LD for structured data / rich results.
        ch_ld = draft.get("content_html", "")
        if "<script type=\"application/ld+json\">" not in ch_ld:
            title_ld  = (draft.get("title") or "").replace('"', '\\"')
            desc_ld   = (draft.get("summary") or draft.get("meta_description") or "")[:160].replace('"', '\\"')
            faq_items_ld = draft.get("faq", []) or []
            # Also pull FAQ items already embedded in the HTML
            if not faq_items_ld:
                faq_items_ld = [
                    {"question": re.sub(r'<[^>]+>', '', q), "answer": re.sub(r'<[^>]+>', '', a)}
                    for q, a in re.findall(r'<h3[^>]*>(.*?)</h3>\s*<p[^>]*>(.*?)</p>',
                                           ch_ld, re.DOTALL | re.I)
                ]
            faq_ld_items = "".join(
                f'{{"@type":"Question","name":"{it.get("question","").replace(chr(34), chr(39))}",'
                f'"acceptedAnswer":{{"@type":"Answer","text":"{it.get("answer","").replace(chr(34), chr(39))[:300]}"}}}}'
                + ("," if i < len(faq_items_ld) - 1 else "")
                for i, it in enumerate(faq_items_ld[:8])
            )
            schema_blocks = (
                f'<script type="application/ld+json">'
                f'{{"@context":"https://schema.org","@type":"Article",'
                f'"headline":"{title_ld}","description":"{desc_ld}",'
                f'"author":{{"@type":"Organization","name":"Lumynor Systems"}},'
                f'"publisher":{{"@type":"Organization","name":"Lumynor Systems",'
                f'"url":"https://lumynor.com"}}}}'
                f'</script>'
            )
            if faq_ld_items:
                schema_blocks += (
                    f'\n<script type="application/ld+json">'
                    f'{{"@context":"https://schema.org","@type":"FAQPage",'
                    f'"mainEntity":[{faq_ld_items}]}}'
                    f'</script>'
                )
            draft["content_html"] = schema_blocks + "\n" + ch_ld

        # Repetition check — deduplicate repeated sentences before SEO validation
        _dupes = _find_repeated_sentences(draft.get("content_html", ""))
        if _dupes:
            _ck = "content_html"
            _rep_prompt = (
                "You are a copy editor. The following article contains repeated sentences "
                "(exact or near-exact duplicates). Rewrite ONLY the second and subsequent "
                "occurrences of each repeated sentence with fresh phrasing that conveys the same idea. "
                "Return ONLY the full revised HTML — no JSON, no markdown wrapper.\n\n"
                f"REPEATED SENTENCES:\n" + "\n".join(f'- "{s[:100]}"' for s in _dupes[:8]) +
                f"\n\nARTICLE HTML:\n{draft.get(_ck, '')[:12000]}"
            )
            try:
                _dedup_html = _llm(_rep_prompt, gemini_key, json_mode=False, timeout=90, max_tokens=8000)
                _dedup_html = re.sub(r'^```html?\s*', '', _dedup_html.strip(), flags=re.I)
                _dedup_html = re.sub(r'\s*```$', '', _dedup_html.strip())
                if len(_dedup_html) > 500:
                    draft[_ck] = _dedup_html
            except Exception as _de:
                print(f"[repetition_fix] {_de}")

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

    # Quality rewrite: if SEO is still weak after refinement, rewrite the full article
    # body once — passing the specific issues back into the LLM so it can address them.
    # Images are already sourced above and are reused in _finalize_draft.
    _min_quality = int(os.getenv("BLOG_MIN_QUALITY_SCORE", "90"))
    if seo_report.get("hard_fail_reasons"):
        log.append(f"🚫 Hard Fail: {' | '.join(seo_report['hard_fail_reasons'])}")
    if seo_report["score"] < _min_quality and seo_report.get("issues"):
        hints = " | ".join(i for i in seo_report["issues"])
        log.append(f"🔄 SEO {seo_report['score']}/100 below threshold {_min_quality} — rewriting with {len(seo_report['issues'])} targeted fixes...")
        try:
            _rewrite = write_longform_blog(
                topic, angle, keywords, research, target_audience, gemini_key,
                quality_hints=hints, research_brief=research_brief,
            )
            _rewrite["image_prompts"] = blog_content.get("image_prompts", [])
            _rewrite, _seo_rewrite = _finalize_draft(_rewrite)
            if _seo_rewrite["score"] >= seo_report["score"]:
                blog_content, seo_report = _rewrite, _seo_rewrite
            else:
                log.append(f"⚠️ Rewrite scored {_seo_rewrite['score']} — keeping original {seo_report['score']}")
        except Exception as e:
            log.append(f"⚠️ Quality rewrite failed ({str(e)[:60]}), keeping original")

    # Full audit report (matches the guide's output format)
    _cat = seo_report.get("category_scores", {})
    _cat_max = {
        "topic_intent": 10, "research_quality": 15, "keyword_usage": 10,
        "title_meta": 10, "structure": 10, "helpful_content": 15,
        "human_writing": 10, "links": 10, "faq_schema_image": 5, "readability": 5,
    }
    _cat_lines = "".join(
        "  " + k.replace("_", " ").title().ljust(25) + str(v) + "/" + str(_cat_max.get(k, 10)) + "\n"
        for k, v in _cat.items()
    )
    _passed_lines  = "".join("  ✓ " + p + "\n" for p in seo_report.get("passed", []))
    _issue_lines   = "".join("  ✗ " + i + "\n" for i in seo_report.get("issues", []))
    _fix_lines     = "".join("  → " + f + "\n" for f in seo_report.get("fixes", []))
    log.append(
        "\n═══ SEO AUDIT REPORT ═════════════════════════════════\n"
        "SEO Score: " + str(seo_report["score"]) + "/100  Grade: " + seo_report["grade"] + "\n"
        "Status: " + seo_report.get("status", "") + "\n"
        "Words: " + str(seo_report.get("word_count", 0)) + "\n\n"
        "Category Breakdown:\n" + _cat_lines +
        "\nPassed:\n" + _passed_lines +
        "\nIssues:\n" + _issue_lines +
        "\nRecommended Fixes:\n" + _fix_lines +
        "═════════════════════════════════════════════════════"
    )

    # Build final blog object
    now = datetime.utcnow()
    _SLUG_STOP = {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "how", "what", "why", "when", "where", "is", "are",
        "was", "were", "will", "can", "do", "does", "this", "that", "s",
    }
    _slug_words = [w for w in re.sub(r'[^a-z0-9]+', '-',
                   blog_content.get("title", topic).lower()).split('-')
                   if w and w not in _SLUG_STOP]
    slug = '-'.join(_slug_words)[:70].strip('-')

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
