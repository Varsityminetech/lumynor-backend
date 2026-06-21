-- Revenue Radar OS — Migration v6 (Consolidated)
-- Adds ALL columns from v2 + v4 + v5 safely (IF NOT EXISTS)
-- Run this if you skipped v2, v4, or v5

-- v2: contact info
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS linkedin_url    TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS contact_email   TEXT DEFAULT '';

-- v4: phone
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS phone           TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS whatsapp        TEXT DEFAULT '';

-- v5: social + reviews + authenticity
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS instagram_url   TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS facebook_url    TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS twitter_url     TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS address         TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS review_rating   NUMERIC(3,1);
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS review_count    INTEGER DEFAULT 0;
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS review_platform TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS established_year INTEGER;
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS verified        BOOLEAN DEFAULT FALSE;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_revenue_leads_phone        ON revenue_leads(phone)         WHERE phone <> '';
CREATE INDEX IF NOT EXISTS idx_revenue_leads_instagram    ON revenue_leads(instagram_url) WHERE instagram_url <> '';
CREATE INDEX IF NOT EXISTS idx_revenue_leads_review_count ON revenue_leads(review_count);
