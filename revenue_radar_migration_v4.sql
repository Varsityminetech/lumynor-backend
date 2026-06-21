-- Revenue Radar OS — Migration v4
-- Adds phone and whatsapp contact fields to revenue_leads

ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS phone     TEXT DEFAULT '';
ALTER TABLE revenue_leads ADD COLUMN IF NOT EXISTS whatsapp  TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_revenue_leads_phone ON revenue_leads(phone) WHERE phone <> '';
