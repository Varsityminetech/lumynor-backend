-- Revenue Radar OS — Migration v2
-- Adds linkedin_url and contact_email to revenue_leads
-- Safe to run multiple times (IF NOT EXISTS / DO NOTHING pattern)

ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS linkedin_url   TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS contact_email  TEXT DEFAULT '';
