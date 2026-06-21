"""
Revenue Radar OS — Phase 2
Lead discovery (manual + regulated automated), lead scoring,
relationship memory, market radar, and launch readiness intelligence.
"""
import json
import re
from datetime import datetime, timezone
from db import _sb, get_settings

# ── Product definitions ────────────────────────────────────────────────────────

PRODUCTS = ['district21', 'agentforge', 'linkforge']

PRODUCT_META = {
    'district21': {
        'name': 'District21',
        'description': 'Event tech platform for organizers, colleges, and venues in India',
        'categories': ['event_organizer', 'event_company', 'venue', 'college', 'festival'],
        'target_description': 'event management companies, festival organizers, colleges, venues, and cultural event coordinators in India',
    },
    'agentforge': {
        'name': 'AgentForge',
        'description': 'AI agent OS for SaaS founders, agencies, and startup studios',
        'categories': ['saas_founder', 'startup_studio', 'agency', 'consultant'],
        'target_description': 'SaaS founders, startup studios, software agencies, and AI-focused consultants',
    },
    'linkforge': {
        'name': 'LinkForge',
        'description': 'Link building and SEO intelligence platform',
        'categories': ['seo_agency', 'saas_company', 'content_business'],
        'target_description': 'SEO agencies, SaaS companies with blogs, and content-heavy businesses needing backlinks',
    },
}

# Search query templates per product + category
DISCOVERY_QUERIES = {
    'district21': {
        'event_organizer':  [
            'event management company India site:linkedin.com',
            'event organizer company India B2B',
            'corporate event management India startup',
        ],
        'event_company':    [
            'event production company India',
            'experiential marketing agency India',
        ],
        'venue':            [
            'event venue booking platform India',
            'banquet hall management software India',
        ],
        'college':          [
            'college cultural fest organizer India',
            'student event management association India',
        ],
        'festival':         [
            'music festival organizer India',
            'food festival event company India',
        ],
    },
    'agentforge': {
        'saas_founder':     [
            'SaaS startup founder AI automation tools',
            'B2B SaaS product company India startup',
        ],
        'startup_studio':   [
            'startup studio venture builder India',
            'startup studio AI product company',
        ],
        'agency':           [
            'software development agency AI automation',
            'digital product agency India',
        ],
        'consultant':       [
            'AI consultant freelance B2B SaaS',
            'technology consultant automation India',
        ],
    },
    'linkforge': {
        'seo_agency':       [
            'SEO agency link building services',
            'SEO consulting firm digital marketing',
        ],
        'saas_company':     [
            'SaaS company blog content marketing backlinks',
            'B2B SaaS company SEO strategy',
        ],
        'content_business': [
            'content marketing agency blog publishing',
            'content-heavy website media company SEO',
        ],
    },
}

