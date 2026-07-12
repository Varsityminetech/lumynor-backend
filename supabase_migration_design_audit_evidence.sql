-- UXRay: persist the MEASURED evidence behind each audit.
--
-- Without these columns the audit still works (save_audit falls back to the legacy
-- columns), but an unlocked report loses all provenance — it can't show that we
-- opened the site on a 390px phone, computed real WCAG contrast ratios, or that a
-- vision model actually looked at the screenshots.
--
-- Safe to run more than once.

ALTER TABLE design_audits ADD COLUMN IF NOT EXISTS evidence      JSONB DEFAULT '{}'::jsonb;
ALTER TABLE design_audits ADD COLUMN IF NOT EXISTS pagespeed     JSONB DEFAULT '{}'::jsonb;
ALTER TABLE design_audits ADD COLUMN IF NOT EXISTS render_method TEXT;
ALTER TABLE design_audits ADD COLUMN IF NOT EXISTS mode          TEXT DEFAULT 'lumynor';
