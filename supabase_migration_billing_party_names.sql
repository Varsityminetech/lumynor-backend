-- ─────────────────────────────────────────────────────────────────────────────
-- Lumynor Systems — Billing OS: simplify client/customer naming
-- Run this in: Supabase Dashboard → SQL Editor → New query
-- Safe to re-run
--
-- Drops the separate "Legal Name" concept — going forward a client/customer
-- has just a company name and a contact person name. An individual with no
-- company leaves the company name blank; invoices then bill in the contact
-- person's name instead (see billing.py's party_display_name()).
--
-- `name` was NOT NULL, which no longer fits: an individual-only party can
-- have it blank. Loosening the constraint, not dropping data — the existing
-- `legal_name` column is left in place (unused going forward) rather than
-- dropped, since dropping a column is a one-way trip and there's no need to
-- force that risk for a column the app just stops writing to.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE billing_clients ALTER COLUMN name DROP NOT NULL;
ALTER TABLE billing_customers ALTER COLUMN name DROP NOT NULL;
