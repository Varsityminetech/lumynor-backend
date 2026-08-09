-- ─────────────────────────────────────────────────────────────────────────────
-- Lumynor Systems — Billing & Accounts OS Migration
-- Run this in: Supabase Dashboard → SQL Editor → New query
-- Safe to re-run (uses IF NOT EXISTS everywhere)
--
-- Company billing profile (legal name, GSTIN, bank details, invoice prefix)
-- is NOT a table here — it reuses the existing generic `settings` table via
-- db.get_settings("billing_company_profile") / db.save_settings(...).
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Clients (project/service engagements) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS billing_clients (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name             TEXT NOT NULL,
  legal_name       TEXT,
  contact_name     TEXT,
  email            TEXT,
  phone            TEXT,
  whatsapp_number  TEXT,
  billing_address  TEXT,
  state            TEXT,
  gstin            TEXT,
  notes            TEXT,
  status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_clients_status ON billing_clients(status);

ALTER TABLE billing_clients ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_clients' AND policyname='Service role full access on billing_clients'
  ) THEN
    CREATE POLICY "Service role full access on billing_clients"
      ON billing_clients FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Customers (product/subscription billing) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS billing_customers (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name             TEXT NOT NULL,
  legal_name       TEXT,
  contact_name     TEXT,
  email            TEXT,
  phone            TEXT,
  whatsapp_number  TEXT,
  billing_address  TEXT,
  state            TEXT,
  gstin            TEXT,
  notes            TEXT,
  status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_customers_status ON billing_customers(status);

ALTER TABLE billing_customers ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_customers' AND policyname='Service role full access on billing_customers'
  ) THEN
    CREATE POLICY "Service role full access on billing_customers"
      ON billing_customers FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Orders (client engagements — delivery/order status tracking) ────────────────
CREATE TABLE IF NOT EXISTS billing_orders (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id     UUID NOT NULL REFERENCES billing_clients(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  description   TEXT,
  status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
                  ('draft', 'in_progress', 'delivered', 'invoiced', 'paid', 'overdue', 'cancelled')),
  amount        NUMERIC(12,2),
  currency      TEXT NOT NULL DEFAULT 'INR',
  start_date    DATE,
  due_date      DATE,
  delivered_at  TIMESTAMPTZ,
  notes         TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_orders_client ON billing_orders(client_id);
CREATE INDEX IF NOT EXISTS idx_billing_orders_status ON billing_orders(status);

ALTER TABLE billing_orders ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_orders' AND policyname='Service role full access on billing_orders'
  ) THEN
    CREATE POLICY "Service role full access on billing_orders"
      ON billing_orders FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Plans (product/subscription catalog — reference data only in v1) ────────────
CREATE TABLE IF NOT EXISTS billing_plans (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              TEXT NOT NULL,
  description       TEXT,
  price             NUMERIC(12,2),
  currency          TEXT NOT NULL DEFAULT 'INR',
  billing_interval  TEXT NOT NULL DEFAULT 'monthly' CHECK (billing_interval IN
                       ('monthly', 'quarterly', 'yearly', 'one_time')),
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE billing_plans ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_plans' AND policyname='Service role full access on billing_plans'
  ) THEN
    CREATE POLICY "Service role full access on billing_plans"
      ON billing_plans FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Subscriptions (product billing half; mandate fields are Phase-2 scaffolding —
--    stored inert in v1, no gateway call is ever made against them yet) ─────────
CREATE TABLE IF NOT EXISTS billing_subscriptions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id         UUID NOT NULL REFERENCES billing_customers(id) ON DELETE CASCADE,
  plan_id             UUID NOT NULL REFERENCES billing_plans(id),
  status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'cancelled')),
  started_at          DATE,
  current_period_end  DATE,
  notes               TEXT,
  -- Phase 2 (Razorpay UPI Autopay / NACH e-mandate) scaffolding:
  mandate_status      TEXT NOT NULL DEFAULT 'none' CHECK (mandate_status IN
                         ('none', 'pending', 'active', 'paused', 'cancelled', 'failed')),
  mandate_provider    TEXT DEFAULT 'razorpay',
  mandate_id          TEXT,
  mandate_max_amount  NUMERIC(12,2),
  mandate_start_date  DATE,
  mandate_end_date    DATE,
  mandate_frequency   TEXT,
  next_charge_date    DATE,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_subscriptions_customer ON billing_subscriptions(customer_id);

ALTER TABLE billing_subscriptions ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_subscriptions' AND policyname='Service role full access on billing_subscriptions'
  ) THEN
    CREATE POLICY "Service role full access on billing_subscriptions"
      ON billing_subscriptions FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Invoices — bills either a client or a customer, never both ──────────────────
