# Lumynor Backend — Dev Log

---

## 2026-07-02

### Credibility Revision — Full Fix

**Problem:** Credibility revision was silently doing nothing on most blogs.

**Root causes fixed:**
1. `/api/blogs/auto-generate` was blocking the Railway proxy for 10+ min → moved pipeline to `asyncio.create_task()` background task, returns 202 immediately.
2. `revise-credibility` endpoint called `_build_llm_cfg(db.get_settings())` with 1 arg (needs 2) → TypeError crash.
3. `validate_credibility` used `href=` regex on Markdown content → found zero links → always triggered "No credible sources" hard fail → rewriter never ran.
4. `revise_blog_credibility` broke out of the revision loop immediately on ANY hard fail, including fixable ones (overconfident claims, repeated sentences, unsupported stats).
5. After revision, `content_html` was cleared but `format_blog_html` was never called → blank blog page.

**Final fix — source hard fails now go through web search:**
- `_extract_claims_needing_sources()` — LLM extracts 3–5 search queries from the article
- `_find_credible_sources_for_claims()` — searches via Serper → Bing → DDG, filters to `_CREDIBLE_DOMAINS` only
- `_weave_sources_into_blog()` — LLM rewrites article to cite the real found URLs as inline links
- Only breaks (reports failure honestly) if search returns zero credible-domain results

**Hard fail split:**
- Unfixable (source-related): run web search pipeline above
- Fixable (repetition, stats, claims): proceed directly to rewrite pass

---

### Mother Brain — Blog & Affiliate Control

**New registered tools (WhatsApp + dashboard chat):**
- `write_blog` — research + write on a specific topic/keyword; `publish: true/false`
- `publish_blog` / `unpublish_blog` — find by `title_contains` or `blog_slug`, toggle live
- `list_blogs` — returns titles, slugs, publish status, SEO scores
- `add_affiliate` / `remove_affiliate` — create/delete keyword→URL pairs

**`/api/atlas/chat` upgraded** to route through `orchestrate()` so dashboard chat can trigger actions, not just answer questions.

**`chat()` enriched** with blog post list + affiliate links in context so Mother knows current state.

**Anti-hallucination rules embedded:**
- 5 iron rules in `chat()` system prompt — cannot claim to have executed actions without tool confirmation
- `_run_tool` now checks result for `error` key before saying "done"
- Proactive message personality: never invent data not in context

**Confirmation window:** 10 min → 30 min. Expired pending now returns explicit message instead of silently dropping.

---

### Google Search Added to Research Chain

Serper.dev Google Search API added between Tavily and Bing:
`Tavily → Google (Serper) → Bing → DuckDuckGo`

`SERPER_API_KEY` env var added to Railway.

---

## 2026-07-01

### Affiliate Links Feature

**Backend:**
- `affiliate_links` + `affiliate_clicks` tables (see `supabase_migration_affiliate.sql`)
- `inject_affiliate_links()` — regex keyword replacement in HTML, skips `<a>`/headings/code, max 2 per keyword
- `strip_affiliate_links()` — removes injected redirect anchors
- Click tracking via `/api/affiliate/click/{id}` → 302 redirect to real URL
- `POST /api/blogs/{id}/toggle-affiliates` — enables/disables injection per blog

**Frontend:**
- Affiliate management moved into blog edit form (add/delete/toggle per keyword, per-blog enable toggle)
- "Affiliate Tracker" sidebar tab — read-only analytics view (click cards + per-blog breakdown)
