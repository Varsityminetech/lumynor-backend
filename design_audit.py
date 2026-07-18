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
import socket
import ipaddress
import threading
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from db import _sb, get_settings


# ── SSRF guard ────────────────────────────────────────────────────────────────
# This module fetches a URL the CALLER supplies and then has an LLM read the
# contents back to them. Exposed publicly that is a textbook SSRF: an attacker
# submits http://169.254.169.254/ (cloud metadata) or http://127.0.0.1:8000/ and
# our own server fetches internal resources and summarises them to the attacker.
#
# Checking the hostname string is NOT enough — "evil.com" can resolve to
# 127.0.0.1. So we resolve the host to its actual IP(s) and reject if ANY of them
# is private/loopback/link-local. Call this before every fetch.

class UnsafeURLError(ValueError):
    """Raised when a URL points somewhere we must never fetch (internal/private)."""


def assert_url_is_public(url: str) -> None:
    """Raise UnsafeURLError unless `url` is a public http(s) address.

    Blocks: non-http(s) schemes (file://, gopher://, etc.), and any host that
    resolves to loopback, private, link-local (incl. 169.254.169.254 metadata),
    reserved, or multicast space.
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError(f"Only http:// and https:// URLs are allowed (got '{parsed.scheme}://').")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no hostname.")

    # Resolve to real IPs — defeats DNS rebinding and hostnames that alias localhost.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise UnsafeURLError(f"Could not resolve '{host}'. Check the domain is spelled correctly.")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise UnsafeURLError(
                f"'{host}' resolves to a private/internal address ({ip}). "
                "Only public websites can be audited."
            )


# ── Concurrency cap ───────────────────────────────────────────────────────────
# Each audit spawns a headless Chromium (~300MB RAM). This backend also runs the
# blog pipeline, Lumy, the admin dashboard and several daemons on ONE Railway
# instance — so unbounded concurrent renders would OOM the container and take the
# whole production app down. Cap simultaneous renders; callers beyond the cap get
# a clean "busy" error instead of dragging the box under.

_AUDIT_SLOTS = threading.BoundedSemaphore(2)


class AuditBusyError(RuntimeError):
    """Raised when all audit slots are in use."""


# ── In-browser probes ─────────────────────────────────────────────────────────
# These run INSIDE the rendered page, so they produce measured facts rather than
# an LLM's guess. Everything here was previously either unverifiable ("requires
# visual review") or, worse, silently assumed to pass.

# WCAG contrast, computed properly: resolve each text node's effective background
# by walking ancestors until something opaque, then compute the real ratio.
# Navigation. The old `nav a` selector assumed a semantic <nav>, which is true of our
# site and false of a great many real ones — so it returned [] on sites with an obvious
# menu and the auditor called the navigation "completely missing". Try semantics first,
# then markup conventions, then pure geometry (links painted in the top strip of the
# page). `source` records which strategy won, so "we found nothing" can be told apart
# from "we looked in the wrong place".
_JS_NAV = r"""
() => {
  const visible = el => {
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) < 0.1) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  // aria-label catches icon-only links; cap the length so a whole paragraph wrapped in
  // an <a> can't masquerade as a nav item.
  const label = a => ((a.innerText || a.getAttribute('aria-label') || '').trim());
  const isNavish = t => t && t.length > 0 && t.length <= 40 && !t.includes('\n');

  const grab = sel => {
    let out = [];
    for (const a of document.querySelectorAll(sel)) {
      if (!visible(a)) continue;
      const t = label(a);
      if (isNavish(t)) out.push({ el: a, text: t });
    }
    return out;
  };

  let found = [], source = '';
  const strategies = [
    ['<nav>',            'nav a'],
    ['role=navigation',  '[role="navigation"] a'],
    ['<header>',         'header a'],
    ['class/id ~ nav',   '[class*="nav" i] a, [class*="menu" i] a, [id*="nav" i] a, [id*="header" i] a'],
  ];
  for (const [name, sel] of strategies) {
    const got = grab(sel);
    if (got.length >= 2) { found = got; source = name; break; }   // >=2: one link is a logo, not a menu
  }

  // Geometry fallback: a header built from unsemantic divs still PAINTS its menu in a
  // horizontal strip at the top of the page. That is observable regardless of markup.
  // Note this must NOT be limited to <a> — plenty of SPAs route from <button>s or
  // click-handling <div>s (powerup.money does), which is why the earlier <a>-only
  // version still came back empty on a site with an obvious menu.
  if (!found.length) {
    const got = grab('a, button, [role="link"], [role="menuitem"], [role="button"], [onclick]')
      .filter(o => {
        const r = o.el.getBoundingClientRect();
        return r.top >= 0 && r.top < 150 && r.height < 80;
      });
    if (got.length >= 2) { found = got; source = 'top-strip geometry'; }
  }

  // Active state. aria-current is the accessible way, but plenty of sites indicate the
  // current page purely visually (an underline, a dot). Not finding a marker here does
  // NOT mean there is no active state — it means we cannot see it from the DOM, and the
  // vision pass must decide. Hence active_source.
  let active = [], activeSource = '';
  const ariaOn = found.filter(o => o.el.getAttribute('aria-current') === 'page');
  if (ariaOn.length) {
    active = ariaOn.map(o => o.text);
    activeSource = 'aria-current="page"';
  } else {
    const cls = found.filter(o => {
      const c = (o.el.className || '') + ' ' + ((o.el.parentElement || {}).className || '');
      return /\b(active|current|selected|is-active)\b/i.test(String(c));
    });
    if (cls.length) {
      active = cls.map(o => o.text);
      activeSource = 'active/current class';
    }
  }

  const items = [...new Set(found.map(o => o.text))];   // dedupe desktop + hidden mobile menu
  return {
    items, count: items.length, source,
    active: [...new Set(active)], active_source: activeSource,
    detected: items.length > 0,
  };
}
"""

# Primary CTAs. The old selector was '[class*="btn-primary"]' — our own Tailwind class,
# which no other site has. Every external audit therefore found zero CTAs and reported
# "no clear primary call-to-action" about pages full of buttons. A real CTA is recognisable
# by how it is PAINTED: an actionable element with a solid fill, sitting above the fold.
_JS_CTA = r"""
() => {
  const vw = window.innerWidth, fold = window.innerHeight;
  const opaque = c => {
    const m = (c || '').match(/rgba?\(([^)]+)\)/);
    if (!m) return false;
    const p = m[1].split(',').map(s => parseFloat(s.trim()));
    return (p.length > 3 ? p[3] : 1) > 0.15;      // a real fill, not transparent
  };

  const cands = [...document.querySelectorAll(
    'button, a, [role="button"], input[type="submit"], input[type="button"]'
  )];

  const out = [];
  for (const el of cands) {
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) < 0.1) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;                 // too small to be a CTA
    if (r.top < 0 || r.top > fold) continue;                     // above the fold only
    if (r.width > vw * 0.9) continue;                            // full-width bar, not a button

    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    if (!t || t.length > 40) continue;

    // Styled like a button: a solid background, OR a visible border, OR named like one.
    const cls = String(el.className || '');
    const filled  = opaque(st.backgroundColor);
    const bordered = parseFloat(st.borderTopWidth) > 0 && opaque(st.borderTopColor);
    const named   = /\b(btn|button|cta)\b/i.test(cls);
    const isBtn   = el.tagName === 'BUTTON' || el.getAttribute('role') === 'button'
                    || el.tagName === 'INPUT';

    if (filled || bordered || named || isBtn) {
      out.push({ text: t, filled, area: r.width * r.height, top: r.top });
    }
  }

  // "Primary" = filled ones first (that is what a primary CTA looks like), then by how
  // prominent/early they are. Dedupe by label.
  out.sort((a, b) => (b.filled - a.filled) || (a.top - b.top) || (b.area - a.area));
  const seen = new Set(), primary = [];
  for (const o of out) {
    if (seen.has(o.text)) continue;
    seen.add(o.text);
    primary.push(o.text);
    if (primary.length >= 8) break;
  }
  return {
    primary,
    filled_count: out.filter(o => o.filled).length,
    source: primary.length ? 'above-fold actionable elements (fill/border/role)' : '',
  };
}
"""

# Footer. "footer a" assumes a semantic <footer>. On a site that builds its footer from
# plain divs the selector returned [] and the auditor confidently reported "footer contains
# no links, missing key navigation and contact information" — about a footer full of links.
_JS_FOOTER = r"""
() => {
  const visible = el => {
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) < 0.1) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const label = a => ((a.innerText || a.getAttribute('aria-label') || '').trim());
  const ok = t => t && t.length > 0 && t.length <= 60;

  const grab = sel => [...document.querySelectorAll(sel)]
    .filter(visible).map(label).filter(ok);

  for (const [name, sel] of [
    ['<footer>',        'footer a'],
    ['role=contentinfo','[role="contentinfo"] a'],
    ['class/id ~ footer','[class*="footer" i] a, [id*="footer" i] a'],
  ]) {
    const got = grab(sel);
    if (got.length) return { items: [...new Set(got)], source: name };
  }

  // Geometry: whatever the markup, a footer is painted at the bottom of the document.
  const docH = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
  const cut  = docH * 0.8;
  const got = [...document.querySelectorAll('a')].filter(a => {
    if (!visible(a)) return false;
    const r = a.getBoundingClientRect();
    return (r.top + window.scrollY) >= cut;      // absolute position, not viewport-relative
  }).map(label).filter(ok);

  return got.length
    ? { items: [...new Set(got)], source: 'bottom-of-document geometry' }
    : { items: [], source: '' };
}
"""

_JS_CONTRAST = r"""
() => {
  const lum = (r, g, b) => {
    const a = [r, g, b].map(v => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
  };
  const parse = c => {
    const m = (c || '').match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(s => parseFloat(s.trim()));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const bgOf = el => {                       // first non-transparent ancestor bg
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.1) return c;
      n = n.parentElement;
    }
    const b = parse(getComputedStyle(document.body).backgroundColor);
    return b && b.a > 0.1 ? b : { r: 255, g: 255, b: 255, a: 1 };
  };

  const fails = [];
  let checked = 0;
  let unmeasurable = 0;        // gradient/clipped text — real, but not measurable from `color`
  const els = document.querySelectorAll('p,span,a,li,h1,h2,h3,h4,h5,h6,button,label,td,div');
  for (const el of els) {
    if (el.children.length > 0) continue;                 // leaf text only
    // WCAG 1.4.3 exempts purely decorative text from contrast requirements, and both
    // Lighthouse and axe skip aria-hidden subtrees for this reason. We were flagging
    // decorative glyphs (e.g. a faint "< / >" ornament) as CRITICAL contrast failures.
    if (el.closest('[aria-hidden="true"]')) continue;
    const txt = (el.innerText || '').trim();
    if (txt.length < 3) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) < 0.1) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;

    const fgRaw = parse(st.color);
    if (!fgRaw) continue;

    // Gradient text (background-clip:text + transparent color) is painted by a
    // background-image, NOT by `color` — the computed color comes back
    // rgba(0,0,0,0), which naively scores ~1:1 and fabricates a critical
    // "invisible text" finding on a heading that is perfectly readable. Contrast
    // genuinely is not computable from `color` here, so don't guess: skip it and
    // let the vision pass (which can actually see the glyphs) judge it.
    const clip  = st.webkitBackgroundClip || st.backgroundClip || '';
    const fillA = parse(st.webkitTextFillColor || '');
    if (fgRaw.a < 0.1 || clip.includes('text') || (fillA && fillA.a < 0.1)) {
      unmeasurable++;
      continue;
    }

    const bg = bgOf(el);

    // Semi-transparent text (Tailwind's text-white/70 and friends, used heavily)
    // actually paints as a blend over its backdrop. Scoring the raw colour treats
    // it as fully opaque and OVERSTATES contrast — hiding real failures. Composite
    // it over the resolved background first.
    const a  = fgRaw.a;
    const fg = a < 1
      ? { r: fgRaw.r * a + bg.r * (1 - a),
          g: fgRaw.g * a + bg.g * (1 - a),
          b: fgRaw.b * a + bg.b * (1 - a) }
      : fgRaw;

    const L1 = lum(fg.r, fg.g, fg.b), L2 = lum(bg.r, bg.g, bg.b);
    const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);

    const size = parseFloat(st.fontSize);
    const bold = (parseInt(st.fontWeight) || 400) >= 700;
    const isLarge = size >= 24 || (size >= 18.66 && bold);
    const required = isLarge ? 3.0 : 4.5;                 // WCAG 2.1 AA

    checked++;
    if (ratio < required) {
      fails.push({
        text: txt.slice(0, 55),
        ratio: Math.round(ratio * 100) / 100,
        required,
        font_px: Math.round(size),
        color: st.color,
        background: `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`,
      });
    }
  }
  fails.sort((a, b) => a.ratio - b.ratio);                // worst first
  return { checked, unmeasurable, fail_count: fails.length, failures: fails.slice(0, 8) };
}
"""

# Trust / social-proof signals — previously invisible to the auditor.
_JS_TRUST = r"""
() => {
  const body = (document.body.innerText || '').toLowerCase();
  const has = re => re.test(body);
  const imgs = [...document.querySelectorAll('img')];
  const alts = imgs.map(i => (i.alt || '').toLowerCase()).join(' ');
  const hrefs = [...document.querySelectorAll('a')].map(a => (a.getAttribute('href') || '').toLowerCase());

  return {
    testimonials:   has(/testimonial|what our (clients|customers)|client(s)? say|review(s)?\b|"\s*[A-Z]/),
    case_studies:   has(/case stud(y|ies)|success story|portfolio|our work/),
    client_logos:   /logo|client|partner|brand|trusted by/.test(alts) || has(/trusted by|as seen (in|on)|our clients|partners/),
    social_proof_numbers: has(/\d+\+?\s*(clients|customers|users|projects|businesses|companies)|\d+\s*(years|yrs)\b/),
    team_or_about:  hrefs.some(h => /about|team|who-we-are/.test(h)),
    contact_email:  has(/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/),
    contact_phone:  has(/(\+\d{1,3}[\s-]?)?\(?\d{3,5}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}/),
    physical_address: has(/\b(street|road|avenue|suite|floor|city|pin ?code|zip)\b/),
    privacy_policy: hrefs.some(h => /privacy/.test(h)),
    terms:          hrefs.some(h => /terms|t-and-c|tnc/.test(h)),
    social_links:   hrefs.filter(h => /linkedin|twitter|x\.com|instagram|facebook|youtube/.test(h)).length,
    guarantee_or_refund: has(/money[- ]back|guarantee|refund|no obligation|free trial/),
  };
}
"""

# Mobile reality check — the claim we could not previously back at all.
_JS_MOBILE = r"""
() => {
  const vpMeta = document.querySelector('meta[name="viewport"]');
  const vw = window.innerWidth;

  // Horizontal overflow => the page does not fit the phone (classic broken responsive)
  const docW = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
  const overflowPx = Math.max(0, Math.round(docW - vw));

  // Which elements actually stick out past the right edge?
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.right > vw + 2) {
      const tag = el.tagName.toLowerCase();
      const txt = (el.innerText || '').trim().slice(0, 40);
      offenders.push({ tag, overflow_px: Math.round(r.right - vw), text: txt });
      if (offenders.length >= 6) break;
    }
  }

  // Touch targets: WCAG/Apple guidance is 44x44 CSS px minimum
  const small = [];
  const tappable = document.querySelectorAll('a, button, input, select, textarea, [role="button"]');
  let tapChecked = 0;
  for (const el of tappable) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    tapChecked++;
    if (r.width < 44 || r.height < 44) {
      small.push({
        label: ((el.innerText || el.getAttribute('aria-label') || el.tagName) + '').trim().slice(0, 35),
        w: Math.round(r.width),
        h: Math.round(r.height),
      });
    }
  }

  // Text too small to read comfortably on a phone
  let tiny = 0, textChecked = 0;
  for (const el of document.querySelectorAll('p, span, li, a, div')) {
    if (el.children.length) continue;
    const t = (el.innerText || '').trim();
    if (t.length < 8) continue;
    const size = parseFloat(getComputedStyle(el).fontSize);
    textChecked++;
    if (size && size < 12) tiny++;
  }

  return {
    viewport_meta: vpMeta ? (vpMeta.getAttribute('content') || '') : null,
    viewport_width: vw,
    horizontal_overflow_px: overflowPx,
    overflow_offenders: offenders,
    tap_targets_checked: tapChecked,
    tap_targets_too_small: small.length,
    tap_target_examples: small.slice(0, 6),
    text_nodes_checked: textChecked,
    text_below_12px: tiny,
  };
}
"""


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
        "active_nav_links": [],
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

                # Navigation. `nav a` alone only works on sites that use a semantic
                # <nav> — i.e. ours. On a site whose header is plain divs it matched
                # nothing, and the auditor then reported "navigation links are
                # completely missing" about a page with a perfectly visible menu.
                # _JS_NAV falls back through several strategies and, crucially, tells
                # us WHICH one worked so absence can be distinguished from
                # not-looking-in-the-right-place.
                try:
                    nav = page.evaluate(_JS_NAV)
                except Exception as e:
                    print(f"[design_audit] nav probe failed: {e}")
                    nav = {}
                result["nav_links"]        = nav.get("items", [])
                result["nav_source"]       = nav.get("source", "")
                result["active_nav_links"] = nav.get("active", [])
                result["active_nav_source"] = nav.get("active_source", "")

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
                # Footer. "footer a" assumes a semantic <footer> — same trap as the nav.
                # On powerup.money this returned [] and the auditor reported "footer contains
                # no links, missing key navigation and contact information" about a footer
                # that has them. Fall through markup conventions, then geometry (links in the
                # bottom fifth of the document).
                try:
                    foot = page.evaluate(_JS_FOOTER)
                except Exception as e:
                    print(f"[design_audit] footer probe failed: {e}")
                    foot = {}
                result["footer_links"]  = foot.get("items", [])
                result["footer_source"] = foot.get("source", "")

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

                # Primary CTAs. The old selector was '[class*="btn-primary"]' — OUR OWN
                # Tailwind class. No other site on earth has it, so every external audit
                # came back with zero CTAs and the model duly reported "no clear primary
                # call-to-action" about pages covered in buttons. _JS_CTA identifies real
                # actions by how they're PAINTED (solid fill, above the fold), not by our
                # class names.
                try:
                    cta = page.evaluate(_JS_CTA)
                except Exception as e:
                    print(f"[design_audit] cta probe failed: {e}")
                    cta = {}
                result["primary_ctas"] = cta.get("primary", [])
                result["cta_source"]   = cta.get("source", "")

                # Target audience statement. aria-label="target-audience" is a convention we
                # invented for OUR site so this check would pass; no external site uses it.
                # Keep reading it (it's authoritative when present) but never treat its
                # absence as a failure — see the INCONCLUSIVE handling in _build_context.
                audience_el = page.query_selector('[aria-label="target-audience"]')
                if audience_el:
                    result["audience_statement"] = audience_el.text_content().strip()

                # Hero text. The first <section> works on our site; a great many sites use
                # <div>/<main>. Fall through, and last-resort take the top of the body so
                # the hero is never empty on a page that obviously has one.
                for _sel in ("section", "main > div", "header + div", "main", "body"):
                    _el = page.query_selector(_sel)
                    if _el:
                        _txt = " ".join(_el.inner_text().split())
                        if _txt:
                            result["hero_text"] = _txt[:600]
                            result["hero_source"] = _sel
                            break

                # Body text (rendered)
                body_text = " ".join(page.inner_text("body").split())
                result["body_text"] = body_text[:2000]

                # ── Contrast, measured — not guessed ─────────────────────────
                # Colour contrast was previously an "LLM eyeballs it" item, which it
                # cannot do from DOM text. Compute the real WCAG ratio in-browser:
                # deterministic, and impossible to hallucinate.
                try:
                    result["contrast"] = page.evaluate(_JS_CONTRAST)
                except Exception as e:
                    print(f"[design_audit] contrast probe failed: {e}")

                # ── Trust signals ────────────────────────────────────────────
                try:
                    result["trust"] = page.evaluate(_JS_TRUST)
                except Exception as e:
                    print(f"[design_audit] trust probe failed: {e}")

                # ── Desktop screenshot (for the vision pass) ─────────────────
                try:
                    import base64 as _b64
                    shot = page.screenshot(type="jpeg", quality=60, full_page=False)
                    result["screenshot_desktop"] = _b64.b64encode(shot).decode()
                except Exception as e:
                    print(f"[design_audit] desktop screenshot failed: {e}")

                # ── MOBILE PASS — the biggest gap ────────────────────────────
                # The audit claims to find "mobile gaps" but only ever rendered at
                # 1440x900 desktop, so every mobile checkpoint (touch targets,
                # responsive layout, hover-only elements) had NO data behind it.
                # Render again on a real phone viewport and measure properly.
                try:
                    m_ctx = browser.new_context(
                        viewport={"width": 390, "height": 844},          # iPhone 14-class
                        device_scale_factor=3,
                        is_mobile=True,
                        has_touch=True,
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                                   "Mobile/15E148 Safari/604.1",
                    )
                    m_page = m_ctx.new_page()
                    m_page.goto(url, wait_until="networkidle", timeout=30_000)
                    m_page.wait_for_timeout(2_000)
                    result["mobile"] = m_page.evaluate(_JS_MOBILE)
                    try:
                        import base64 as _b64m
                        mshot = m_page.screenshot(type="jpeg", quality=60, full_page=False)
                        result["screenshot_mobile"] = _b64m.b64encode(mshot).decode()
                    except Exception:
                        pass
                    m_ctx.close()
                except Exception as e:
                    print(f"[design_audit] mobile pass failed: {e}")
                    result["mobile"] = {"error": str(e)[:200]}

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
        "active_nav_links": [],
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
            # Same trap as the Playwright path: "nav a" only matches a semantic <nav>.
            # Fall through the common header conventions before concluding there is none.
            for _sel in ('nav a', '[role="navigation"] a', 'header a'):
                _found = [a.get_text(strip=True) for a in soup.select(_sel) if a.get_text(strip=True)]
                if len(_found) >= 2:                      # one link is a logo, not a menu
                    result["nav_links"]  = list(dict.fromkeys(_found))
                    result["nav_source"] = _sel
                    break
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
    Call Google PageSpeed Insights API (mobile strategy).
    Returns real Core Web Vitals and Lighthouse scores.

    Unauthenticated calls are aggressively rate-limited — we were getting 429s, which
    surfaced in reports as TWO HIGH ISSUES against the audited site ("PageSpeed API
    returned 429 — cannot verify"). That is our infrastructure failing, not a defect in
    anyone's website. Send a key when we have one, and back off on 429/5xx.
    """
    import os as _os, time as _time
    key = _os.getenv("PAGESPEED_API_KEY") or _os.getenv("GOOGLE_API_KEY") or ""
    api = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url={url}&strategy=mobile"
    if key:
        api += f"&key={key}"
    try:
        resp = None
        for attempt in range(3):
            resp = requests.get(api, timeout=45)
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 503) and attempt < 2:
                _time.sleep((2 ** attempt) * 5)          # 5s, 10s
                continue
            break
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "no response"
            hint = " (no PAGESPEED_API_KEY set — unauthenticated quota is very low)" if not key else ""
            return {"error": f"PageSpeed API returned {code}{hint}"}

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

