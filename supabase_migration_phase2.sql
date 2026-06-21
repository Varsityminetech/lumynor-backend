-- ─────────────────────────────────────────────────────────────────────────────
-- Lumynor Systems — Supabase Migration Phase 2
-- Run this in: Supabase Dashboard → SQL Editor → New query
-- Safe to re-run (uses IF NOT EXISTS everywhere)
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Patch user_profiles with missing columns ──────────────────────────────────
ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS bio             text,
  ADD COLUMN IF NOT EXISTS avatar_color    text,
  ADD COLUMN IF NOT EXISTS reading_history jsonb DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS saved_blogs     jsonb DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS favorite_blogs  jsonb DEFAULT '[]';

-- ── Activity Events ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS activity_events (
  id            text        PRIMARY KEY,
  source        text        NOT NULL DEFAULT 'manual',
  project       text        NOT NULL DEFAULT 'other',
  event_type    text        NOT NULL DEFAULT 'task_update',
  title         text        NOT NULL DEFAULT '',
  summary       text        NOT NULL DEFAULT '',
  status        text        NOT NULL DEFAULT 'new',
  priority      text        NOT NULL DEFAULT 'normal',
  actor         text        NOT NULL DEFAULT '',
  related_url   text        NOT NULL DEFAULT '',
  metadata_json jsonb                DEFAULT '{}',
  created_at    timestamptz          DEFAULT now(),
  updated_at    timestamptz          DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ae_project    ON activity_events(project);
CREATE INDEX IF NOT EXISTS idx_ae_status     ON activity_events(status);
CREATE INDEX IF NOT EXISTS idx_ae_priority   ON activity_events(priority);
CREATE INDEX IF NOT EXISTS idx_ae_created_at ON activity_events(created_at DESC);

ALTER TABLE activity_events ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='activity_events' AND policyname='Service role full access on activity_events'
  ) THEN
    CREATE POLICY "Service role full access on activity_events"
      ON activity_events FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Projects ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
  id                    text        PRIMARY KEY,
  slug                  text        UNIQUE NOT NULL,
  name                  text        NOT NULL DEFAULT '',
  owner_name            text        NOT NULL DEFAULT '',
  status                text        NOT NULL DEFAULT 'Development',
  note                  text        NOT NULL DEFAULT '',
  description           text        NOT NULL DEFAULT '',
  strategic_notes       text        NOT NULL DEFAULT '',
  priority              text        NOT NULL DEFAULT 'Medium',
  strategic_importance  text        NOT NULL DEFAULT 'Internal Tool',
  current_blocker       text        NOT NULL DEFAULT '',
  next_milestone        text        NOT NULL DEFAULT '',
  target_launch_date    date,
  notes                 text        NOT NULL DEFAULT '',
  human_locked_fields   jsonb                DEFAULT '[]',
  primary_repo          text        NOT NULL DEFAULT '',
  auto_created          boolean              DEFAULT false,
  recent_activity_count integer              DEFAULT 0,
  last_activity_at      timestamptz,
  last_commit_at        timestamptz,
  last_pr_at            timestamptz,
  last_deploy_at        timestamptz,
  created_at            timestamptz          DEFAULT now(),
  updated_at            timestamptz          DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_projects_slug   ON projects(slug);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='projects' AND policyname='Service role full access on projects'
  ) THEN
    CREATE POLICY "Service role full access on projects"
      ON projects FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Project Members ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_members (
  id           text        PRIMARY KEY,
  project_slug text        NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
  user_name    text        NOT NULL,
  role         text        NOT NULL DEFAULT 'member',
  created_at   timestamptz          DEFAULT now(),
  UNIQUE (project_slug, user_name)
);

CREATE INDEX IF NOT EXISTS idx_pm_project_slug ON project_members(project_slug);

