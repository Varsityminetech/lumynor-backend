"""
Lumynor Auto Blogger — Full Pipeline
Trending Research → Keyword Research → Web Research → Longform Writing → SEO → Images
"""

import os
import json
import urllib.request
import urllib.parse
import re
from datetime import datetime

# ── GEMINI HELPER ──────────────────────────────────────────────────────────────

def _gemini(prompt: str, api_key: str, json_mode: bool = False, timeout: int = 60, max_tokens: int = 8192) -> str:
    """Call Gemini 2.5 Flash and return text response."""
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
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
        return result["candidates"][0]["content"]["parts"][0]["text"].strip()


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

    result = _gemini(prompt, gemini_key, json_mode=True)
    try:
        return json.loads(result)
    except:
        return {"topic": f"The Future of {niche} in 2025", "angle": "Comprehensive guide", "why_trending": "Growing interest", "target_audience": "professionals", "search_intent": "informational"}


# ── STAGE 2: SEO KEYWORD RESEARCH ─────────────────────────────────────────────

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

    result = _gemini(prompt, gemini_key, json_mode=True)
    try:
        return json.loads(result)
    except:
        return {
            "primary_keyword": topic.lower().split()[0],
            "secondary_keywords": [topic, niche],
            "lsi_keywords": [],
            "search_volume": "1K-10K",
            "keyword_difficulty": "medium",
            "people_also_ask": [],
            "meta_title": topic[:60],
            "meta_description": f"Learn about {topic} in this comprehensive guide."
        }


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

    result = _gemini(prompt, gemini_key, json_mode=True, timeout=45)
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
4. Include internal linking placeholders: [INTERNAL: link text]
5. Images get descriptive alt text placeholders: [IMAGE: descriptive caption]

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

    result = _gemini(prompt, gemini_key, json_mode=True, timeout=180, max_tokens=32768)
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


# ── STAGE 5: AI IMAGE GENERATION ──────────────────────────────────────────────