MARKET_RADAR_QUERIES = {
    'district21': [
        ('competitor', 'event management software India competitor 2025'),
        ('competitor', 'Eventbrite alternative India event platform'),
        ('launch',     'event tech startup India launch 2025'),
        ('trend',      'event management industry trends India 2025'),
        ('pricing',    'event management software pricing India'),
    ],
    'agentforge': [
        ('competitor', 'AI agent platform SaaS competitor 2025'),
        ('competitor', 'AI automation tool for agencies competitor'),
        ('launch',     'AI agent startup launch funding 2025'),
        ('trend',      'agentic AI SaaS trends 2025'),
        ('pricing',    'AI agent platform pricing model 2025'),
    ],
    'linkforge': [
        ('competitor', 'link building SaaS tool competitor 2025'),
        ('competitor', 'SEO link building platform alternative'),
        ('launch',     'SEO tool startup launch 2025'),
        ('trend',      'link building SEO trends 2025'),
        ('pricing',    'link building service pricing 2025'),
    ],
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json(text: str) -> dict:
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return {}


def _llm_cfg():
    stored = get_settings("auto_blog")
    from auto_blogger import _build_llm_cfg
    return _build_llm_cfg(stored, stored.get("llmApiKey", ""))


def _llm(prompt: str, json_mode=True, max_tokens=600) -> str:
    from auto_blogger import _llm as llm_call
    return llm_call(prompt, _llm_cfg(), json_mode=json_mode, timeout=60, max_tokens=max_tokens)


def _search(query: str, num: int = 6) -> list:
    from auto_blogger import _search_web
    return _search_web(query, num)


def _normalize_company(name: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', '', name.lower())).strip()


def _is_url_reachable(url: str) -> bool:
    """Quick HEAD request to verify a URL is alive. Returns False on any error."""
    if not url or not url.startswith('http'):
        url = f'https://{url}' if url else ''
    if not url:
        return False
    try:
        import requests
        r = requests.head(url, timeout=5, allow_redirects=True,
                          headers={'User-Agent': 'Mozilla/5.0'})
        return r.status_code < 500
    except Exception:
        return False


# ── Lead CRUD ─────────────────────────────────────────────────────────────────

def get_leads(product: str = None, temperature: str = None, status: str = None,
              category: str = None, limit: int = 100) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("revenue_leads").select(
        "*, lead_contacts(id, name, role)"
    )
    if product:
        q = q.eq("product_target", product)
    if temperature:
        q = q.eq("temperature", temperature)
    if status:
        q = q.eq("status", status)
    if category:
        q = q.eq("category", category)
    return q.order("discovered_at", desc=True).limit(limit).execute().data or []


def create_lead(data: dict, source: str = 'manual') -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {
        "company_name":    data.get("company_name", "").strip(),
        "website":         data.get("website", "").strip(),
        "linkedin_url":    data.get("linkedin_url", "").strip(),
        "contact_email":   data.get("contact_email", "").strip(),
        "location":        data.get("location", "").strip(),
        "product_target":  data.get("product_target", ""),
        "category":        data.get("category", ""),
        "temperature":     data.get("temperature", "cold"),
        "relevance_score": int(data.get("relevance_score", 0)),
        "business_size":   data.get("business_size", "smb"),
        "digital_maturity": data.get("digital_maturity", "basic"),
        "buying_signals":  data.get("buying_signals", ""),
        "source":          source,
        "source_url":      data.get("source_url", ""),
        "notes":           data.get("notes", ""),
        "status":          "confirmed" if source == "manual" else "pending_review",
    }
    res = sb.table("revenue_leads").insert(payload).execute()
    return (res.data or [{}])[0]


def update_lead(lead_id: str, data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    allowed = {"company_name", "website", "linkedin_url", "contact_email",
               "location", "category", "temperature", "relevance_score",
               "business_size", "digital_maturity", "buying_signals",
               "notes", "status", "source_url"}
    payload = {k: v for k, v in data.items() if k in allowed}
    payload["updated_at"] = _now()
    res = sb.table("revenue_leads").update(payload).eq("id", lead_id).execute()
    return (res.data or [{}])[0]


def delete_lead(lead_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("revenue_leads").delete().eq("id", lead_id).execute()
    return True


def approve_lead(lead_id: str) -> dict:
    return update_lead(lead_id, {"status": "confirmed"})


def reject_lead(lead_id: str) -> dict:
    return update_lead(lead_id, {"status": "rejected"})


# ── Auto-Discovery ─────────────────────────────────────────────────────────────

def _existing_company_names(product: str) -> set:
    leads = get_leads(product=product, limit=500)
    return {_normalize_company(l.get("company_name", "")) for l in leads}


def _score_result(result: dict, product: str, category: str) -> dict | None:
    """
    Ask the LLM to evaluate a single search result.
    Returns a structured lead dict, or None if the result fails the quality gate.
    """
    meta = PRODUCT_META[product]
    prompt = f"""You are evaluating a search result to determine if it represents a genuine, high-quality sales lead for {meta['name']}.

Product: {meta['name']}
Product description: {meta['description']}
Target audience: {meta['target_description']}
Category being searched: {category}

Search result:
Title: {result.get('title', '')}
URL: {result.get('url', '')}
Snippet: {result.get('snippet', '')}

Evaluate this result and respond ONLY with a JSON object:
{{
  "company_name": "exact company name (empty string if not identifiable)",
  "website": "clean root domain URL e.g. https://acme.com (empty string if not found)",
  "linkedin_url": "LinkedIn company page URL if visible in title/snippet/URL, else empty string",
  "contact_email": "any contact email found in snippet, else empty string",
  "location": "city/country or empty string",
  "relevance_score": 0-100,
  "business_size": "startup|smb|enterprise",
  "digital_maturity": "basic|intermediate|advanced",
  "buying_signals": "specific signals that suggest they might need this product, or empty string",
  "temperature": "hot|warm|cold",
  "should_include": true or false,
  "reject_reason": "why rejected, or empty string if included"
}}

Rules for should_include=true:
- Must be a REAL, IDENTIFIABLE company (not a directory, blog post, or news article)
- relevance_score must be >= 60
- Must actually match the target audience
- Must have a real website or LinkedIn presence

Be strict. Quality over quantity."""

    try:
        raw = _llm(prompt, json_mode=True, max_tokens=400)
        scored = _extract_json(raw)
        if not scored:
            return None
        if not scored.get("should_include"):
            return None
        if scored.get("relevance_score", 0) < 60:
            return None
        if not scored.get("company_name", "").strip():
            return None
        website = scored.get("website", "").strip()
        # If LLM didn't extract a clean website but the source URL is a real domain, use it
        if not website and result.get("url", "").startswith("http"):
            url = result["url"]
            # Only use source URL as website if it's not LinkedIn/social/directory
            skip_domains = ("linkedin.com", "facebook.com", "twitter.com", "x.com",
                            "instagram.com", "wikipedia.org", "indiamart.com",
                            "justdial.com", "yelp.com", "yellowpages")
            if not any(d in url for d in skip_domains):
                from urllib.parse import urlparse
                parsed = urlparse(url)
                website = f"{parsed.scheme}://{parsed.netloc}"

        return {
            "company_name":    scored["company_name"].strip(),
            "website":         website,
            "linkedin_url":    scored.get("linkedin_url", "").strip(),
            "contact_email":   scored.get("contact_email", "").strip(),
            "location":        scored.get("location", "").strip(),
            "product_target":  product,
            "category":        category,
            "temperature":     scored.get("temperature", "cold"),
            "relevance_score": min(100, max(0, int(scored.get("relevance_score", 0)))),
            "business_size":   scored.get("business_size", "smb"),
            "digital_maturity": scored.get("digital_maturity", "basic"),
            "buying_signals":  scored.get("buying_signals", ""),
            "source_url":      result.get("url", ""),
        }
    except Exception as e:
        print(f"[revenue_radar] Score error: {e}")
        return None


def run_auto_discovery(product: str, category: str, limit: int = 10, location: str = '') -> dict:
    """
    Regulated automated lead discovery.
    - Runs targeted searches for the product+category+location
    - Each result is individually scored by LLM (relevance gate >= 60)
    - Deduplicates against existing leads in DB
    - Saves only verified leads as status=pending_review
    - Returns counts: searched, passed_gate, duplicates_skipped, saved
    """
    if product not in PRODUCTS:
        return {"error": f"Unknown product: {product}"}
    if category not in DISCOVERY_QUERIES.get(product, {}):
        return {"error": f"Unknown category '{category}' for {product}"}

    limit = min(limit, 20)  # hard cap at 20 per scan
    location = location.strip()
    base_queries = DISCOVERY_QUERIES[product][category]
    # Append location to each query if provided, otherwise use as-is
    queries = [f"{q} {location}" if location else q for q in base_queries]
    existing = _existing_company_names(product)

    searched = 0
    passed_gate = 0
    duplicates_skipped = 0
    saved = 0
    saved_leads = []
    seen_in_session = set()

    for query in queries:
        if saved >= limit:
            break
        results = _search(query, num=6)
        searched += len(results)
        for result in results:
            if saved >= limit:
                break
            scored = _score_result(result, product, category)
            if not scored:
                continue
            passed_gate += 1
            norm = _normalize_company(scored["company_name"])
            if norm in existing or norm in seen_in_session:
                duplicates_skipped += 1
                continue
            seen_in_session.add(norm)
            # Validate website is reachable — skip lead if URL is dead
            if scored.get("website") and not _is_url_reachable(scored["website"]):
                print(f"[revenue_radar] Skipping {scored['company_name']} — website unreachable: {scored['website']}")
                scored["website"] = ""  # clear dead URL but still save the lead
            lead = create_lead(scored, source='auto_scan')
            if lead.get("id"):
                saved += 1
                saved_leads.append(lead)

    return {
        "product": product,
        "category": category,
        "searched": searched,
        "passed_gate": passed_gate,
        "duplicates_skipped": duplicates_skipped,
        "saved": saved,
        "leads": saved_leads,
    }


# ── Contacts CRUD ─────────────────────────────────────────────────────────────

def get_contacts(lead_id: str) -> list:
    sb = _sb()
    if not sb:
        return []
    return sb.table("lead_contacts").select("*").eq("lead_id", lead_id)\
             .order("created_at").execute().data or []


def create_contact(data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {
        "lead_id":  data.get("lead_id"),
        "name":     data.get("name", "").strip(),
        "role":     data.get("role", "").strip(),
        "linkedin": data.get("linkedin", "").strip(),
        "email":    data.get("email", "").strip(),
        "notes":    data.get("notes", "").strip(),
    }
    res = sb.table("lead_contacts").insert(payload).execute()
    return (res.data or [{}])[0]


def update_contact(contact_id: str, data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    allowed = {"name", "role", "linkedin", "email", "notes"}
    payload = {k: v for k, v in data.items() if k in allowed}
    res = sb.table("lead_contacts").update(payload).eq("id", contact_id).execute()
    return (res.data or [{}])[0]


def delete_contact(contact_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("lead_contacts").delete().eq("id", contact_id).execute()
    return True


# ── Market Radar ──────────────────────────────────────────────────────────────

def get_signals(product: str = None, include_dismissed: bool = False) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("market_signals").select("*")
    if product:
        q = q.eq("product", product)
    if not include_dismissed:
        q = q.eq("dismissed", False)
    return q.order("detected_at", desc=True).limit(100).execute().data or []


def _score_signal(result: dict, product: str, signal_type: str) -> dict | None:
    prompt = f"""You are evaluating a search result for market intelligence about {product}.

Signal type being tracked: {signal_type}
Title: {result.get('title', '')}
URL: {result.get('url', '')}
Snippet: {result.get('snippet', '')}

Respond ONLY with JSON:
{{
  "title": "concise title for this signal (max 80 chars)",
  "summary": "2-3 sentence summary of what this signal means for {product}",
  "importance": "high|medium|low",
  "should_include": true or false
}}

Include only if it contains genuinely useful market intelligence (competitor moves, pricing info, new launches, meaningful trends).
Exclude: generic articles, press releases without substance, unrelated content."""

    try:
        raw = _llm(prompt, json_mode=True, max_tokens=300)
        scored = _extract_json(raw)
        if not scored or not scored.get("should_include"):
            return None
        return {
            "product":     product,
            "signal_type": signal_type,
            "title":       scored.get("title", result.get("title", ""))[:200],
            "summary":     scored.get("summary", ""),
            "source_url":  result.get("url", ""),
            "importance":  scored.get("importance", "medium"),
        }
    except Exception as e:
        print(f"[revenue_radar] Signal score error: {e}")
        return None


def run_market_scan(product: str) -> dict:
    if product not in PRODUCTS and product != 'all':
        return {"error": f"Unknown product: {product}"}

    products_to_scan = PRODUCTS if product == 'all' else [product]
    sb = _sb()
    if not sb:
        return {"error": "Database not available"}

    # Existing URLs to deduplicate
    existing_urls = {
        s.get("source_url", "") for s in get_signals(include_dismissed=True)
    }

    total_saved = 0
    results_by_product = {}

    for prod in products_to_scan:
        saved_signals = []
        queries = MARKET_RADAR_QUERIES.get(prod, [])
        for signal_type, query in queries:
            results = _search(query, num=5)
            for result in results:
                if result.get("url") in existing_urls:
                    continue
                signal = _score_signal(result, prod, signal_type)
                if not signal:
                    continue
                res = sb.table("market_signals").insert(signal).execute()
                row = (res.data or [{}])[0]
                if row.get("id"):
                    saved_signals.append(row)
                    existing_urls.add(result.get("url"))
                    total_saved += 1
        results_by_product[prod] = len(saved_signals)

    return {"total_saved": total_saved, "by_product": results_by_product}


def dismiss_signal(signal_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("market_signals").update({"dismissed": True}).eq("id", signal_id).execute()
    return True


# ── Launch Readiness ──────────────────────────────────────────────────────────

def get_launch_readiness() -> dict:
    sb = _sb()
    if not sb:
        return {}

    all_leads = sb.table("revenue_leads")\
        .select("product_target, temperature, status")\
        .eq("status", "confirmed")\
        .execute().data or []

    readiness = {}
    for prod in PRODUCTS:
        leads = [l for l in all_leads if l["product_target"] == prod]
        hot   = sum(1 for l in leads if l["temperature"] == "hot")
        warm  = sum(1 for l in leads if l["temperature"] == "warm")
        cold  = sum(1 for l in leads if l["temperature"] == "cold")
        total = len(leads)

        # Readiness score: weighted lead count + bonus tiers
        score = min(100, int(
            (hot * 10) +
            (warm * 4) +
            (cold * 1) +
            (20 if hot >= 5 else 10 if hot >= 2 else 0) +   # hot tier bonus
            (15 if warm >= 10 else 7 if warm >= 5 else 0) +  # warm tier bonus
            (10 if total >= 20 else 5 if total >= 10 else 0)  # coverage bonus
        ))

        pending = sum(
            1 for l in (sb.table("revenue_leads")
                .select("id")
                .eq("product_target", prod)
                .eq("status", "pending_review")
                .execute().data or [])
        )

        signals = len(get_signals(product=prod))

        readiness[prod] = {
            "product":      prod,
            "name":         PRODUCT_META[prod]["name"],
            "total_leads":  total,
            "hot":          hot,
            "warm":         warm,
            "cold":         cold,
            "pending_review": pending,
            "market_signals": signals,
            "readiness_score": score,
            "readiness_label": (
                "Launch Ready" if score >= 70 else
                "Getting There" if score >= 40 else
                "Early Stage"
            ),
        }

    return readiness
