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


def build_digest_text() -> str | None:
    """Build the WhatsApp-formatted morning briefing text."""
    sb = _sb()
    if not sb:
        return None

    today        = datetime.now(timezone.utc).date()
    yesterday    = today - timedelta(days=1)
    yest_iso     = yesterday.isoformat()
    today_iso    = today.isoformat()

    # Yesterday's events — fetch all columns so we can group meaningfully
    events = (
        sb.table("activity_events")
          .select("project, event_type, title, status")
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
        lines.append(f"📋 *Yesterday ({day_name})*")

        # Show wins first: milestones, deployments, completions
        wins = [e for e in events if e.get("event_type") in
                ("milestone", "launch", "deployment", "feature_completed")]
        if wins:
            lines.append("  *Wins*")
            for e in wins[:4]:
                proj  = (e.get("project") or "").replace("_", " ").title()
                verb  = _TYPE_VERB.get(e.get("event_type", ""), "✅")
                title = (e.get("title") or "").strip()
                lines.append(f"  {verb} · [{proj}] {title}")

        # Then group remaining work by project (skip already shown wins)
        win_ids = {id(e) for e in wins}
        other   = [e for e in events if id(e) not in win_ids]

        if other:
            lines.append("  *Progress*")
            # Show one line per project with count + most significant event
            proj_summary: dict[str, dict] = {}
            for e in other:
                proj = (e.get("project") or "general").strip()
                if proj not in proj_summary:
                    proj_summary[proj] = {"count": 0, "top": e}
                proj_summary[proj]["count"] += 1

            for proj, info in list(proj_summary.items())[:5]:
                label   = proj.replace("_", " ").title()
                count   = info["count"]
                top     = info["top"]
                verb    = _TYPE_VERB.get(top.get("event_type", "task_update"), "🔧 Progress")
                title   = (top.get("title") or "").strip()[:55]
                suffix  = f" (+{count-1} more)" if count > 1 else ""
                lines.append(f"  · [{label}] {verb}: {title}{suffix}")
    else:
        lines.append(f"📋 *Yesterday ({day_name})* — No activity logged")

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
