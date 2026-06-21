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
# Base queries used when NO location is given — broad India-wide searches
DISCOVERY_QUERIES = {
    'district21': {
        'event_organizer':  [
            'event management company India',
            'event organizer company India',
            'corporate event planner India',
            'event management services India',
        ],
        'event_company':    [
            'event production company India',
            'experiential marketing agency India',
            'event entertainment company India',
        ],
        'venue':            [
            'event venue India banquet hall',
            'wedding venue India event space',
            'conference venue India',
        ],
        'college':          [
            'college cultural fest organizer India',
            'student event management India university',
        ],
        'festival':         [
            'music festival organizer India',
            'food festival event company India',
            'cultural festival organizer India',
        ],
    },
    'agentforge': {
        'saas_founder':     [
            'SaaS startup founder AI automation India',
            'B2B SaaS product company India',
            'SaaS founder AI tools',
        ],
        'startup_studio':   [
            'startup studio venture builder India',
            'startup studio AI product company',
        ],
        'agency':           [
            'software development agency AI automation India',
            'digital product agency India',
            'tech agency AI tools',
        ],
        'consultant':       [
            'AI consultant B2B SaaS India',
            'technology consultant automation India',
        ],
    },
    'linkforge': {
        'seo_agency':       [
            'SEO agency link building services India',
            'SEO consulting firm digital marketing India',
            'link building agency India',
        ],
        'saas_company':     [
            'SaaS company content marketing India',
            'B2B SaaS company SEO India',
        ],
        'content_business': [
            'content marketing agency India',
            'media publishing company SEO India',
        ],
    },
}

