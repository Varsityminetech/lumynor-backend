"""
Search engine indexer for Lumynor blog pages.
Auto-submits URLs to major crawlers on publish, and on demand.

Platforms covered:
  1. IndexNow         — Bing, Yahoo, Yandex, Ecosia, Naver, Startpage (one call)
  2. Google Indexing API — Google Search (requires service account)
  3. Google Sitemap Ping — queues sitemap re-crawl
  4. Bing Sitemap Ping   — queues sitemap re-crawl
  5. Pingler             — 50+ smaller directories / blog aggregators
  6. Ping-o-Matic        — Technorati, Weblogs.com, blog directories
"""
import os
import json
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

SITE_URL       = os.getenv("SITE_URL", "https://lumynorsystems.com").rstrip("/")
INDEXNOW_KEY   = os.getenv("INDEXNOW_KEY", "")
GOOGLE_CREDS   = os.getenv("GOOGLE_CREDENTIALS_JSON", "")  # service account JSON string

INDEXNOW_HOST  = SITE_URL.replace("https://", "").replace("http://", "")
INDEXNOW_URL   = "https://api.indexnow.org/indexnow"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _post_json(url: str, payload: dict, timeout: int = 10) -> int:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _get(url: str, timeout: int = 10) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _google_token() -> tuple:
    """Generate a short-lived OAuth2 token from the service account credentials.
    Returns (token, error_message) — error_message is None on success. The specific
    failure reason (bad JSON, malformed PEM key, OAuth rejection body, etc.) is
    returned instead of swallowed, so it's visible in the indexer status UI instead
    of only in Railway logs."""
    if not GOOGLE_CREDS:
        return None, "GOOGLE_CREDENTIALS_JSON not set"
    try:
        import base64, time
        creds = json.loads(GOOGLE_CREDS)
        private_key_pem = creds["private_key"]
        client_email    = creds["client_email"]
    except Exception as e:
        return None, f"Invalid GOOGLE_CREDENTIALS_JSON: {e}"

    try:
        now = int(time.time())
        header  = base64.urlsafe_b64encode(json.dumps({"alg":"RS256","typ":"JWT"}).encode()).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(json.dumps({
            "iss": client_email,
            "scope": "https://www.googleapis.com/auth/indexing",
            "aud": "https://oauth2.googleapis.com/token",
            "exp": now + 3600,
            "iat": now,
        }).encode()).rstrip(b"=").decode()

        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend

        key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None, backend=default_backend()
        )
        sig_input = f"{header}.{payload}".encode()
        signature = base64.urlsafe_b64encode(
            key.sign(sig_input, padding.PKCS1v15(), hashes.SHA256())
        ).rstrip(b"=").decode()

        jwt_token = f"{header}.{payload}.{signature}"
    except Exception as e:
        return None, f"Could not sign JWT (check private_key format): {e}"

    # Exchange JWT for access token
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion":  jwt_token,
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["access_token"], None
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        print(f"[indexer] Google OAuth token exchange failed: {e.code} {body}")
        return None, f"OAuth token exchange failed ({e.code}): {body}"
    except Exception as e:
        print(f"[indexer] Google token error: {e}")
        return None, str(e)[:300]


# ── Platform submissions ───────────────────────────────────────────────────────

def submit_indexnow(urls: list[str]) -> dict:
    """Submit one or more URLs to IndexNow (Bing, Yahoo, Yandex, Ecosia, Naver, Startpage)."""
    if not INDEXNOW_KEY:
        return {"status": "skipped", "reason": "INDEXNOW_KEY not set"}
    payload = {
        "host":        INDEXNOW_HOST,
        "key":         INDEXNOW_KEY,
        "keyLocation": f"{SITE_URL}/{INDEXNOW_KEY}.txt",
        "urlList":     urls,
    }
    code = _post_json(INDEXNOW_URL, payload)
    return {"status": "ok" if code in (200, 202) else "error", "http": code}


def submit_google(url: str) -> dict:
    """Submit a URL to Google's Indexing API."""
    if not GOOGLE_CREDS:
        return {"status": "skipped", "reason": "GOOGLE_CREDENTIALS_JSON not set"}
    token, token_err = _google_token()
    if not token:
        return {"status": "error", "reason": token_err or "Could not obtain Google access token"}
    data = json.dumps({"url": url, "type": "URL_UPDATED"}).encode()
    req  = urllib.request.Request(
        "https://indexing.googleapis.com/v3/urlNotifications:publish",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"status": "ok", "http": r.status}
    except urllib.error.HTTPError as e:
        # Google's error body names the real cause (e.g. "does not have permission" when
        # the service account isn't added as an Owner in Search Console) — surface it
        # instead of just the numeric code.
        body = e.read().decode(errors="replace")[:300]
        print(f"[indexer] Google Indexing API rejected {url}: {e.code} {body}")
        return {"status": "error", "http": e.code, "reason": body}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:300]}


def ping_sitemaps() -> dict:
    """Ping Google and Bing with the sitemap URL to request re-crawl."""
    sitemap = urllib.parse.quote(f"{SITE_URL}/sitemap.xml", safe="")
    results = {
        "google": _get(f"https://www.google.com/ping?sitemap={sitemap}"),
        "bing":   _get(f"https://www.bing.com/ping?sitemap={sitemap}"),
    }
    return {"status": "ok", "codes": results}