def _visual_review(page_data: dict, gemini_key: str) -> dict | None:
    """Actually LOOK at the page.

    Until now the auditor never saw the site — it read extracted DOM text, so
    "does this look good / feel crowded / read as premium" was structurally
    unanswerable and the prompt correctly punted with "requires visual review".
    Gemini 2.5 Flash is multimodal, so we send it the real desktop + mobile
    screenshots and ask ONLY for the judgements that genuinely need eyes.

    Returns None if unavailable — the audit degrades gracefully rather than
    inventing visual findings.
    """
    def _skip(reason: str) -> None:
        """Record WHY there is no visual verdict, on the page_data we were handed.

        The caller can't reconstruct this, and a silently-missing vision pass is
        exactly the bug this function shipped with: the report kept claiming
        'we looked at your page' while nothing had looked at anything.
        """
        page_data["visual_skipped"] = reason
        print(f"[design_audit] vision SKIPPED: {reason}")
        return None

    shot_d = page_data.get("screenshot_desktop")
    shot_m = page_data.get("screenshot_mobile")
    if not shot_d:
        return _skip("no desktop screenshot was captured")
    if not gemini_key:
        return _skip("no Gemini key — vision needs Gemini even when the main LLM is "
                     "Ollama; set GEMINI_API_KEY in the environment")

    parts = [{
        "text": (
            "You are a senior visual/UX designer. You are shown a real screenshot of a "
            "website's above-the-fold view (desktop first, mobile second if present).\n\n"
            "Judge ONLY what you can actually SEE. Do not speculate about behaviour, "
            "code, or anything below the fold.\n\n"
            "Assess:\n"
            "1. visual_hierarchy — does the eye land on the right thing first? Is the "
            "primary action obvious?\n"
            "2. crowding — is it visually cluttered, or is whitespace used deliberately?\n"
            "3. design_quality — does it look credible, current, and professionally made, "
            "or dated/template-y/untrustworthy?\n"
            "4. mobile_layout — if a mobile shot is shown: is anything cramped, cut off, "
            "overlapping, or unreadable?\n"
            "5. first_impression — in one sentence, what would a first-time visitor think?\n"
            "6. nav_visible — is there a navigation menu visible in the header? true/false. "
            "Our DOM probe misses menus built without semantic markup, so YOU are the "
            "authority on whether one exists. If you can see it, it exists.\n"
            "7. nav_labels — the menu item labels you can actually read, as a list "
            "(empty list if no menu is visible).\n"
            "8. active_nav_indicated — is the CURRENT page marked in the menu by any visual "
            "means at all (underline, dot, highlight, bolder or brighter text)? true/false. "
            "Many sites do this with pure styling and no DOM attribute.\n\n"
            "Be concrete and honest. If something looks genuinely good, say so.\n\n"
            'Return ONLY JSON: {"visual_hierarchy":"...","crowding":"...",'
            '"design_quality":"...","mobile_layout":"...","first_impression":"...",'
            '"nav_visible":true,"nav_labels":["..."],"active_nav_indicated":true,'
            '"visual_issues":[{"issue":"...","severity":"critical|high|medium","fix":"..."}]}'
        )
    }]
    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": shot_d}})
    if shot_m:
        parts.append({"text": "Above: desktop. Below: the SAME page on a 390px phone."})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": shot_m}})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.3,                      # low — this is assessment, not prose
            # gemini-2.5-flash is a THINKING model: its reasoning tokens are billed
            # against maxOutputTokens. Reasoning over a screenshot burned the entire
            # old 1200-token budget, so the API returned finishReason=MAX_TOKENS with a
            # `content` that has no `parts` at all — the parse below raised KeyError, it
            # was swallowed, and vision silently never ran. Thinking buys nothing on a
            # "describe what you see" task, so switch it off and give the budget to the
            # answer (8192 matches _gemini_generate in auto_blogger.py).
            "thinkingConfig": {"thinkingBudget": 0},
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }
    api = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash:generateContent?key={gemini_key}")
    try:
        r = requests.post(api, json=payload, timeout=90)
        if not r.ok:
            return _skip(f"vision API returned {r.status_code}: {r.text[:160]}")

        body = r.json()
        cands = body.get("candidates") or []
        if not cands:
            fb = (body.get("promptFeedback") or {}).get("blockReason", "no candidates")
            return _skip(f"vision API returned no candidates ({fb})")

        cand   = cands[0]
        pieces = (cand.get("content") or {}).get("parts") or []
        txt    = "".join(p.get("text", "") for p in pieces).strip()
        if not txt:
            # Don't guess — say exactly why the model gave us nothing.
            return _skip(f"vision model returned no text (finishReason="
                         f"{cand.get('finishReason', 'unknown')})")

        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return _skip(f"vision model returned non-JSON: {txt[:120]}")
        return json.loads(m.group())
    except Exception as e:
        return _skip(f"vision error: {type(e).__name__}: {e}")


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
            nav_count = len(page['nav_links'])
            if nav_count:
                nav_verdict = ("PASSES C2-01 (within Miller's 7±2 rule)" if nav_count <= 7
                               else f"FAILS C2-01 ({nav_count} > 7)")
                lines.append(
                    f"Navigation links (found via {page.get('nav_source') or 'DOM'}, deduplicated — "
                    f"{nav_count} items — {nav_verdict}): {page['nav_links']}"
                )
            else:
                # An empty result here means OUR PROBE found nothing — not that the site
                # has no menu. We previously printed an empty list and the model turned it
                # into a confident "navigation links are completely missing" on sites whose
                # menu is plainly visible in the screenshot. Never let a detection miss
                # become a finding.
                lines.append(
                    "★ NAVIGATION: our DOM probe could not identify a nav menu on this page. "
                    "This is INCONCLUSIVE, NOT a finding — many sites build headers without "
                    "semantic markup. You MUST NOT claim navigation is missing/absent. If the "
                    "VISUAL REVIEW shows a menu, the navigation exists and C2-01/C2-02 PASS. "
                    "Only if the visual review ALSO shows no menu may you raise it, and then "
                    "only as a medium issue."
                )

            # Active nav state
            if page.get("active_nav_links"):
                lines.append(
                    f"★ ACTIVE NAV LINKS (via {page.get('active_nav_source') or 'DOM'} — "
                    f"active state IS implemented): {page['active_nav_links']}"
                )
                lines.append("  → C2-03 PASSES: at least one nav link shows the current page state.")
            else:
                # Same trap: an active page is very often marked with a purely visual cue —
                # an underline, a dot, a colour change — that carries no DOM signal at all.
                lines.append(
                    "★ ACTIVE NAV STATE: no DOM marker (aria-current / .active class) found. "
                    "This is INCONCLUSIVE, NOT a finding — active state is frequently indicated "
                    "visually only (underline, dot, colour). Do NOT fail C2-03 on this alone. "
                    "Defer to the VISUAL REVIEW; if it shows no current-page indication, raise "
                    "C2-03 as MEDIUM at most."
                )
            # Target audience statement (aria-label="target-audience")
            if page.get("audience_statement"):
                lines.append(f"★ TARGET AUDIENCE STATEMENT (explicit element with aria-label='target-audience', visible above H1): \"{page['audience_statement']}\"")
                lines.append("  → C6-04 PASSES: the page explicitly answers 'Who is this for?' via this element.")
            else:
                # aria-label="target-audience" is a convention WE invented for our own site.
                # Its absence tells you nothing about anyone else's site — judge C6-04 from
                # the hero copy, which is where every normal site states who it's for.
                lines.append(
                    "★ TARGET AUDIENCE: no explicitly-tagged audience element (this tag is a "
                    "Lumynor-only convention — its absence is EXPECTED and MEANINGLESS on any "
                    "other site). Judge C6-04 from the HERO SECTION TEXT and the visual review: "
                    "if the hero makes clear who the product is for, C6-04 PASSES. Never fail "
                    "C6-04 merely because this tag is absent."
                )

            # Primary CTAs are the most important conversion elements
            if page.get("primary_ctas"):
                lines.append(
                    f"★ PRIMARY CTA ELEMENTS (detected via {page.get('cta_source') or 'DOM'} — "
                    f"the main conversion actions visible above the fold): {page['primary_ctas']}"
                )
                lines.append("  → C1-02 PASSES: a primary CTA is present and visible above the fold.")
                lines.append("  → C6-01 STATUS: one dominant CTA plus a subtle secondary link is standard UX — this is NOT three competing conversion goals.")
            else:
                lines.append(
                    "★ PRIMARY CTA: our probe found no clearly-styled action above the fold. "
                    "This is INCONCLUSIVE, NOT a finding. Cross-check against 'Button texts' "
                    "below and the VISUAL REVIEW — if either shows an obvious action (Get "
                    "Started, Sign Up, Download…), C1-02 PASSES. Only claim a missing CTA if "
                    "the screenshot genuinely shows none."
                )
            lines.append(f"Button texts (all buttons): {page['button_texts']}")
            lines.append(f"Link texts (sample): {page['link_texts'][:20]}")
            if page.get("footer_links"):
                lines.append(
                    f"Footer links (via {page.get('footer_source') or 'DOM'}): {page['footer_links']}"
                )
            else:
                lines.append(
                    "★ FOOTER: our probe found no footer links. This is INCONCLUSIVE, NOT a "
                    "finding — do NOT claim the footer is empty or missing links. Footers are "
                    "often below the fold and outside what we captured."
                )
            lines.append(f"Images: {page['images_total']} total, {page['images_missing_alt']} missing alt attribute")
            lines.append(f"External links without target=_blank: {page['external_links_no_target']}")
            lines.append(f"Form input fields: {page['form_fields']}")
            if page.get("hero_text"):
                lines.append(f"★ HERO SECTION TEXT (above-fold content — first section rendered): {page['hero_text']}")
                if page.get("h1s"):
                    lines.append("  → C1-01 PASSES: hero section contains a clear H1 and value proposition. Visual layout/density assessment requires visual review — cannot be determined from extracted text.")
            if page["body_text"]:
                lines.append(f"Body text sample:\n{page['body_text']}")

    lines.append("")
    lines.append("=== GOOGLE PAGESPEED INSIGHTS (Mobile) ===")
    if "error" in pagespeed:
        lines.append(f"⚠ PageSpeed API error: {pagespeed['error']}")
        lines.append(
            "  This is OUR tool failing to fetch data — it is NOT a defect in the audited "
            "website. Do NOT invent performance numbers, and do NOT raise C5-01/C5-02 as "
            "critical, high, or medium issues: the site is not at fault for our API quota. "
            "OMIT them from the issue lists entirely and simply note in the executive summary "
            "that load performance could not be measured on this run."
        )
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

    # ── MEASURED MOBILE (real 390x844 phone render — not inferred) ────────────
    mob = page.get("mobile") or {}
    if mob and not mob.get("error"):
        lines.append("\n★ MOBILE — MEASURED ON A REAL 390px PHONE VIEWPORT (authoritative)")
        vpm = mob.get("viewport_meta")
        lines.append(f"viewport meta tag: {vpm if vpm else 'MISSING — page will not scale on phones'}")
        ov = mob.get("horizontal_overflow_px", 0)
        if ov > 0:
            lines.append(f"HORIZONTAL OVERFLOW: content is {ov}px wider than the screen — the page does NOT fit; users must pinch/scroll sideways. THIS IS A REAL, MEASURED FAILURE.")
            for o in (mob.get("overflow_offenders") or [])[:4]:
                lines.append(f"  · <{o['tag']}> sticks out {o['overflow_px']}px — \"{o.get('text','')}\"")
        else:
            lines.append("horizontal overflow: none — layout fits the phone screen correctly")
        tt_small = mob.get("tap_targets_too_small", 0)
        tt_all   = mob.get("tap_targets_checked", 0)
        if tt_all:
            if tt_small:
                lines.append(f"TAP TARGETS: {tt_small} of {tt_all} are under the 44x44px minimum — measured:")
                for t in (mob.get("tap_target_examples") or [])[:5]:
                    lines.append(f"  · \"{t['label']}\" is only {t['w']}x{t['h']}px")
            else:
                lines.append(f"tap targets: all {tt_all} meet the 44x44px minimum")
        tiny = mob.get("text_below_12px", 0)
        if tiny:
            lines.append(f"TEXT SIZE: {tiny} text elements render below 12px on mobile — hard to read.")
        else:
            lines.append("text size: no text below 12px on mobile")

    # ── MEASURED CONTRAST (real WCAG ratios — not eyeballed) ──────────────────
    con = page.get("contrast") or {}
    if con.get("checked"):
        lines.append("\n★ COLOUR CONTRAST — COMPUTED WCAG 2.1 AA RATIOS (authoritative, do NOT guess)")
        if con.get("fail_count"):
            lines.append(f"{con['fail_count']} of {con['checked']} text elements FAIL the required ratio. Worst offenders:")
            for f in con.get("failures", [])[:6]:
                lines.append(
                    f"  · \"{f['text']}\" — {f['ratio']}:1 (needs {f['required']}:1) "
                    f"at {f['font_px']}px, {f['color']} on {f['background']}"
                )
        else:
            lines.append(f"All {con['checked']} text elements PASS WCAG AA contrast. C1-06 PASSES.")
        if con.get("unmeasurable"):
            # Be explicit that these were EXCLUDED, not passed — otherwise the model
            # reads "all N pass" as covering every word on the page.
            lines.append(
                f"({con['unmeasurable']} more elements use gradient/clipped text, where contrast "
                f"cannot be computed from the CSS colour. They were EXCLUDED from the counts above — "
                f"do NOT report them as failures, and do NOT claim they passed. If the visual review "
                f"flags them as hard to read, use that instead.)"
            )

    # ── TRUST SIGNALS (detected in the rendered page) ─────────────────────────
    tr = page.get("trust") or {}
    if tr:
        lines.append("\n★ TRUST & SOCIAL-PROOF SIGNALS (detected in the live page)")
        present = [k for k, v in tr.items() if v]
        missing = [k for k, v in tr.items() if not v and k != "social_links"]
        lines.append(f"present: {', '.join(present) if present else 'NONE'}")
        lines.append(f"absent:  {', '.join(missing) if missing else 'none'}")
        lines.append(f"social profile links found: {tr.get('social_links', 0)}")

    # ── VISUAL REVIEW (a vision model actually LOOKED at the page) ────────────
    vis = page.get("visual") or {}
    if vis:
        lines.append("\n★ VISUAL REVIEW — a vision model SAW the rendered page (desktop + mobile screenshots)")
        lines.append("These are eyes-on judgements. Treat them as observed fact, not speculation.")
        for k in ("first_impression", "visual_hierarchy", "crowding", "design_quality", "mobile_layout"):
            if vis.get(k):
                lines.append(f"{k}: {vis[k]}")

        # The vision model's word on navigation OVERRIDES the DOM probe. A menu it can
        # see is a menu that exists, whatever the markup looks like — this is what stops
        # us telling someone their navigation is missing while it sits in their screenshot.
        if vis.get("nav_visible"):
            labels = vis.get("nav_labels") or []
            lines.append(
                f"→ NAVIGATION IS VISIBLE in the screenshot{f' — labels seen: {labels}' if labels else ''}. "
                "The site HAS navigation. C2-01/C2-02 PASS. You MUST NOT report navigation as "
                "missing or absent, regardless of what the DOM probe found."
            )
        elif vis.get("nav_visible") is False:
            lines.append(
                "→ No navigation menu is visible in the screenshot either. Combined with the DOM "
                "probe, missing navigation is now a supportable finding."
            )
        if vis.get("active_nav_indicated"):
            lines.append(
                "→ The CURRENT PAGE IS VISUALLY INDICATED in the nav (underline/dot/highlight). "
                "C2-03 PASSES — do NOT report a missing active state."
            )

        for vi in (vis.get("visual_issues") or [])[:6]:
            lines.append(f"  · [{vi.get('severity','medium')}] {vi.get('issue','')} → fix: {vi.get('fix','')}")

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
2b. ABSENCE OF DETECTION IS NOT EVIDENCE OF ABSENCE. This is the single most damaging mistake you can make. An empty field, a zero count, or a missing block means OUR PROBE DID NOT FIND IT — it does NOT mean the site lacks the feature. Our probes look for specific markup, and a huge number of real sites are built differently (no semantic <nav>, no aria-current, visual-only state, styling we don't recognise). NEVER write "X is completely missing", "the site has no X", or "X is absent" on the strength of an empty field alone. Any block marked INCONCLUSIVE must be treated as unknown, not as failed. Where a VISUAL REVIEW block exists, it outranks every DOM probe: if the vision model can SEE the thing, the thing exists and the check PASSES, no matter what the DOM said. Reporting a feature as missing when a visitor can plainly see it on their own screen destroys all trust in this audit.
3. For checklist items that CAN be verified from fetched data (nav labels, headings, image alt, HTTPS, footer links, PageSpeed scores), your finding MUST match the data exactly.
4. VISUAL ITEMS ARE NOW MEASURED — do NOT punt on them.
   · Colour contrast (C1-06): the "★ COLOUR CONTRAST" block gives COMPUTED WCAG ratios. Use them verbatim. If it reports failures, C1-06 FAILS and you must quote the real ratio (e.g. "2.9:1, needs 4.5:1"). If it says all pass, C1-06 PASSES. NEVER write "requires visual review" for contrast — we measured it.
   · Crowding / hierarchy / design quality (C1-03, C1-07): the "★ VISUAL REVIEW" block is a vision model that actually SAW the page. Treat it as observed fact and cite it. Only fall back to "requires visual review" if that block is entirely absent.
   · Animation quality (C4-03) still cannot be judged from a still frame — that one may remain unverifiable.
5. For performance items: USE the PageSpeed scores provided. NEVER invent LCP/CLS values. If PageSpeed ERRORED (429, timeout, quota), that is OUR tool failing — NOT a fault of the audited site. NEVER penalise a site for our own failed API call: OMIT C5-01/C5-02 from critical_issues, high_issues AND medium_issues entirely, and just note in the executive summary that load performance could not be measured. A site must never lose points for our infrastructure.
5b. NEVER report a limitation of THIS TOOL as a defect of the website. "Cannot verify X", "API returned an error", "requires interactive testing" describe US, not them. Such statements are not findings and must not appear in critical_issues or high_issues.
6. Overall score: start at 10. Deduct 1 per Critical fail (evidence-based only). Deduct 0.5 per High fail (evidence-based only). Items that cannot be verified from a static render do NOT cause deductions and must NOT appear in critical_issues or high_issues.
7. Every finding must include: a principle citation AND a specific, actionable fix. Generic fixes are not acceptable.
8. PRIMARY CTA (C1-02): The "★ PRIMARY CTA ELEMENTS" field lists actions detected above the fold. If it lists items, the CTA IS present and visible — do NOT flag C1-02 as failing. If the block instead says INCONCLUSIVE, do NOT conclude the site has no CTA: check the "Button texts" list and the visual review first. A page showing "Get Started" / "Sign Up" / "Book a Demo" HAS a CTA no matter what our probe returned.
9. NAVIGATION COUNT (C2-01): The nav_links field is already deduplicated. Count only the unique items shown. Do NOT report duplicates unless the exact same label appears twice in the deduplicated list.
10. HERO AND AUDIENCE (C1-01, C6-04): If "★ TARGET AUDIENCE STATEMENT" field exists, C6-04 PASSES — NEVER mark it failing when this field is present. Its ABSENCE means nothing: that tag is a Lumynor-only convention, so on any other site judge C6-04 from the hero copy and the visual review — if the hero makes clear who the product serves, C6-04 PASSES. If H1 and hero_text are present with a value proposition, C1-01 passes.
11. ACTIVE NAV STATE (C2-03): If "★ ACTIVE NAV LINKS" field is non-empty, C2-03 PASSES — the active state system is implemented. Do NOT flag C2-03 as failing when active_nav_links contains items. If the block says INCONCLUSIVE, obey it: an active page is very often marked with a purely visual cue (underline, dot, colour) that leaves no DOM trace, and a small cue is easy for a vision model to miss. C2-03 may therefore NEVER exceed medium_issues, and you must NEVER phrase it as "no DOM-based active state detected" — that describes OUR probe, not their site (see rule 5b). Also: on a single-page site with no current section, an active state is not even applicable — in that case C2-03 PASSES.
12. UNVERIFIABLE ITEMS — PLACEMENT RULE: A finding that genuinely cannot be verified goes in medium_issues ONLY, never critical/high. This now applies ONLY to: C4-01 (hover/active states), C4-03 (animation quality), C4-07 (loading indicators). It does NOT apply to mobile layout, tap targets, text size, or colour contrast — those are now MEASURED on a real device viewport and with computed WCAG ratios, so they are fully verifiable and MUST be scored normally (critical/high where the data shows a real failure).
13. HOVER STATES (C4-01): CSS :hover and :active are invisible to static renders. If you cannot verify, put ONLY in medium_issues with note "Requires interactive testing." NEVER in high_issues.
14. LOADING INDICATORS (C4-07): Loading spinners only appear after form submission. NEVER flag C4-07 in high_issues from a static render. Put ONLY in medium_issues if you cannot verify.
15. NAV ITEM COUNT (C2-01): Miller's Law says 7 ± 2 items. A nav with ≤ 7 items IS within the law and PASSES C2-01. The context block already pre-calculates this verdict. NEVER flag a nav with 5, 6, or 7 items as "could be streamlined" in high_issues — that is NOT a failure. Only flag C2-01 as failing when nav_links count exceeds 7.
15b. NAV JARGON (C2-02): "Jargon" means a label a visitor CANNOT UNDERSTAND — internal team names, acronyms, or system terms ("Synergy Hub", "CRM", "Tier 2 Assets"). It does NOT mean:
   · Standard web conventions. "Contact Us", "About", "Blog", "Products", "Pricing", "Work", "Login" are the plainest labels in existence — NEVER flag these. Flagging "Contact Us" as jargon is absurd and destroys trust in the audit.
   · PRODUCT AND BRAND NAMES. Companies legitimately put their product names in the nav (Notion, Figma, Slack all do). A named product in the nav is a deliberate branding decision, not a usability defect. NEVER recommend renaming a product to a generic description.
   Only fail C2-02 when a label would genuinely leave a first-time visitor unable to guess where it leads. If in doubt, C2-02 PASSES.
16. PRIMARY CTA VISIBILITY (C1-02): C1-02 asks ONLY: is there a primary CTA visible above the fold? If "★ PRIMARY CTA ELEMENTS" is non-empty, the answer is YES — C1-02 PASSES. A subtle secondary text link ("or chat on WhatsApp") next to the primary button does NOT make C1-02 fail. C1-02 is about CTA presence and visibility, NOT about uniqueness. Do NOT fail C1-02 when primary_ctas is populated.
17. VISUAL DENSITY (C1-01 scope): C1-01 asks whether the value proposition communicates within 5 seconds. This is verified by: (a) Is there a clear H1? (b) Is there an audience statement? (c) Is there a primary CTA? If yes to all three — C1-01 PASSES. "The text looks dense" is a VISUAL judgement you cannot make from extracted DOM text. The context block pre-calculates C1-01 when hero_text and H1 are both present. Never re-fail C1-01 on grounds of "density" or "too much text" — those require visual review and go in medium_issues at most.
"""


# ── Prompt mode ───────────────────────────────────────────────────────────────
# The 41-item checklist is already built on universal, industry-standard heuristics
# (Nielsen, Norman, Laws of UX, Gestalt, WCAG), so it applies to ANY site. Only the
# persona header and one checklist item are Lumynor-specific. "generic" mode swaps
# just those, so auditing a competitor/external site doesn't grade it as "fails to
# match our design system".

_GENERIC_HEADER = (
    "You are a Senior UX Auditor. You evaluate ANY website against universal, "
    "industry-standard usability and design principles.\n\n"
    "IMPORTANT: This site is NOT yours and is NOT affiliated with you. Judge it on its "
    "own merits — against its own apparent purpose, industry, and target audience. "
    "NEVER grade it against another company's design system, brand, or house style, and "
    "never suggest it should look like some other site. Infer what the site is trying to "
    "be from its own content, then assess how well it achieves that."
)

_LUMYNOR_HEADER = (
    "You are the Lumynor Design Audit Agent. Your knowledge base is the Lumynor UI/UX "
    "Design Principles document (v1.0, June 2026)."
)

_C1_03_LUMYNOR = ("Visual design communicates premium quality appropriate to a "
                  "digital product studio | van Schneider")
_C1_03_GENERIC = ("Visual design communicates quality appropriate to the site's own "
                  "apparent purpose and audience | van Schneider")


def _system_prompt(mode: str) -> str:
    """Return the system prompt for the requested mode ('lumynor' or 'generic')."""
    if (mode or "lumynor").lower() != "generic":
        return _SYSTEM_PROMPT
    p = _SYSTEM_PROMPT.replace(_LUMYNOR_HEADER, _GENERIC_HEADER, 1)
    p = p.replace(_C1_03_LUMYNOR, _C1_03_GENERIC, 1)
    return p


# ── Core audit function ───────────────────────────────────────────────────────

def run_audit(url: str, pages: list = None, notes: str = "", auditor_notes: str = "",
              mode: str = "lumynor") -> dict:
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
    # Fall back to the environment like the rest of the codebase does. Without this,
    # gemini_key is empty whenever the configured provider is Ollama — which silently
    # disabled the whole vision pass (visually_reviewed came back False in prod even
    # though screenshots were captured fine).
    import os as _os
    gemini_key = (stored.get("llmApiKey")
                  or _os.getenv("GEMINI_API_KEY")
                  or _os.getenv("GOOGLE_API_KEY")
                  or "")
    if not gemini_key and stored.get("llmProvider", "gemini") == "gemini":
        return {"error": "LLM not configured — set API key in Settings → Auto Blogger"}

    # ── Step 0: normalise the URL ─────────────────────────────────────────────
    # A user typing "powerup.money" (no scheme) previously went straight to the
    # fetcher, which cannot resolve a schemeless URL — it failed, and the audit
    # then scored the empty result 10/10. Default to https:// when omitted.
    url = (url or "").strip()
    if url and not re.match(r'^https?://', url, re.I):
        url = "https://" + url

    # SSRF guard — must run BEFORE any fetch. Mandatory once this is reachable by
    # the public, and harmless for admin use.
    try:
        assert_url_is_public(url)
    except UnsafeURLError as e:
        return {"error": str(e)}

    # ── Step 1: fetch live data ───────────────────────────────────────────────
    # Bounded: each render is a headless Chromium; unbounded concurrency would OOM
    # the shared Railway box. Non-blocking acquire so a flood fails fast instead of
    # queueing up and holding worker threads.
    if not _AUDIT_SLOTS.acquire(blocking=False):
        return {"error": "Audit capacity is full right now — please try again in a minute."}
    try:
        page_data = _fetch_page(url)
    finally:
        _AUDIT_SLOTS.release()

    # HARD FAIL if the page could not be fetched. Previously this fell through to
    # the LLM with an empty context: the scoring rule is "start at 10, deduct per
    # failure", so with no data there was nothing to deduct and the audit returned
    # a perfect 10/10 — while its own summary said "unable to reach the website"
    # and every Nielsen heuristic was marked PASS. A confidently wrong 10/10 is
    # worse than no audit, so refuse to score instead.
    fetch_err = page_data.get("fetch_error")
    if fetch_err:
        return {"error": f"Could not reach {url} — {fetch_err}. No audit was run: a page that cannot be loaded cannot be scored."}

    # Even without an explicit error, a page with no extractable content (empty
    # response, bot-block, JS wall) must not be scored either.
    has_content = any([
        (page_data.get("title") or "").strip(),
        (page_data.get("hero_text") or "").strip(),
        page_data.get("nav_links"),
        page_data.get("button_texts"),
        page_data.get("link_texts"),
    ])
    if not has_content:
        return {"error": f"Reached {url} but extracted no usable content (empty, bot-blocked, or JS-walled page). No audit was run."}

    pagespeed = _fetch_pagespeed(url)

    # Look at the page with actual eyes. This is what makes the visual checkpoints
    # (crowding, hierarchy, "does it look credible") answerable instead of punted.
    # On failure this records the exact reason in page_data["visual_skipped"] — never
    # fails silently, so the report stays honest about a capability it didn't deliver.
    visual = _visual_review(page_data, gemini_key)
    if visual:
        page_data["visual"] = visual

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
- Contrast, mobile layout, tap targets and text size are now MEASURED (see the ★ blocks above). Quote the real numbers and score them normally. Do NOT write "requires visual review" for any of them.
- Crowding, hierarchy and design quality come from a vision model that SAW the page (★ VISUAL REVIEW). Cite it as observed fact.
- Only animation quality (C4-03), hover states (C4-01) and loading indicators (C4-07) remain genuinely unverifiable.
- Do NOT assume anything not present in the data. If you cannot verify it, say so.

Return ONLY valid JSON — no markdown fences, no explanation outside the JSON."""

    # ── Step 3: LLM evaluation ────────────────────────────────────────────────
    try:
        llm_cfg = _build_llm_cfg(stored, gemini_key)
        raw = _llm(
            f"{_system_prompt(mode)}\n\n{user_prompt}",
            llm_cfg,
            json_mode=False,
            timeout=120,
            max_tokens=4000,
        )

        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            return {"error": "LLM did not return valid JSON", "raw": raw[:500]}

        report = json.loads(match.group())
        report["url"]           = url          # normalised (scheme added if omitted)
        report["mode"]          = (mode or "lumynor").lower()
        report["audited_at"]    = datetime.now(timezone.utc).isoformat()
        report["notes"]         = notes
        report["pagespeed"]     = pagespeed
        report["is_spa"]        = page_data.get("is_spa", False)
        report["render_method"] = page_data.get("render_method", "http")

        # Carry the measured evidence onto the report so the delivered audit can SHOW
        # its work ("tested on a 390px phone", "contrast computed, 3 failures") rather
        # than just asserting findings. Screenshots are deliberately NOT attached —
        # they're large base64 and the visual verdict already carries the signal.
        report["evidence"] = {
            "mobile":   page_data.get("mobile"),
            "contrast": page_data.get("contrast"),
            "trust":    page_data.get("trust"),
            "visual":   page_data.get("visual"),
            "visual_skipped": page_data.get("visual_skipped"),
            # Which strategy actually found each thing. Empty means "our probe missed it",
            # which is NOT the same as "the site lacks it" — keep the distinction in the
            # record so a false "X is missing" finding is traceable to the probe that
            # caused it rather than looking like the model made it up.
            "nav_source":        page_data.get("nav_source"),
            "active_nav_source": page_data.get("active_nav_source"),
            "cta_source":        page_data.get("cta_source"),
            "hero_source":       page_data.get("hero_source"),
            "footer_source":     page_data.get("footer_source"),
            "rendered_on": ["desktop 1440x900", "mobile 390x844"]
                           if page_data.get("mobile") and not (page_data.get("mobile") or {}).get("error")
                           else ["desktop 1440x900"],
        }
        return report

    except json.JSONDecodeError as e:
        print(f"[design_audit] LLM returned invalid JSON: {str(e)[:200]}")
        return {"error": "The AI evaluation returned malformed output. Please try again."}
    except Exception as e:
        # Log the real exception for debugging, but never show a visitor raw text
        # like "HTTP Error 429: Too Many Requests" — that was happening whenever
        # BOTH configured LLM providers were exhausted. Say what's actually true
        # (our infrastructure couldn't complete this right now) without dressing
        # it up as something the visitor did wrong.
        err_text = str(e)
        print(f"[design_audit] LLM evaluation failed: {err_text[:300]}")
        if re.search(r'\b429\b|rate.?limit|quota|too many requests', err_text, re.I):
            return {"error": "Our AI provider is at capacity right now — please try again in a few minutes."}
        return {"error": "We couldn't complete this audit right now. Please try again in a moment."}


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
        # Provenance — previously dropped on save, so an unlocked report lost all
        # trace of HOW it was produced (render method, mobile pass, contrast, vision).
        "evidence":          report.get("evidence", {}),
        "pagespeed":         report.get("pagespeed", {}),
        "render_method":     report.get("render_method", ""),
        "mode":              report.get("mode", "lumynor"),
    }
    # The evidence/pagespeed/render_method/mode columns are NEW. If the Supabase table
    # doesn't have them yet, Postgres rejects the whole insert — which would mean the
    # audit never saves and every email-unlock 404s. So: try the full row, and if the
    # schema isn't migrated yet, fall back to the columns we know exist rather than
    # losing the audit entirely. Run supabase_migration_design_audit_evidence.sql to
    # enable full evidence persistence.
    _NEW_COLS = ("evidence", "pagespeed", "render_method", "mode")
    try:
        res = sb.table("design_audits").insert(row).execute()
        saved = (res.data or [{}])[0]
        return {**report, "id": saved.get("id", row["id"])}
    except Exception as e:
        print(f"[design_audit] full save failed ({str(e)[:120]}) — retrying without new columns")
        legacy = {k: v for k, v in row.items() if k not in _NEW_COLS}
        try:
            res = sb.table("design_audits").insert(legacy).execute()
            saved = (res.data or [{}])[0]
            print("[design_audit] saved without evidence columns — run the migration to persist them")
            return {**report, "id": saved.get("id", legacy["id"])}
        except Exception as e2:
            print(f"[design_audit] save error: {e2}")
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
