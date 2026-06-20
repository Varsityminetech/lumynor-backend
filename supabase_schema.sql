-- ─────────────────────────────────────────────────────────────────────────────
-- Lumynor Systems — Supabase Schema (Phase 1)
-- Run this in your Supabase SQL Editor (Dashboard → SQL Editor → New query)
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Drop old tables if re-running ────────────────────────────────────────────
DROP TABLE IF EXISTS comments CASCADE;
DROP TABLE IF EXISTS blogs CASCADE;
DROP TABLE IF EXISTS leads CASCADE;
DROP TABLE IF EXISTS settings CASCADE;
DROP TABLE IF EXISTS user_profiles CASCADE;

-- ── Blogs ─────────────────────────────────────────────────────────────────────
CREATE TABLE blogs (
  id            text PRIMARY KEY,
  slug          text UNIQUE NOT NULL,
  title         text NOT NULL DEFAULT '',
  published     boolean DEFAULT false,
  category      text,
  is_auto_posted boolean DEFAULT false,
  seo_score     integer,
  views         integer DEFAULT 0,
  created_at    timestamptz DEFAULT now(),
  updated_at    timestamptz DEFAULT now(),
  data          jsonb NOT NULL DEFAULT '{}'   -- full blog object
);

CREATE INDEX idx_blogs_published  ON blogs(published);
CREATE INDEX idx_blogs_category   ON blogs(category);
CREATE INDEX idx_blogs_created_at ON blogs(created_at DESC);
CREATE INDEX idx_blogs_seo_score  ON blogs(seo_score);

-- ── Leads ─────────────────────────────────────────────────────────────────────
CREATE TABLE leads (
  id         text PRIMARY KEY,
  email      text,
  source     text DEFAULT 'website',
  created_at timestamptz DEFAULT now(),
  data       jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_leads_email      ON leads(email);
CREATE INDEX idx_leads_created_at ON leads(created_at DESC);

-- ── Comments ──────────────────────────────────────────────────────────────────
CREATE TABLE comments (
  id         text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  blog_id    text REFERENCES blogs(id) ON DELETE CASCADE,
  blog_slug  text NOT NULL,
  author     text NOT NULL,
  email      text,
  content    text NOT NULL,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX idx_comments_blog_id   ON comments(blog_id);
CREATE INDEX idx_comments_blog_slug ON comments(blog_slug);

-- ── Settings (key-value singleton store) ──────────────────────────────────────
CREATE TABLE settings (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL DEFAULT '{}',
  updated_at timestamptz DEFAULT now()
);

-- ── User Profiles (linked to Supabase Auth) ───────────────────────────────────
CREATE TABLE user_profiles (
  id           uuid REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
  email        text UNIQUE NOT NULL,
  display_name text,
  avatar_url   text,
  role         text DEFAULT 'user',   -- 'user' | 'admin'
  created_at   timestamptz DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Row Level Security
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE blogs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads         ENABLE ROW LEVEL SECURITY;
ALTER TABLE comments      ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings      ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;

-- Blogs: public can read published; service role can do everything
CREATE POLICY "Public read published blogs"
  ON blogs FOR SELECT USING (published = true);

CREATE POLICY "Service role full access on blogs"
  ON blogs FOR ALL USING (auth.role() = 'service_role');

-- Leads: only service role
CREATE POLICY "Service role full access on leads"
  ON leads FOR ALL USING (auth.role() = 'service_role');

-- Comments: public read + insert; service role full access
CREATE POLICY "Public read comments"
  ON comments FOR SELECT USING (true);

CREATE POLICY "Public insert comments"
  ON comments FOR INSERT WITH CHECK (true);

CREATE POLICY "Service role full access on comments"
  ON comments FOR ALL USING (auth.role() = 'service_role');

-- Settings: only service role
CREATE POLICY "Service role full access on settings"
  ON settings FOR ALL USING (auth.role() = 'service_role');

-- User profiles: users can read/update their own; service role full access
CREATE POLICY "Users read own profile"
  ON user_profiles FOR SELECT USING (auth.uid() = id);

CREATE POLICY "Users update own profile"
  ON user_profiles FOR UPDATE USING (auth.uid() = id);

CREATE POLICY "Service role full access on user_profiles"
  ON user_profiles FOR ALL USING (auth.role() = 'service_role');

-- ─────────────────────────────────────────────────────────────────────────────
-- Auto-create user profile on sign up
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
  INSERT INTO public.user_profiles (id, email, display_name)
  VALUES (
    NEW.id,
    NEW.email,
    COALESCE(NEW.raw_user_meta_data->>'full_name', split_part(NEW.email, '@', 1))
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
