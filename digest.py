"""
Lumynor Daily Digest — morning WhatsApp briefing via Twilio.
Surfaces what was actually accomplished yesterday, today's strategic focus,
active blockers with impact, and authority opportunities pending action.
"""
from datetime import datetime, timezone, timedelta
from db import _sb, get_settings

# Maps raw event_type → readable verb for digest output
_TYPE_VERB = {
    "milestone":         "🏁 Milestone",
    "deployment":        "🚀 Deployed",
    "feature_completed": "✅ Completed",
    "decision":          "🧭 Decision",
    "review_needed":     "👀 Ready for review",
    "task_update":       "🔧 Progress",
    "blocker":           "🔴 Blocked",
    "research":          "🔍 Research",
    "meeting":           "📞 Meeting",
    "launch":            "🚀 Launched",
}

# Priority order: surface these event types first
_TYPE_ORDER = [
    "milestone", "launch", "deployment", "feature_completed",
    "decision", "review_needed", "research", "meeting", "task_update", "blocker",
]


def _rank_event(e: dict) -> int:
    t = e.get("event_type", "task_update")
    try:
        return _TYPE_ORDER.index(t)
    except ValueError:
        return 99


def _summarize_accomplishments(events: list) -> list | None:
    """Turn raw git/activity events into a human 'what actually got done' list.

    The raw events are noise ("Deployment success" x4, "N commits pushed to main").
    The real work lives in the commit messages (event `summary`). This feeds those
    to the LLM and asks for a concise, deduplicated list of ACCOMPLISHED TASKS —
    not commit counts or deploy pings. Returns a list of WhatsApp lines, or None
    if the LLM is unavailable/fails (caller falls back to the raw grouping)."""
    try:
        from auto_blogger import _build_llm_cfg, _llm
    except ImportError:
        return None

    stored     = get_settings("auto_blog")
    gemini_key = stored.get("llmApiKey", "")
    if not gemini_key and stored.get("llmProvider", "gemini") == "gemini":
        return None

    # Build the raw material: project + commit messages / event detail
    detail_lines = []
    for e in events:
        proj = (e.get("project") or "general").replace("_", " ").title()
        title = (e.get("title") or "").strip()
        summ  = (e.get("summary") or "").strip()
        # summary holds the commit messages ("fix X · add Y") — the real signal
        text = summ or title
        if text:
            detail_lines.append(f"[{proj}] {text}")
    if not detail_lines:
        return None

    raw = "\n".join(detail_lines[:40])
    prompt = f"""You are writing the "what got done yesterday" section of a founder's daily briefing.

Below are yesterday's raw dev events and commit messages across projects. Write a SHORT list of what was actually ACCOMPLISHED — the real tasks/features/fixes.

STRICT RULES:
- Describe the WORK, never the git mechanics. NEVER write "N commits pushed", "deployment success", "deployed to main", or commit counts.
- Deduplicate: if 4 deploys and 6 commits all relate to one feature, that's ONE line about the feature.
- Group by project. One line per meaningful accomplishment, grouped under its project.
- If commit messages are vague ("wip", "fix", "update"), infer the theme conservatively or omit — never invent specifics.
- Plain text, WhatsApp style. Each line starts with "  · " and names the project in brackets, e.g. "  · [LinkForge] Added retry logic to the ingestion pipeline".
- Max 6 lines total. Most important work first.
- Output ONLY the lines, nothing else.

RAW EVENTS:
{raw}"""

    try:
        llm_cfg = _build_llm_cfg(stored, gemini_key)
        out = _llm(prompt, llm_cfg, json_mode=False, timeout=45, max_tokens=350)
        lines = [ln.rstrip() for ln in out.strip().splitlines() if ln.strip()]
        # keep only lines that look like our bullet format; guard against preamble
        lines = [ln if ln.lstrip().startswith("·") or ln.lstrip().startswith("- ")
                 else f"  · {ln.lstrip()}" for ln in lines]
        return lines[:6] or None
    except Exception as e:
        print(f"[digest] accomplishment summary failed, using raw grouping: {e}")
        return None


