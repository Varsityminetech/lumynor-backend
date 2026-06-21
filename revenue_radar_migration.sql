-- Revenue Radar OS — Phase 2 Migration
-- Run this in Supabase SQL Editor after Phase 2 schema

-- ── Lead categories per product ───────────────────────────────────────────────
-- district21:  event_organizer | event_company | venue | college | festival
-- agentforge:  saas_founder | startup_studio | agency | consultant
-- linkforge:   seo_agency | saas_company | content_business

CREATE TABLE IF NOT EXISTS revenue_leads (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_name    TEXT NOT NULL,
  website         TEXT,
  location        TEXT,
  product_target  TEXT NOT NULL CHECK (product_target IN ('district21', 'agentforge', 'linkforge')),
  category        TEXT,
  temperature     TEXT NOT NULL DEFAULT 'cold' CHECK (temperature IN ('hot', 'warm', 'cold')),
  relevance_score INTEGER DEFAULT 0 CHECK (relevance_score BETWEEN 0 AND 100),
  business_size   TEXT DEFAULT 'smb' CHECK (business_size IN ('startup', 'smb', 'enterprise')),
  digital_maturity TEXT DEFAULT 'basic' CHECK (digital_maturity IN ('basic', 'intermediate', 'advanced')),
  buying_signals  TEXT,
  source          TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'auto_scan')),
  source_url      TEXT,
  notes           TEXT,
  status          TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('pending_review', 'confirmed', 'rejected')),
  discovered_at   TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS lead_contacts (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id     UUID NOT NULL REFERENCES revenue_leads(id) ON DELETE CASCADE,
  name        TEXT,
  role        TEXT,
  linkedin    TEXT,
  email       TEXT,
  notes       TEXT,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS market_signals (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product      TEXT NOT NULL,
  signal_type  TEXT NOT NULL CHECK (signal_type IN ('competitor', 'pricing', 'launch', 'trend', 'discussion')),
  title        TEXT NOT NULL,
  summary      TEXT,
  source_url   TEXT,
  importance   TEXT DEFAULT 'medium' CHECK (importance IN ('high', 'medium', 'low')),
  dismissed    BOOLEAN DEFAULT FALSE,
  detected_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Auto-discovery uses status='pending_review'; manual defaults to 'confirmed'
-- Indexes for common filters
CREATE INDEX IF NOT EXISTS idx_revenue_leads_product    ON revenue_leads(product_target);
CREATE INDEX IF NOT EXISTS idx_revenue_leads_status     ON revenue_leads(status);
CREATE INDEX IF NOT EXISTS idx_revenue_leads_temp       ON revenue_leads(temperature);
CREATE INDEX IF NOT EXISTS idx_lead_contacts_lead_id    ON lead_contacts(lead_id);
CREATE INDEX IF NOT EXISTS idx_market_signals_product   ON market_signals(product);
CREATE INDEX IF NOT EXISTS idx_market_signals_dismissed ON market_signals(dismissed);
