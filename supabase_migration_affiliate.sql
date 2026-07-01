-- Affiliate links: keyword → URL mappings
CREATE TABLE IF NOT EXISTS affiliate_links (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keyword     TEXT NOT NULL,
  url         TEXT NOT NULL,
  is_active   BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Click events: one row per click on an affiliate link
CREATE TABLE IF NOT EXISTS affiliate_clicks (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  affiliate_id UUID REFERENCES affiliate_links(id) ON DELETE CASCADE,
  blog_id      TEXT,
  blog_slug    TEXT,
  clicked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_affiliate_id ON affiliate_clicks(affiliate_id);
CREATE INDEX IF NOT EXISTS idx_affiliate_clicks_blog_id ON affiliate_clicks(blog_id);