def generate_blog_images(image_prompts: list, nanobanana_key: str = "", nanobanana_url: str = "") -> list:
    """
    Generate images using Nanobanana API.
    Falls back to placeholder SVG URLs if API not configured.
    """
    generated = []

    for img in image_prompts:
        placement = img.get("placement", "cover")
        prompt_text = img.get("prompt", "")
        alt = img.get("alt", "")

        image_url = None

        # Try Nanobanana API if configured
        if nanobanana_key and nanobanana_url:
            try:
                api_endpoint = nanobanana_url.rstrip("/")
                payload = json.dumps({
                    "prompt": prompt_text,
                    "width": 1200 if placement == "cover" else 800,
                    "height": 630 if placement == "cover" else 450,
                    "style": "photorealistic",
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"{api_endpoint}/generate",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {nanobanana_key}"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = json.loads(resp.read().decode())
                    image_url = resp_data.get("url") or resp_data.get("image_url") or resp_data.get("data", {}).get("url")
            except Exception as e:
                print(f"[image_gen] Nanobanana error for {placement}: {e}")

        # Fallback: use a descriptive placeholder
        if not image_url:
            encoded_prompt = urllib.parse.quote(prompt_text[:80])
            # Use a free placeholder service
            image_url = f"https://placehold.co/1200x630/0a0e1a/00f0ff?text={encoded_prompt}"

        generated.append({
            "placement": placement,
            "url": image_url,
            "alt": alt,
            "prompt": prompt_text
        })

    return generated


# ── STAGE 6: SEO VALIDATION ────────────────────────────────────────────────────

def validate_seo(blog: dict) -> dict:
    """Score the blog against Google SEO guidelines. Returns score + fixes."""
    score = 0
    issues = []
    fixes = []

    title = blog.get("title", "")
    meta_desc = blog.get("meta_description", "")
    content = blog.get("content_html", "")
    primary_kw = blog.get("primary_keyword", "").lower()
    faq = blog.get("faq", [])

    # Title checks
    if primary_kw in title.lower():
        score += 15
    else:
        issues.append("Primary keyword missing from title")
        fixes.append(f"Add '{primary_kw}' to title")

    if 40 <= len(title) <= 65:
        score += 5
    else:
        issues.append(f"Title length {len(title)} (ideal: 40-65)")

    # Meta description
    if meta_desc and primary_kw in meta_desc.lower():
        score += 10
    else:
        issues.append("Primary keyword missing from meta description")

    if 130 <= len(meta_desc) <= 165:
        score += 5
    else:
        issues.append(f"Meta description length {len(meta_desc)} (ideal: 130-165)")

    # Content checks
    word_count = len(re.sub('<[^>]+>', '', content).split())
    if word_count >= 1500:
        score += 15
    elif word_count >= 800:
        score += 8
        issues.append(f"Word count {word_count} — aim for 1500+")
    else:
        issues.append(f"Word count too low: {word_count}")

    # Keyword density — realistic for multi-word phrases.
    # Score on the best of: exact-phrase density OR average individual-word density.
    clean_text = re.sub('<[^>]+>', '', content).lower()
    words_list = clean_text.split()
    total_words = max(len(words_list), 1)
    kw_words = [w for w in primary_kw.lower().split() if len(w) > 2] if primary_kw else []

    exact_count = clean_text.count(primary_kw.lower()) if primary_kw else 0
    exact_density = (exact_count / total_words) * 100

    # Coverage: do the keyword's words each appear a healthy number of times?
    if kw_words:
        word_hits = [clean_text.count(w) for w in kw_words]
        avg_word_density = (sum(word_hits) / len(kw_words) / total_words) * 100
        all_words_present = all(h > 0 for h in word_hits)
    else:
        avg_word_density = exact_density
        all_words_present = exact_count > 0

    if 0.4 <= exact_density <= 3.0:
        score += 15
    elif all_words_present and 0.5 <= avg_word_density <= 6.0:
        # Multi-word keyword: each component used naturally throughout
        score += 14
    elif exact_count > 0 or all_words_present:
        score += 9
        issues.append(f"Keyword density {exact_density:.1f}% exact (ideal: 0.5-2.5%)")
    else:
        issues.append("Primary keyword not found in content")

    # Headings
    h2_count = content.count("<h2")
    h3_count = content.count("<h3")
    if h2_count >= 3:
        score += 10
    else:
        issues.append(f"Only {h2_count} H2 headings (aim for 4+)")

    # FAQ
    if len(faq) >= 5:
        score += 10
    elif len(faq) >= 3:
        score += 6
        issues.append("FAQ has fewer than 5 questions")
    else:
        issues.append("Missing FAQ section")

    # References
    if "<a " in content and ("href=" in content):
        score += 5
    else:
        issues.append("No outbound links / references")

    # Images (check content, cover, or generated image_prompts/images array)
    has_images = (
        "<img" in content
        or blog.get("cover_image")
        or blog.get("coverImage")
        or blog.get("images")
        or blog.get("image_prompts")
    )
    if has_images:
        score += 5
    else:
        issues.append("No images found")

    # Lists
    if "<ul" in content or "<ol" in content:
        score += 5
    else:
        issues.append("No bullet or numbered lists")

    return {
        "score": min(score, 100),
        "grade": "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D",
        "word_count": word_count,
        "issues": issues,
        "fixes": fixes
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
        result = _gemini(prompt, gemini_key, json_mode=True, timeout=40, max_tokens=1024)
        fixed = _parse_json_lenient(result)
        if fixed.get("title") and primary_kw.lower() in fixed["title"].lower():
            blog["title"] = fixed["title"]
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

    log = []

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

    # Stage 5: Generate images
    image_prompts = blog_content.get("image_prompts", [])
    log.append(f"🖼️ Generating {len(image_prompts)} images...")
    images = generate_blog_images(image_prompts, nanobanana_key, nanobanana_url)
    blog_content["images"] = images  # so SEO validator sees them

    # Set cover image
    cover_image = ""
    for img in images:
        if img["placement"] == "cover":
            cover_image = img["url"]
            break

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
        "primaryKeyword": keywords.get("primary_keyword", ""),
        "secondaryKeywords": ", ".join(keywords.get("secondary_keywords", [])),
        "metaTitle": blog_content.get("title", topic),
        "metaDescription": blog_content.get("meta_description", ""),
        "readTime": blog_content.get("read_time", f"{max(1, word_count // 200)} min read"),
        "tags": blog_content.get("tags", [niche.lower(), category.lower()]),
        "faq": blog_content.get("faq", []),
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
