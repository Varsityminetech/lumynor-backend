-- Revenue Radar OS — Migration v5
-- Adds social profiles, reviews, address, established year, verified flag

ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS instagram_url    TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS facebook_url     TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS twitter_url      TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS address          TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS review_rating    NUMERIC(3,1);
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS review_count     INTEGER DEFAULT 0;
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS review_platform  TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS established_year INTEGER;
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS verified         BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_revenue_leads_instagram ON revenue_leads(instagram_url) WHERE instagram_url <> '';
CREATE INDEX IF NOT EXISTS idx_revenue_leads_review_count ON revenue_leads(review_count);