# Location-specific query templates — {city} substituted at runtime.
# Goal: maximum surface area. We want every business in the category, not just "professional" ones.
# Includes directory-specific, social, contact-number, and platform searches.
LOCATION_QUERY_TEMPLATES = {
    'district21': {
        'event_organizer': [
            # Generic — cast widest net first
            'event organizer {city}',
            'event management company {city}',
            'event planner {city}',
            'event management {city}',
            'best event organizer {city}',
            # Subcategory terms (wedding/corporate/birthday all count)
            'wedding planner {city}',
            'corporate event management {city}',
            'birthday party organizer {city}',
            'event decorator {city}',
            'party organizer {city}',
            # Directory searches — these pages list multiple businesses with phone numbers
            'event organizer {city} site:justdial.com',
            'event management {city} site:sulekha.com',
            'event planner {city} site:indiamart.com',
            'event organizer {city} site:indiacom.com',
            'event management company {city} contact number',
            # Social
            'event organizer {city} Instagram',
            'event management company {city} Facebook',
            # Platform / listing
            'event organizer {city} WedMeGood',
            'event planner {city} ShaadiSaga',
            'event company {city} Wedbook',
            # Contact-intent
            'event organizer {city} phone mobile contact',
        ],
        'event_company': [
            'event company {city}',
            'event production company {city}',
            'entertainment company {city}',
            'event management agency {city}',
            'experiential marketing {city}',
            'event company {city} site:justdial.com',
            'event production {city} site:sulekha.com',
            'entertainment agency {city} contact number',
            'event company {city} Instagram',
        ],
        'venue': [
            'banquet hall {city}',
            'event venue {city}',
            'wedding venue {city}',
            'party hall {city}',
            'conference hall {city}',
            'banquet hall {city} site:justdial.com',
            'wedding venue {city} site:sulekha.com',
            'banquet hall {city} contact phone',
            'marriage garden {city}',
            'farmhouse event venue {city}',
        ],
        'college': [
            'college cultural fest {city}',
            'university event committee {city}',
            'college event management society {city}',
            'student union {city} college',
            'management college fest {city}',
        ],
        'festival': [
            'festival organizer {city}',
            'music festival {city}',
            'cultural festival company {city}',
            'mela organizer {city}',
            'fair organizer {city} India',
            'festival management company {city}',
        ],
    },
    'agentforge': {
        'saas_founder': [
            'SaaS startup {city}',
            'tech startup {city}',
            'software startup founder {city}',
            'B2B SaaS company {city}',
            'SaaS product company {city}',
            'tech founder {city} India',
            'startup {city} site:linkedin.com',
            'SaaS company {city} contact',
        ],
        'startup_studio': [
            'startup studio {city}',
            'venture builder {city}',
            'startup incubator {city}',
            'startup accelerator {city}',
            'product studio {city} India',
        ],
        'agency': [
            'software development agency {city}',
            'tech agency {city}',
            'IT company {city}',
            'digital agency {city}',
            'web development company {city}',
            'software company {city} site:justdial.com',
            'IT company {city} contact',
        ],
        'consultant': [
            'AI consultant {city}',
            'technology consultant {city}',
            'IT consultant {city}',
            'tech consultant freelance {city}',
            'business automation consultant {city}',
        ],
    },
    'linkforge': {
        'seo_agency': [
            'SEO agency {city}',
            'digital marketing agency {city}',
            'SEO company {city}',
            'link building agency {city}',
            'digital marketing company {city} site:justdial.com',
            'SEO agency {city} contact phone',
            'online marketing company {city}',
        ],
        'saas_company': [
            'SaaS company {city}',
            'software product company {city}',
            'B2B software company {city}',
            'SaaS startup {city} India',
        ],
        'content_business': [
            'content marketing agency {city}',
            'content writing company {city}',
            'media company {city}',
            'blog publishing company {city}',
            'digital content agency {city}',
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


def _search_multi(query: str, num: int = 10, india_region: bool = False) -> list:
    """
    Search across multiple engines and deduplicate by URL.
    Uses DuckDuckGo (global + India region) and Bing for broader coverage.
    india_region=True sets DDGS region to in-en which gives far better results
    for Indian city queries like "event organizer Udaipur".
    """
    seen_urls = set()
    combined  = []

    def _add(results):
        for r in results:
            url = r.get("url", "") or r.get("href", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined.append(r)

    # 1. DuckDuckGo — India region (best for local Indian queries)
    if india_region:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                hits = ddgs.text(query, region='in-en', max_results=num)
                _add([{"title": h.get("title",""), "url": h.get("href",""), "snippet": h.get("body","")} for h in (hits or [])])
        except Exception as e:
            print(f"[search] DDGS India error: {e}")

    # 2. DuckDuckGo — global (catches international sources)
    try:
        from auto_blogger import _search_web
        _add(_search_web(query, num))
    except Exception as e:
        print(f"[search] DDGS global error: {e}")

    # 3. Bing — no API key needed, different index from DDG
    try:
        import requests as _req
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}
        r = _req.get(
            "https://www.bing.com/search",
            params={"q": query, "count": num, "mkt": "en-IN" if india_region else "en-US"},
            headers=headers, timeout=8
        )
        if r.ok:
            import re as _re
            # Extract title+URL from Bing HTML result snippets
            titles = _re.findall(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text)
            snippets = _re.findall(r'<p class="b_algoSlug"[^>]*>(.*?)</p>', r.text)
            for i, (url, title) in enumerate(titles[:num]):
                if url.startswith("http") and "bing.com" not in url:
                    snippet = snippets[i] if i < len(snippets) else ""
                    # Strip HTML tags from snippet
                    snippet = _re.sub(r'<[^>]+>', '', snippet)
                    _add([{"title": _re.sub(r'<[^>]+>', '', title), "url": url, "snippet": snippet}])
    except Exception as e:
        print(f"[search] Bing error: {e}")

    return combined


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
        "company_name":     data.get("company_name", "").strip(),
        "website":          data.get("website", "").strip(),
        "linkedin_url":     data.get("linkedin_url", "").strip(),
        "instagram_url":    data.get("instagram_url", "").strip(),
        "facebook_url":     data.get("facebook_url", "").strip(),
        "twitter_url":      data.get("twitter_url", "").strip(),
        "contact_email":    data.get("contact_email", "").strip(),
        "phone":            data.get("phone", "").strip(),
        "whatsapp":         data.get("whatsapp", "").strip(),
        "address":          data.get("address", "").strip(),
        "location":         data.get("location", "").strip(),
        "review_rating":    data.get("review_rating"),
        "review_count":     int(data.get("review_count") or 0),
        "review_platform":  data.get("review_platform", "").strip(),
        "established_year": data.get("established_year"),
        "verified":         bool(data.get("verified", False)),
        "product_target":   data.get("product_target", ""),
        "category":         data.get("category", ""),
        "temperature":      data.get("temperature", "cold"),
        "relevance_score":  int(data.get("relevance_score", 0)),
        "business_size":    data.get("business_size", "smb"),
        "digital_maturity": data.get("digital_maturity", "basic"),
        "buying_signals":   data.get("buying_signals", ""),
        "source":           source,
        "source_url":       data.get("source_url", ""),
        "notes":            data.get("notes", ""),
        "status":           "confirmed" if source == "manual" else "pending_review",
    }
    res = sb.table("revenue_leads").insert(payload).execute()
    return (res.data or [{}])[0]


def update_lead(lead_id: str, data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    allowed = {"company_name", "website", "linkedin_url", "instagram_url",
               "facebook_url", "twitter_url", "contact_email", "phone",
               "whatsapp", "address", "location", "review_rating",
               "review_count", "review_platform", "established_year",
               "verified", "category", "temperature", "relevance_score",
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


def _compute_lead_score(data: dict) -> int:
    """
    Deterministic score from 0-100 based on extracted lead signals.
    LLM extracts raw fields; this function computes the score consistently.

    Breakdown:
      Data completeness  — 35 pts  (contacts we can actually reach them on)
      Social presence    — 25 pts  (Instagram, Facebook, LinkedIn, Twitter)
      Reviews            — 25 pts  (rating + count prove they're a real operating business)
      Authenticity       — 15 pts  (address, established year, verified badge)
    """
    score = 0

    # ── Data completeness (35 pts) ────────────────────────────────────────────
    if data.get("company_name", "").strip():          score += 5
    if data.get("phone") or data.get("whatsapp"):     score += 10
    if data.get("contact_email"):                     score += 7
    if data.get("website"):                           score += 7
    if data.get("location"):                          score += 3
    if data.get("buying_signals"):                    score += 3

    # ── Social presence (25 pts) ──────────────────────────────────────────────
    if data.get("instagram_url"):   score += 10
    if data.get("facebook_url"):    score += 8
    if data.get("linkedin_url"):    score += 5
    if data.get("twitter_url"):     score += 2

    # ── Reviews (25 pts) ──────────────────────────────────────────────────────
    rating = float(data.get("review_rating") or 0)
    count  = int(data.get("review_count") or 0)
    if count > 0:
        # Rating quality
        if rating >= 4.0:   score += 10
        elif rating >= 3.5: score += 7
        elif rating >= 3.0: score += 4
        else:               score += 2
        # Volume of social proof
        if count >= 200:   score += 15
        elif count >= 100: score += 12
        elif count >= 50:  score += 9
        elif count >= 20:  score += 6
        elif count >= 5:   score += 3
        else:              score += 1

    # ── Authenticity (15 pts) ─────────────────────────────────────────────────
    if data.get("address"):            score += 5
    if data.get("established_year"):   score += 5
    if data.get("verified"):           score += 5

    return min(100, score)


_MAJOR_CITIES = [
    'delhi', 'new delhi', 'mumbai', 'bangalore', 'bengaluru', 'hyderabad',
    'chennai', 'kolkata', 'pune', 'ahmedabad', 'surat', 'noida',
    'gurgaon', 'gurugram', 'chandigarh', 'lucknow', 'patna', 'jaipur',
    'indore', 'bhopal', 'nagpur', 'visakhapatnam', 'kochi', 'coimbatore',
    'thane', 'nashik', 'vadodara', 'ludhiana', 'agra', 'varanasi',
    'kanpur', 'meerut', 'rajkot', 'amritsar', 'jabalpur',
]

_CITY_ALIASES = {
    'delhi':     ['new delhi', 'delhi ncr', 'ncr'],
    'bangalore': ['bengaluru'],
    'gurgaon':   ['gurugram'],
    'mumbai':    ['bombay', 'navi mumbai'],
    'kolkata':   ['calcutta'],
    'chennai':   ['madras'],
}


def _location_matches(extracted: str, target_city: str) -> bool:
    """
    Returns True if extracted location is compatible with target city.
    Rejects results that are clearly from a different known major city.
    Empty extracted location gets benefit of the doubt.
    """
    if not extracted or not target_city:
        return True
    city = target_city.lower().strip()
    ext  = extracted.lower().strip()
    if city in ext:
        return True
    # Accept known aliases (e.g. "Bengaluru" when searching "Bangalore")
    for canonical, aliases in _CITY_ALIASES.items():
        if city in (canonical, *aliases) and any(a in ext or canonical in ext for a in aliases):
            return True
    # Reject if result is clearly a different major Indian city
    for mc in _MAJOR_CITIES:
        if mc in ext and mc != city and mc not in city and city not in mc:
            return False
    return True


def _score_result(result: dict, product: str, category: str, target_city: str = '') -> dict | None:
    """
    LLM extracts all available signals from a search result.
    Score is computed deterministically by _compute_lead_score().
    Returns a structured lead dict or None if result is not a real business.
    """
    meta = PRODUCT_META[product]
    city_context = (
        f"\nTARGET LOCATION: {target_city}. ONLY include businesses that are physically located in "
        f"or primarily serve {target_city.split(',')[0].strip()}. "
        f"If the result is clearly from a different city (e.g. Delhi, Mumbai, Bangalore) and has no "
        f"connection to {target_city.split(',')[0].strip()}, set should_include=false."
    ) if target_city else ""

    prompt = f"""You are a lead data extraction engine. Extract every available signal from this search result for our sales database.

Product we sell: {meta['name']}
Target category: {category}{city_context}

Search result:
Title: {result.get('title', '')}
URL: {result.get('url', '')}
Snippet: {result.get('snippet', '')}

Respond ONLY with this JSON — extract everything you can see, leave fields empty string/null if not found:
{{
  "company_name": "name of the business (not the directory — the LISTED business)",
  "website": "company's own website URL (not a directory URL — empty if not found)",
  "linkedin_url": "LinkedIn company/profile URL if visible, else empty",
  "instagram_url": "Instagram profile URL or handle (e.g. https://instagram.com/xyz or @xyz), else empty",
  "facebook_url": "Facebook page URL if visible, else empty",
  "twitter_url": "Twitter/X profile URL if visible, else empty",
  "contact_email": "any email address visible, else empty",
  "phone": "phone/mobile number with country code if shown (e.g. +91-98765-43210), else empty",
  "whatsapp": "WhatsApp number if explicitly mentioned, else same as phone if phone found, else empty",
  "address": "physical street/area address if visible, else empty",
  "location": "city and state extracted from the result (e.g. Udaipur, Rajasthan), else empty",
  "review_rating": null or numeric rating e.g. 4.2,
  "review_count": null or integer number of reviews e.g. 127,
  "review_platform": "Google|JustDial|Sulekha|Facebook|Yelp|other, else empty",
  "established_year": null or integer year e.g. 2012,
  "verified": false or true (true if listing shows verified/trusted badge),
  "business_size": "startup|smb|enterprise",
  "buying_signals": "brief description of why they need our product, else empty",
  "temperature": "hot|warm|cold",
  "should_include": true or false,
  "reject_reason": "only fill if should_include is false"
}}

Rules for should_include:
- true: real business name identifiable AND clearly in the {category} category{' AND located in ' + target_city.split(',')[0].strip() if target_city else ''}
- false: news articles, Wikipedia, government pages, generic "Top 10" lists with no extractable business, completely unidentifiable source{', or business clearly located in a different city' if target_city else ''}
- Directory listings (JustDial, Sulekha, IndiaMart): extract the LISTED BUSINESS details, not the directory
- JustDial snippets show ratings like "4.2 ★ · 127 Ratings" — extract those
- Indian phone formats: +91-XXXXX-XXXXX or 0XXXXXXXXXX or 9XXXXXXXXX — extract as-is
- When in doubt about whether to include, include — our founder reviews all leads before outreach"""

    try:
        raw = _llm(prompt, json_mode=True, max_tokens=600)
        data = _extract_json(raw)
        if not data:
            return None
        if not data.get("should_include"):
            return None
        if not data.get("company_name", "").strip():
            return None

        # Resolve website — fall back to source URL only if it's not a directory/social
        website = data.get("website", "").strip()
        if not website and result.get("url", "").startswith("http"):
            url = result["url"]
            social_dirs = ("linkedin.com", "facebook.com", "twitter.com", "x.com",
                           "instagram.com", "wikipedia.org", "indiamart.com",
                           "justdial.com", "sulekha.com", "yelp.com", "yellowpages",
                           "indiacom.com", "tradeindia.com")
            if not any(d in url for d in social_dirs):
                from urllib.parse import urlparse
                parsed = urlparse(url)
                website = f"{parsed.scheme}://{parsed.netloc}"

        # Instagram from source URL if not extracted by LLM
        instagram_url = data.get("instagram_url", "").strip()
        if not instagram_url and "instagram.com" in result.get("url", ""):
            instagram_url = result["url"]

        # Facebook from source URL if not extracted
        facebook_url = data.get("facebook_url", "").strip()
        if not facebook_url and "facebook.com" in result.get("url", ""):
            facebook_url = result["url"]

        phone    = data.get("phone", "").strip()
        whatsapp = data.get("whatsapp", "").strip() or phone

        # Compute review fields safely
        try:
            review_rating = float(data["review_rating"]) if data.get("review_rating") else None
        except (ValueError, TypeError):
            review_rating = None
        try:
            review_count = int(data["review_count"]) if data.get("review_count") else 0
        except (ValueError, TypeError):
            review_count = 0
        try:
            established_year = int(data["established_year"]) if data.get("established_year") else None
        except (ValueError, TypeError):
            established_year = None

        extracted = {
            "company_name":    data["company_name"].strip(),
            "website":         website,
            "linkedin_url":    data.get("linkedin_url", "").strip(),
            "instagram_url":   instagram_url,
            "facebook_url":    facebook_url,
            "twitter_url":     data.get("twitter_url", "").strip(),
            "contact_email":   data.get("contact_email", "").strip(),
            "phone":           phone,
            "whatsapp":        whatsapp,
            "address":         data.get("address", "").strip(),
            "location":        data.get("location", "").strip(),
            "review_rating":   review_rating,
            "review_count":    review_count,
            "review_platform": data.get("review_platform", "").strip(),
            "established_year": established_year,
            "verified":        bool(data.get("verified", False)),
            "product_target":  product,
            "category":        category,
            "temperature":     data.get("temperature", "cold"),
            "business_size":   data.get("business_size", "smb"),
            "buying_signals":  data.get("buying_signals", ""),
            "source_url":      result.get("url", ""),
        }
        extracted["relevance_score"] = _compute_lead_score(extracted)
        return extracted

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

    limit = min(limit, 100)  # hard cap at 100 per scan
    location = location.strip()
    base_queries = DISCOVERY_QUERIES[product][category]

    city = ''
    if location:
        # Extract just the city name ("Udaipur" from "Udaipur, Rajasthan, India")
        city = location.split(',')[0].strip()
        loc_templates = LOCATION_QUERY_TEMPLATES.get(product, {}).get(category, [])
        # Primary: city-first template queries
        queries = [t.replace('{city}', city) for t in loc_templates]
        # Secondary: base queries appended with city
        queries += [f"{q} {city}" for q in base_queries]
    else:
        queries = base_queries

    existing = _existing_company_names(product)

    searched = 0
    passed_gate = 0
    location_skipped = 0
    duplicates_skipped = 0
    saved = 0
    saved_leads = []
    seen_in_session = set()

    use_india_region = bool(city)  # India region mode when location is specified

    for query in queries:
        if saved >= limit:
            break
        results = _search_multi(query, num=15, india_region=use_india_region)
        searched += len(results)
        for result in results:
            if saved >= limit:
                break
            scored = _score_result(result, product, category, target_city=location)
            if not scored:
                continue
            passed_gate += 1
            # Location post-filter: if we searched for a city and LLM extracted a different major city, skip
            if city and not _location_matches(scored.get("location", ""), city):
                location_skipped += 1
                print(f"[revenue_radar] Location mismatch: wanted {city!r}, got {scored.get('location')!r} — skipping {scored['company_name']}")
                continue
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
        "product":            product,
        "category":           category,
        "searched":           searched,
        "passed_gate":        passed_gate,
        "location_skipped":   location_skipped,
        "duplicates_skipped": duplicates_skipped,
        "saved":              saved,
        "leads":              saved_leads,
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


# ── Intelligence: Funding + Market Trends ────────────────────────────────────

FUNDING_QUERIES = {
    'district21': [
        'startup accelerator event tech hospitality India 2025 applications open',
        'India event management startup grant funding 2025',
        'SaaS startup accelerator India open applications 2025',
        'event technology venture capital investment India 2025',
    ],
    'agentforge': [
        'AI startup accelerator 2025 open applications agentic',
        'generative AI SaaS startup accelerator program 2025',
        'AI agent startup VC funding seed round 2025',
        'Microsoft Google startup program AI 2025 apply',
    ],
    'linkforge': [
        'SEO SaaS startup accelerator funding 2025',
        'content marketing tool startup grant accelerator 2025',
        'B2B SaaS link building startup VC investment 2025',
        'martech startup accelerator open applications 2025',
    ],
    'all': [
        'Y Combinator application 2025 open',
        'Techstars accelerator 2025 apply',
        'Sequoia Surge India startup 2025',
        'Google for Startups accelerator India 2025',
        'Microsoft for Startups program apply 2025',
        'startup India grant scheme 2025 apply',
        'NASSCOM startup warehouse program 2025',
    ],
}

TREND_QUERIES = {
    'district21': [
        'event tech startup ideas trending 2025 funded',
        'hot product ideas events festivals India 2025',
        'event management software new features 2025 demand',
    ],
    'agentforge': [
        'hottest AI agent product ideas 2025 market demand',
        'agentic AI use cases funded startups 2025',
        'new AI automation product trending Product Hunt 2025',
    ],
    'linkforge': [
        'SEO link building new product ideas 2025 trending',
        'content marketing SaaS hot features 2025 demand',
        'backlink analysis tool trends 2025 market',
    ],
    'all': [
        'hottest startup ideas funded 2025 B2B SaaS',
        'trending product categories venture capital 2025',
        'new product launches Product Hunt trending this week',
        'underserved market problems startups solving 2025',
    ],
}


def _score_intelligence(result: dict, product: str, scan_type: str) -> dict | None:
    product_name = PRODUCT_META.get(product, {}).get('name', product) if product != 'all' else 'Lumynor (all products)'
    type_label = 'funding opportunity (accelerator, VC, grant, startup program)' if scan_type == 'funding' else 'market trend or hot product idea'

    prompt = f"""You are evaluating a search result for {product_name} — an early-stage startup.

Looking for: {type_label}
Title: {result.get('title', '')}
URL: {result.get('url', '')}
Snippet: {result.get('snippet', '')}

Respond ONLY with JSON:
{{
  "title": "clear, concise title (max 90 chars)",
  "summary": "2-3 sentences: what this is, why it matters for {product_name}, and any key details like eligibility or deadline",
  "deadline": {"application deadline or program date if mentioned, else empty string" if scan_type == 'funding' else '""'},
  "importance": "high|medium|low",
  "should_include": true or false
}}

Include only if:
{"- It is a REAL, currently active or upcoming funding program / accelerator / grant / VC firm actively investing" if scan_type == 'funding' else "- It represents a GENUINELY trending market need or validated product idea with evidence (funding, traction, discussion volume)"}
- It is clearly relevant to {product_name} or early-stage B2B SaaS startups in general
- Not generic news, opinions without substance, or expired/closed programs

Be strict. Only high-signal items."""

    try:
        raw = _llm(prompt, json_mode=True, max_tokens=400)
        scored = _extract_json(raw)
        if not scored or not scored.get('should_include'):
            return None
        if not scored.get('title', '').strip():
            return None
        return {
            'type':       scan_type,
            'product':    product,
            'title':      scored['title'].strip()[:200],
            'summary':    scored.get('summary', '').strip(),
            'source_url': result.get('url', ''),
            'deadline':   scored.get('deadline', '').strip(),
            'importance': scored.get('importance', 'medium'),
        }
    except Exception as e:
        print(f"[revenue_radar] Intelligence score error: {e}")
        return None


def get_intelligence(product: str = None, scan_type: str = None,
                     include_dismissed: bool = False) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table('intelligence_items').select('*')
    if product:
        q = q.eq('product', product)
    if scan_type:
        q = q.eq('type', scan_type)
    if not include_dismissed:
        q = q.eq('dismissed', False)
    return q.order('detected_at', desc=True).limit(100).execute().data or []


def run_intelligence_scan(product: str = 'all', scan_type: str = 'all') -> dict:
    """Scan for funding opportunities and/or market trends for the given product."""
    sb = _sb()
    if not sb:
        return {'error': 'Database not available'}

    existing_urls = {
        i.get('source_url', '') for i in get_intelligence(include_dismissed=True)
    }

    types_to_scan = ['funding', 'trend'] if scan_type == 'all' else [scan_type]
    products_to_scan = PRODUCTS + ['all'] if product == 'all' else [product, 'all']
    # deduplicate
    products_to_scan = list(dict.fromkeys(products_to_scan))

    total_saved = 0
    by_type = {'funding': 0, 'trend': 0}

    for t in types_to_scan:
        query_map = FUNDING_QUERIES if t == 'funding' else TREND_QUERIES
        for prod in products_to_scan:
            queries = query_map.get(prod, [])
            for query in queries:
                results = _search(query, num=5)
                for result in results:
                    if result.get('url') in existing_urls:
                        continue
                    item = _score_intelligence(result, prod, t)
                    if not item:
                        continue
                    res = sb.table('intelligence_items').insert(item).execute()
                    row = (res.data or [{}])[0]
                    if row.get('id'):
                        existing_urls.add(result.get('url'))
                        total_saved += 1
                        by_type[t] += 1

    return {'total_saved': total_saved, 'funding': by_type['funding'], 'trends': by_type['trend']}


def dismiss_intelligence(item_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table('intelligence_items').update({'dismissed': True}).eq('id', item_id).execute()
    return True