-- GST snapshot columns are captured at creation time so a later company-profile
-- edit never rewrites a historical invoice's tax facts.
CREATE TABLE IF NOT EXISTS billing_invoices (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id             UUID REFERENCES billing_clients(id) ON DELETE CASCADE,
  customer_id           UUID REFERENCES billing_customers(id) ON DELETE CASCADE,
  order_id              UUID REFERENCES billing_orders(id) ON DELETE SET NULL,
  subscription_id       UUID REFERENCES billing_subscriptions(id) ON DELETE SET NULL,
  invoice_number        TEXT NOT NULL UNIQUE,
  status                TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
                           ('draft', 'sent', 'paid', 'partially_paid', 'overdue', 'cancelled')),
  issue_date            DATE,
  due_date              DATE NOT NULL,
  currency              TEXT NOT NULL DEFAULT 'INR',
  -- GST snapshot, frozen at creation
  company_gstin         TEXT,
  company_state         TEXT,
  recipient_gstin       TEXT,
  recipient_state       TEXT,
  place_of_supply_state TEXT,
  -- Totals
  taxable_amount        NUMERIC(12,2) NOT NULL DEFAULT 0,
  cgst_amount           NUMERIC(12,2) NOT NULL DEFAULT 0,
  sgst_amount           NUMERIC(12,2) NOT NULL DEFAULT 0,
  igst_amount           NUMERIC(12,2) NOT NULL DEFAULT 0,
  total_tax             NUMERIC(12,2) NOT NULL DEFAULT 0,
  total                 NUMERIC(12,2) NOT NULL DEFAULT 0,
  amount_paid           NUMERIC(12,2) NOT NULL DEFAULT 0,
  notes                 TEXT,
  sent_at               TIMESTAMPTZ,
  created_at            TIMESTAMPTZ DEFAULT NOW(),
  updated_at            TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT billing_invoices_one_recipient CHECK (num_nonnulls(client_id, customer_id) = 1)
);

CREATE INDEX IF NOT EXISTS idx_billing_invoices_client   ON billing_invoices(client_id);
CREATE INDEX IF NOT EXISTS idx_billing_invoices_customer ON billing_invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_billing_invoices_status   ON billing_invoices(status);
CREATE INDEX IF NOT EXISTS idx_billing_invoices_due_date ON billing_invoices(due_date);

ALTER TABLE billing_invoices ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_invoices' AND policyname='Service role full access on billing_invoices'
  ) THEN
    CREATE POLICY "Service role full access on billing_invoices"
      ON billing_invoices FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Invoice line items — HSN/SAC + discount, GST split per line ─────────────────
CREATE TABLE IF NOT EXISTS billing_invoice_items (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id        UUID NOT NULL REFERENCES billing_invoices(id) ON DELETE CASCADE,
  description       TEXT NOT NULL,
  hsn_sac_code      TEXT,
  quantity          NUMERIC(12,2) NOT NULL DEFAULT 1,
  unit_price        NUMERIC(12,2) NOT NULL DEFAULT 0,
  discount_percent  NUMERIC(5,2) NOT NULL DEFAULT 0,
  discount_amount   NUMERIC(12,2) NOT NULL DEFAULT 0,
  taxable_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
  gst_rate_percent  NUMERIC(5,2) NOT NULL DEFAULT 18,
  cgst_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
  sgst_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
  igst_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
  line_total        NUMERIC(12,2) NOT NULL DEFAULT 0,
  sort_order        INTEGER NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_invoice_items_invoice ON billing_invoice_items(invoice_id);

ALTER TABLE billing_invoice_items ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_invoice_items' AND policyname='Service role full access on billing_invoice_items'
  ) THEN
    CREATE POLICY "Service role full access on billing_invoice_items"
      ON billing_invoice_items FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Payments — manual records; one invoice can have multiple partial payments ──
CREATE TABLE IF NOT EXISTS billing_payments (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id      UUID NOT NULL REFERENCES billing_invoices(id) ON DELETE CASCADE,
  amount          NUMERIC(12,2) NOT NULL,
  paid_at         DATE NOT NULL,
  method          TEXT NOT NULL DEFAULT 'bank_transfer' CHECK (method IN
                     ('bank_transfer', 'upi', 'cash', 'cheque', 'card', 'other')),
  reference_note  TEXT,
  recorded_by     TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_payments_invoice ON billing_payments(invoice_id);

ALTER TABLE billing_payments ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_payments' AND policyname='Service role full access on billing_payments'
  ) THEN
    CREATE POLICY "Service role full access on billing_payments"
      ON billing_payments FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- ── Pending-payment alerts — materialized rows (not a live query), so the UI has
--    a stable badge count and a dismiss/acknowledge state. Refreshed every 60s by
--    billing_alert_daemon() in main.py, which upserts idempotently on the unique
--    (invoice_id, alert_type) pair instead of duplicating rows every tick. ───────
CREATE TABLE IF NOT EXISTS billing_alerts (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id    UUID NOT NULL REFERENCES billing_invoices(id) ON DELETE CASCADE,
  alert_type    TEXT NOT NULL DEFAULT 'overdue' CHECK (alert_type IN ('due_soon', 'overdue')),
  message       TEXT,
  acknowledged  BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (invoice_id, alert_type)
);

CREATE INDEX IF NOT EXISTS idx_billing_alerts_invoice ON billing_alerts(invoice_id);
CREATE INDEX IF NOT EXISTS idx_billing_alerts_ack      ON billing_alerts(acknowledged);

ALTER TABLE billing_alerts ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_alerts' AND policyname='Service role full access on billing_alerts'
  ) THEN
    CREATE POLICY "Service role full access on billing_alerts"
      ON billing_alerts FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;
