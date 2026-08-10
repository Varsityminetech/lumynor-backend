-- ─────────────────────────────────────────────────────────────────────────────
-- Lumynor Systems — Billing OS: Credit Notes
-- Run this in: Supabase Dashboard → SQL Editor → New query
-- Safe to re-run
--
-- Invoices are immutable once sent/paid (billing.py's delete_invoice only
-- allows drafts) — correct, since a sent invoice is a real financial record.
-- Credit notes are the GST-compliant way to correct one afterward: their own
-- numbered document, referencing the original invoice, reducing what's owed
-- without ever rewriting the invoice itself.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS billing_credit_notes (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id          UUID NOT NULL REFERENCES billing_invoices(id) ON DELETE CASCADE,
  credit_note_number  TEXT NOT NULL UNIQUE,
  issue_date          DATE,
  reason              TEXT NOT NULL,
  currency            TEXT NOT NULL DEFAULT 'INR',
  taxable_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
  cgst_amount         NUMERIC(12,2) NOT NULL DEFAULT 0,
  sgst_amount         NUMERIC(12,2) NOT NULL DEFAULT 0,
  igst_amount         NUMERIC(12,2) NOT NULL DEFAULT 0,
  total_tax           NUMERIC(12,2) NOT NULL DEFAULT 0,
  total               NUMERIC(12,2) NOT NULL DEFAULT 0,
  notes               TEXT,
  created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_credit_notes_invoice ON billing_credit_notes(invoice_id);

ALTER TABLE billing_credit_notes ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_credit_notes' AND policyname='Service role full access on billing_credit_notes'
  ) THEN
    CREATE POLICY "Service role full access on billing_credit_notes"
      ON billing_credit_notes FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS billing_credit_note_items (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  credit_note_id    UUID NOT NULL REFERENCES billing_credit_notes(id) ON DELETE CASCADE,
  description       TEXT NOT NULL,
  hsn_sac_code      TEXT,
  quantity          NUMERIC(12,2) NOT NULL DEFAULT 1,
  unit_price        NUMERIC(12,2) NOT NULL DEFAULT 0,
  taxable_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
  gst_rate_percent  NUMERIC(5,2) NOT NULL DEFAULT 18,
  cgst_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
  sgst_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
  igst_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
  line_total        NUMERIC(12,2) NOT NULL DEFAULT 0,
  sort_order        INTEGER NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_credit_note_items_note ON billing_credit_note_items(credit_note_id);

ALTER TABLE billing_credit_note_items ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename='billing_credit_note_items' AND policyname='Service role full access on billing_credit_note_items'
  ) THEN
    CREATE POLICY "Service role full access on billing_credit_note_items"
      ON billing_credit_note_items FOR ALL USING (auth.role() = 'service_role');
  END IF;
END $$;

-- Source of truth for "how much of this invoice has been credited" is the
-- credit_notes table (same pattern as amount_paid / billing_payments) — this
-- column is a recomputed cache, not independently editable.
ALTER TABLE billing_invoices ADD COLUMN IF NOT EXISTS amount_credited NUMERIC(12,2) NOT NULL DEFAULT 0;