def ping_pingler(url: str, title: str = "") -> dict:
    """Pingler discontinued its free public ping API — pingler.com/ping/api/ and even
    pingler.com/api.html (their own linked docs page) now return 404. Confirmed by
    direct test; their site has moved to a paid membership model with no documented
    public endpoint. Rather than attempt a call that fails 100% of the time and shows
    a misleading permanent "error", report honestly that this platform is disabled."""
    return {"status": "skipped", "reason": "Pingler discontinued its free public ping API"}


def ping_pingomatic(url: str, title: str = "") -> dict:
    """Submit to Ping-o-Matic via XML-RPC (hits Technorati, Weblogs.com, etc.)."""
    xml_body = (
        '<?xml version="1.0"?>'
        '<methodCall><methodName>weblogUpdates.extendedPing</methodName>'
        f'<params><param><value>{title or "Lumynor Blog"}</value></param>'
        f'<param><value>{url}</value></param>'
        f'<param><value>{SITE_URL}/blog</value></param>'
        f'<param><value>{SITE_URL}/rss.xml</value></param>'
        '</params></methodCall>'
    )
    data = xml_body.encode()
    req  = urllib.request.Request(
        "https://rpc.pingomatic.com/",
        data=data,
        headers={"Content-Type": "text/xml"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"status": "ok", "http": r.status}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# ── Main orchestrator ─────────────────────────────────────────────────────────

def index_blog(blog: dict) -> dict:
    """
    Submit a blog post to all indexing platforms.
    Returns a results dict with per-platform status.
    """
    slug = blog.get("slug", "")
    url  = f"{SITE_URL}/blog/{slug}"
    title = blog.get("title", "")

    results = {
        "url":         url,
        "indexed_at":  datetime.now(timezone.utc).isoformat(),
        "indexnow":    submit_indexnow([url]),
        "google":      submit_google(url),
        "sitemap":     ping_sitemaps(),
        "pingler":     ping_pingler(url, title),
        "pingomatic":  ping_pingomatic(url, title),
    }
    print(f"[indexer] {slug} → {json.dumps({k: v.get('status') for k, v in results.items() if isinstance(v, dict)})}")
    return results


def index_blog_async(blog: dict) -> None:
    """Fire-and-forget: index in a background thread so the API response isn't held up."""
    threading.Thread(target=index_blog, args=(blog,), daemon=True).start()


def index_all_blogs(blogs: list[dict]) -> dict:
    """Bulk index all published blogs — submits URL list to IndexNow in one call."""
    urls = [f"{SITE_URL}/blog/{b['slug']}" for b in blogs if b.get("slug")]
    results = {
        "count":       len(urls),
        "indexed_at":  datetime.now(timezone.utc).isoformat(),
        "indexnow":    submit_indexnow(urls) if urls else {"status": "skipped"},
        "sitemap":     ping_sitemaps(),
    }
    # Individual Google + ping submissions for each URL
    google_results = []
    for blog in blogs:
        if blog.get("slug"):
            google_results.append(submit_google(f"{SITE_URL}/blog/{blog['slug']}"))
    results["google"] = {"submitted": len(google_results)}
    print(f"[indexer] Bulk indexed {len(urls)} URLs")
    return results


# ── Sitemap generator ─────────────────────────────────────────────────────────

STATIC_PAGES = [
    ("",         "1.0", "weekly"),
    ("blog",     "0.9", "daily"),
    # The free design-audit lead magnet — high priority: it's the main inbound
    # entry point, so it needs to be indexable and rank.
    ("audit",    "0.9", "monthly"),
    ("about",    "0.7", "monthly"),
    ("contact",  "0.7", "monthly"),
    ("products", "0.6", "monthly"),
]


def generate_sitemap(blogs: list[dict]) -> str:
    """Return a full XML sitemap string from the list of published blogs."""
    urlset = Element("urlset")
    urlset.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")
    urlset.set("xmlns:news", "http://www.google.com/schemas/sitemap-news/0.9")

    for path, priority, changefreq in STATIC_PAGES:
        loc = f"{SITE_URL}/{path}".rstrip("/")
        el  = SubElement(urlset, "url")
        SubElement(el, "loc").text        = loc
        SubElement(el, "priority").text   = priority
        SubElement(el, "changefreq").text = changefreq

    for blog in blogs:
        slug = blog.get("slug")
        if not slug:
            continue
        el = SubElement(urlset, "url")
        SubElement(el, "loc").text        = f"{SITE_URL}/blog/{slug}"
        SubElement(el, "lastmod").text    = (blog.get("updated_at") or blog.get("created_at", ""))[:10]
        SubElement(el, "priority").text   = "0.8"
        SubElement(el, "changefreq").text = "monthly"

        # Google News extension for fresh posts (<48 h old)
        try:
            pub_date = datetime.fromisoformat(blog.get("created_at", "")[:19])
            age_h    = (datetime.utcnow() - pub_date).total_seconds() / 3600
            if age_h < 48:
                news = SubElement(el, "news:news")
                pub  = SubElement(news, "news:publication")
                SubElement(pub,  "news:name").text     = "Lumynor Systems"
                SubElement(pub,  "news:language").text = "en"
                SubElement(news, "news:publication_date").text = blog.get("created_at", "")[:10]
                SubElement(news, "news:title").text    = blog.get("title", "")
        except Exception:
            pass

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(urlset, encoding="unicode")