ALTER TABLE project_members ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='project_members' AND policyname='Service role full access on project_members'
  ) THEN
    CREATE POLICY "Service role full access on project_members"
      ON project_members FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Project Messages ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_messages (
  id           text        PRIMARY KEY,
  project_slug text        NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
  sender_name  text        NOT NULL DEFAULT '',
  message      text        NOT NULL DEFAULT '',
  read_by_json jsonb                DEFAULT '[]',
  created_at   timestamptz          DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pmsg_project_slug ON project_messages(project_slug);
CREATE INDEX IF NOT EXISTS idx_pmsg_created_at   ON project_messages(created_at);

ALTER TABLE project_messages ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='project_messages' AND policyname='Service role full access on project_messages'
  ) THEN
    CREATE POLICY "Service role full access on project_messages"
      ON project_messages FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Story Opportunities (Authority OS) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS story_opportunities (
  id               text        PRIMARY KEY,
  project_slug     text        NOT NULL DEFAULT '',
  title            text        NOT NULL DEFAULT '',
  summary          text        NOT NULL DEFAULT '',
  opportunity_type text        NOT NULL DEFAULT 'Build In Public',
  source_event_ids jsonb                DEFAULT '[]',
  importance_score integer              DEFAULT 0,
  why_it_matters   text        NOT NULL DEFAULT '',
  suggested_angle  text        NOT NULL DEFAULT '',
  status           text        NOT NULL DEFAULT 'new',
  created_at       timestamptz          DEFAULT now(),
  updated_at       timestamptz          DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_so_project_slug     ON story_opportunities(project_slug);
CREATE INDEX IF NOT EXISTS idx_so_status           ON story_opportunities(status);
CREATE INDEX IF NOT EXISTS idx_so_importance_score ON story_opportunities(importance_score DESC);

ALTER TABLE story_opportunities ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='story_opportunities' AND policyname='Service role full access on story_opportunities'
  ) THEN
    CREATE POLICY "Service role full access on story_opportunities"
      ON story_opportunities FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Strategy Briefs ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS strategy_briefs (
  id                          text        PRIMARY KEY,
  biggest_progress            text        NOT NULL DEFAULT '',
  biggest_risks               text        NOT NULL DEFAULT '',
  most_important_opportunity  text        NOT NULL DEFAULT '',
  recommended_focus           text        NOT NULL DEFAULT '',
  projects_to_ignore          text        NOT NULL DEFAULT '',
  raw_json                    jsonb                DEFAULT '{}',
  created_at                  timestamptz          DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sb_created_at ON strategy_briefs(created_at DESC);

ALTER TABLE strategy_briefs ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='strategy_briefs' AND policyname='Service role full access on strategy_briefs'
  ) THEN
    CREATE POLICY "Service role full access on strategy_briefs"
      ON strategy_briefs FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Weekly Reports ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS weekly_reports (
  id           text        PRIMARY KEY,
  week_start   date        NOT NULL,
  week_end     date        NOT NULL,
  weekly_score integer              DEFAULT 0,
  raw_json     jsonb                DEFAULT '{}',
  created_at   timestamptz          DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wr_created_at ON weekly_reports(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wr_week_start ON weekly_reports(week_start DESC);

ALTER TABLE weekly_reports ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='weekly_reports' AND policyname='Service role full access on weekly_reports'
  ) THEN
    CREATE POLICY "Service role full access on weekly_reports"
      ON weekly_reports FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Attention Logs (Strategy OS) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS attention_logs (
  id           text        PRIMARY KEY,
  project_slug text        NOT NULL DEFAULT '',
  minutes      integer     NOT NULL DEFAULT 0,
  logged_at    timestamptz          DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_al_project_slug ON attention_logs(project_slug);
CREATE INDEX IF NOT EXISTS idx_al_logged_at    ON attention_logs(logged_at DESC);

ALTER TABLE attention_logs ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='attention_logs' AND policyname='Service role full access on attention_logs'
  ) THEN
    CREATE POLICY "Service role full access on attention_logs"
      ON attention_logs FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;