def build_digest_text() -> str | None:
    """Build the WhatsApp-formatted morning briefing text."""
    sb = _sb()
    if not sb:
        return None

    today        = datetime.now(timezone.utc).date()
    yesterday    = today - timedelta(days=1)
    yest_iso     = yesterday.isoformat()
    today_iso    = today.isoformat()

    # Yesterday's events — include `summary` (holds the actual commit messages),
    # which is what describes the real work; the title is just "N commits pushed".
    events = (
        sb.table("activity_events")
          .select("project, event_type, title, status, summary")
          .gte("created_at", f"{yest_iso}T00:00:00+00:00")
          .lt("created_at",  f"{today_iso}T00:00:00+00:00")
          .order("created_at", desc=True)
          .limit(50).execute().data or []
    )

    # Sort: milestones/deploys first, then by project for grouping
    events.sort(key=_rank_event)

    # Group by project
    by_project: dict[str, list] = {}
    for e in events:
        proj = (e.get("project") or "general").strip()
        by_project.setdefault(proj, []).append(e)

    # Portfolio health from Strategy OS
    try:
        import strategy as strat
        focus_list = strat.get_daily_focus(limit=3)
    except Exception:
        focus_list = []

    try:
        import strategy as strat
        blockers = strat.get_strategic_blockers()
    except Exception:
        from team import get_projects
        raw = get_projects()
        blockers = [
            {"project_name": p.get("name", p["slug"]),
             "blocker": (p.get("current_blocker") or "").strip(),
             "impact": "Limiting progress",
             "priority": p.get("priority", "Medium")}
            for p in raw if (p.get("current_blocker") or "").strip()
        ]

    # Authority opportunities pending approval
    pending_opps = (
        sb.table("story_opportunities")
          .select("title, importance_score, opportunity_type")
          .eq("status", "new")
          .order("importance_score", desc=True)
          .limit(3).execute().data or []
    )

    # ── Build message ────────────────────────────────────────────────────────────
    day_name = yesterday.strftime("%A, %d %b")
    lines = [f"☀️ *Lumynor Briefing — {today.strftime('%d %b')}*\n"]

    # ── Section 1: What was accomplished yesterday ───────────────────────────────
    if events:
        lines.append(f"📋 *Done Yesterday ({day_name})*")

        # Preferred: an LLM-synthesized "what actually got done" list built from the
        # commit messages — real accomplishments, not "N commits pushed / deploy OK".
        summary_lines = _summarize_accomplishments(events)
        if summary_lines:
            lines.extend(summary_lines)
        else:
            # Fallback (LLM unavailable): the old raw grouping, so the digest never
            # breaks — but at least prefer the commit-message summary over the title.
            wins = [e for e in events if e.get("event_type") in
                    ("milestone", "launch", "deployment", "feature_completed")]
            if wins:
                lines.append("  *Wins*")
                for e in wins[:4]:
                    proj  = (e.get("project") or "").replace("_", " ").title()
                    verb  = _TYPE_VERB.get(e.get("event_type", ""), "✅")
                    detail = (e.get("summary") or e.get("title") or "").strip()[:70]
                    lines.append(f"  {verb} · [{proj}] {detail}")

            win_ids = {id(e) for e in wins}
            other   = [e for e in events if id(e) not in win_ids]
            if other:
                lines.append("  *Progress*")
                proj_summary: dict[str, dict] = {}
                for e in other:
                    proj = (e.get("project") or "general").strip()
                    if proj not in proj_summary:
                        proj_summary[proj] = {"count": 0, "top": e}
                    proj_summary[proj]["count"] += 1
                for proj, info in list(proj_summary.items())[:5]:
                    label  = proj.replace("_", " ").title()
                    count  = info["count"]
                    top    = info["top"]
                    detail = (top.get("summary") or top.get("title") or "").strip()[:55]
                    suffix = f" (+{count-1} more)" if count > 1 else ""
                    lines.append(f"  · [{label}] {detail}{suffix}")
    else:
        lines.append(f"📋 *Done Yesterday ({day_name})* — No activity logged")

    lines.append("")

    # ── Section 2: Today's focus (from Strategy OS) ──────────────────────────────
    if focus_list:
        lines.append("🎯 *Focus Today*")
        for f in focus_list:
            score   = f.get("score", 0)
            name    = f.get("name", f.get("slug", ""))
            reason  = f.get("reason", "")
            health  = f.get("health", "healthy")
            h_icon  = "🔴" if health == "blocked" else ("🟡" if health == "at_risk" else "🟢")
            lines.append(f"  {h_icon} *{name}* (score {score}) — {reason}")
    lines.append("")

    # ── Section 3: Blockers ──────────────────────────────────────────────────────
    if blockers:
        lines.append(f"🔴 *Blockers* ({len(blockers)})")
        for b in blockers[:4]:
            name    = b.get("project_name", "")
            blocker = (b.get("blocker") or "")[:70]
            impact  = b.get("impact", "")
            lines.append(f"  · *{name}* — {blocker}")
            lines.append(f"    ↳ {impact}")
    else:
        lines.append("✅ *No blockers* — clear runway")

    # ── Section 4: Authority opportunities to act on ─────────────────────────────
    if pending_opps:
        lines.append("")
        lines.append(f"💡 *Authority Opportunities* — {len(pending_opps)} awaiting review")
        for o in pending_opps:
            opp_type = (o.get("opportunity_type") or "").replace("_", " ").title()
            title    = (o.get("title") or "")[:60]
            score    = o.get("importance_score", 0)
            lines.append(f"  · [{opp_type}] {title} (score {score})")

    lines.append("\n_ATLAS · Lumynor Systems_")
    return "\n".join(lines)


def send_digest() -> dict:
    """Send the morning digest via Twilio WhatsApp API."""
    stored      = get_settings("digest")
    account_sid = stored.get("twilioAccountSid", "").strip()
    auth_token  = stored.get("twilioAuthToken", "").strip()
    from_number = stored.get("twilioFrom", "").strip()
    to_number   = stored.get("digestTo", "").strip()

    if not from_number.startswith('whatsapp:'):
        from_number = f'whatsapp:{from_number}'
    if not to_number.startswith('whatsapp:'):
        to_number = f'whatsapp:{to_number}'

    if not all([account_sid, auth_token, from_number, to_number]):
        return {
            "ok": False,
            "error": "Twilio credentials not fully configured. Fill all four fields in Settings → Daily Digest.",
        }

    text = build_digest_text()
    if not text:
        return {"ok": False, "error": "Could not build digest — database unavailable."}

    try:
        from twilio.rest import Client
        client  = Client(account_sid, auth_token)
        message = client.messages.create(from_=from_number, to=to_number, body=text)
        return {"ok": True, "sid": message.sid, "status": message.status}
    except ImportError:
        return {
            "ok": False,
            "error": "twilio package not installed. Run: pip install twilio",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
