"""
Lumynor Billing & Accounts OS
Client project invoicing + product/subscription billing, GST-compliant invoice
PDFs, manual payment tracking, and pending-payment alerts.

Clients (project/service engagements) and Customers (product subscribers) are
deliberately two separate tables/concepts, not one shared "account" — an
invoice bills exactly one or the other (see billing_invoices' CHECK constraint).
"""
import base64
import os
import shutil
from datetime import datetime, timezone, date
from urllib.parse import quote
from db import _sb, get_settings, save_settings

_LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "lumynor-logo-text.png")
_logo_b64_cache = None


def _logo_data_uri() -> str:
    """Base64-embeds the site's actual logo (transparent bg, violet->cyan brand
    gradient wordmark) so the invoice PDF is unmistakably a Lumynor document
    instead of a generic black-on-white template. Cached after first read —
    the file never changes at runtime."""
    global _logo_b64_cache
    if _logo_b64_cache is None:
        try:
            with open(_LOGO_PATH, "rb") as f:
                _logo_b64_cache = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            _logo_b64_cache = ""
    return f"data:image/png;base64,{_logo_b64_cache}" if _logo_b64_cache else ""

# Mirrors the INDIAN_STATES list in Settings.jsx — kept identical so the state
# comparison behind CGST/SGST-vs-IGST is an exact string match, not fuzzy text.
INDIAN_STATES = [
    'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar', 'Chhattisgarh', 'Goa',
    'Gujarat', 'Haryana', 'Himachal Pradesh', 'Jharkhand', 'Karnataka', 'Kerala',
    'Madhya Pradesh', 'Maharashtra', 'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland',
    'Odisha', 'Punjab', 'Rajasthan', 'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura',
    'Uttar Pradesh', 'Uttarakhand', 'West Bengal',
    'Andaman and Nicobar Islands', 'Chandigarh',
    'Dadra and Nagar Haveli and Daman and Diu', 'Delhi', 'Jammu and Kashmir',
    'Ladakh', 'Lakshadweep', 'Puducherry',
]

ORDER_STATUSES = ['draft', 'in_progress', 'delivered', 'invoiced', 'paid', 'overdue', 'cancelled']
INVOICE_STATUSES = ['draft', 'sent', 'paid', 'partially_paid', 'overdue', 'cancelled']
PAYMENT_METHODS = ['bank_transfer', 'upi', 'cash', 'cheque', 'card', 'other']
ALERT_TYPES = ['due_soon', 'overdue']
MANDATE_STATUSES = ['none', 'pending', 'active', 'paused', 'cancelled', 'failed']

_PARTY_FIELDS = {
    "name", "contact_name", "email", "phone", "whatsapp_number",
    "billing_address", "state", "gstin", "notes", "status",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def party_display_name(party: dict) -> str:
    """Company name if set, otherwise the contact person's name — an
    individual client/customer with no company bills in their own name."""
    return (party.get("name") or party.get("contact_name") or "").strip()


def _require_a_name(payload: dict, existing: dict = None) -> None:
    """A client/customer needs a company name OR a contact person name — never
    neither. `existing` lets a partial update check the post-merge state
    instead of just the fields being changed in this call."""
    name = payload.get("name", existing.get("name") if existing else None)
    contact = payload.get("contact_name", existing.get("contact_name") if existing else None)
    if not (name or "").strip() and not (contact or "").strip():
        raise ValueError("Provide a company name or a contact person name")


# ── Clients (project/service engagements) ───────────────────────────────────────

def get_clients(status: str = None) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("billing_clients").select("*").order("name")
    if status:
        q = q.eq("status", status)
    return q.execute().data or []


def get_client(client_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    res = sb.table("billing_clients").select("*").eq("id", client_id).limit(1).execute()
    return res.data[0] if res.data else None


def create_client(data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in data.items() if k in _PARTY_FIELDS}
    _require_a_name(payload)
    payload.setdefault("status", "active")
    res = sb.table("billing_clients").insert(payload).execute()
    return (res.data or [{}])[0]


def update_client(client_id: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in kwargs.items() if k in _PARTY_FIELDS}
    if "name" in payload or "contact_name" in payload:
        _require_a_name(payload, existing=get_client(client_id) or {})
    payload["updated_at"] = _now()
    res = sb.table("billing_clients").update(payload).eq("id", client_id).execute()
    return (res.data or [{}])[0]


def delete_client(client_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("billing_clients").delete().eq("id", client_id).execute()
    return True


# ── Customers (product/subscription billing) ────────────────────────────────────

def get_customers(status: str = None) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("billing_customers").select("*").order("name")
    if status:
        q = q.eq("status", status)
    return q.execute().data or []


def get_customer(customer_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    res = sb.table("billing_customers").select("*").eq("id", customer_id).limit(1).execute()
    return res.data[0] if res.data else None


def create_customer(data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in data.items() if k in _PARTY_FIELDS}
    _require_a_name(payload)
    payload.setdefault("status", "active")
    res = sb.table("billing_customers").insert(payload).execute()
    return (res.data or [{}])[0]


def update_customer(customer_id: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in kwargs.items() if k in _PARTY_FIELDS}
    if "name" in payload or "contact_name" in payload:
        _require_a_name(payload, existing=get_customer(customer_id) or {})
    payload["updated_at"] = _now()
    res = sb.table("billing_customers").update(payload).eq("id", customer_id).execute()
    return (res.data or [{}])[0]


def delete_customer(customer_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("billing_customers").delete().eq("id", customer_id).execute()
    return True


# ── Orders (client engagements — delivery/order status tracking) ────────────────

_ORDER_FIELDS = {
    "client_id", "title", "description", "status", "amount", "currency",
    "start_date", "due_date", "delivered_at", "notes",
}


def get_orders(client_id: str = None, status: str = None) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("billing_orders").select("*").order("created_at", desc=True)
    if client_id:
        q = q.eq("client_id", client_id)
    if status:
        q = q.eq("status", status)
    return q.execute().data or []


def get_order(order_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    res = sb.table("billing_orders").select("*").eq("id", order_id).limit(1).execute()
    return res.data[0] if res.data else None


def create_order(data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in data.items() if k in _ORDER_FIELDS}
    payload.setdefault("status", "draft")
    payload.setdefault("currency", "INR")
    res = sb.table("billing_orders").insert(payload).execute()
    return (res.data or [{}])[0]


def update_order(order_id: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in kwargs.items() if k in _ORDER_FIELDS}
    payload["updated_at"] = _now()
    res = sb.table("billing_orders").update(payload).eq("id", order_id).execute()
    return (res.data or [{}])[0]


def delete_order(order_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("billing_orders").delete().eq("id", order_id).execute()
    return True


# ── Plans (product/subscription catalog — reference data only in v1, no
#    payment gateway is wired) ───────────────────────────────────────────────

_PLAN_FIELDS = {"name", "description", "price", "currency", "billing_interval", "is_active"}


def get_plans(is_active: bool = None) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("billing_plans").select("*").order("name")
    if is_active is not None:
        q = q.eq("is_active", is_active)
    return q.execute().data or []


def create_plan(data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in data.items() if k in _PLAN_FIELDS}
    payload.setdefault("currency", "INR")
    payload.setdefault("billing_interval", "monthly")
    res = sb.table("billing_plans").insert(payload).execute()
    return (res.data or [{}])[0]


def update_plan(plan_id: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in kwargs.items() if k in _PLAN_FIELDS}
    res = sb.table("billing_plans").update(payload).eq("id", plan_id).execute()
    return (res.data or [{}])[0]


# ── Subscriptions ─────────────────────────────────────────────────────────────
# Mandate fields (mandate_status etc.) are stored as provided but nothing here
# ever calls a payment gateway — this is schema + UI scaffolding for the 10+
# year auto-pay mandates, with the actual Razorpay UPI Autopay/e-mandate
# wiring deferred to a Phase 2 once merchant KYC is complete.

_SUBSCRIPTION_FIELDS = {
    "customer_id", "plan_id", "status", "started_at", "current_period_end", "notes",
    "mandate_status", "mandate_provider", "mandate_id", "mandate_max_amount",
    "mandate_start_date", "mandate_end_date", "mandate_frequency", "next_charge_date",
}


def get_subscriptions(customer_id: str = None) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("billing_subscriptions").select("*").order("created_at", desc=True)
    if customer_id:
        q = q.eq("customer_id", customer_id)
    return q.execute().data or []


def get_subscription(subscription_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    res = sb.table("billing_subscriptions").select("*").eq("id", subscription_id).limit(1).execute()
    return res.data[0] if res.data else None


def create_subscription(data: dict) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in data.items() if k in _SUBSCRIPTION_FIELDS}
    payload.setdefault("status", "active")
    payload.setdefault("mandate_status", "none")
    # Phase 2: wire to Razorpay Subscriptions / UPI Autopay e-mandate API here —
    # for now mandate_* fields are just stored as given, no gateway call is made.
    res = sb.table("billing_subscriptions").insert(payload).execute()
    return (res.data or [{}])[0]


def update_subscription(subscription_id: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in kwargs.items() if k in _SUBSCRIPTION_FIELDS}
    payload["updated_at"] = _now()
    res = sb.table("billing_subscriptions").update(payload).eq("id", subscription_id).execute()
    return (res.data or [{}])[0]


# ── GST calculation ──────────────────────────────────────────────────────────

def compute_gst_split(company_state: str, recipient_state: str,
                       taxable_amount: float, gst_rate_percent: float) -> dict:
    """Same state as the company -> CGST+SGST (half the rate each).
    Different state -> IGST (full rate). Split off tax_total - half rather than
    two independent roundings so cgst + sgst always reconciles to tax_total
    exactly, even on odd-cent totals."""
    tax_total = round(taxable_amount * gst_rate_percent / 100, 2)
    same_state = bool(company_state) and bool(recipient_state) and \
        company_state.strip().lower() == recipient_state.strip().lower()
    if same_state:
        half = round(tax_total / 2, 2)
        return {"cgst": half, "sgst": round(tax_total - half, 2), "igst": 0.0}
    return {"cgst": 0.0, "sgst": 0.0, "igst": tax_total}


def _fmt_amount(v) -> str:
    try:
        return f"{float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


# ── Invoices ──────────────────────────────────────────────────────────────────

_INVOICE_UPDATE_FIELDS = {"due_date", "notes"}


def _current_financial_year() -> str:
    """Indian financial year: Apr N -> Mar N+1, formatted '2026-27'."""
    now = datetime.now(timezone.utc)
    start = now.year if now.month >= 4 else now.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def next_invoice_number() -> str:
    """v1 has no DB-level lock on the FY counter — a race between two
    simultaneous invoice creations could hand out the same number. Acceptable
    for single-founder usage; revisit if this ever needs concurrent admins."""
    profile = get_settings("billing_company_profile")
    prefix = profile.get("invoice_number_prefix") or "INV"
    fy = _current_financial_year()
    counters = dict(profile.get("fy_invoice_counters") or {})
    counters[fy] = int(counters.get(fy, 0)) + 1
    profile["fy_invoice_counters"] = counters
    save_settings(profile, "billing_company_profile")
    return f"{prefix}/{fy}/{counters[fy]:04d}"


def get_invoices(client_id: str = None, customer_id: str = None, status: str = None) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("billing_invoices").select("*").order("created_at", desc=True)
    if client_id:
        q = q.eq("client_id", client_id)
    if customer_id:
        q = q.eq("customer_id", customer_id)
    if status:
        q = q.eq("status", status)
    return q.execute().data or []


def get_invoice(invoice_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    res = sb.table("billing_invoices").select("*").eq("id", invoice_id).limit(1).execute()
    if not res.data:
        return None
    invoice = res.data[0]
    items = sb.table("billing_invoice_items").select("*") \
        .eq("invoice_id", invoice_id).order("sort_order").execute()
    payments = sb.table("billing_payments").select("*") \
        .eq("invoice_id", invoice_id).order("paid_at").execute()
    invoice["items"] = items.data or []
    invoice["payments"] = payments.data or []
    return invoice


def create_invoice(client_id: str = None, customer_id: str = None, order_id: str = None,
                    subscription_id: str = None, items: list = None, due_date: str = None,
                    issue_date: str = None, notes: str = None) -> dict:
    """Bills exactly one of client_id/customer_id. Resolves the recipient's
    GSTIN/state and snapshots both parties' GST facts onto the invoice at
    creation time, so a later company-profile edit never rewrites a historical
    invoice's tax numbers."""
    sb = _sb()
    if not sb:
        return {}
    if bool(client_id) == bool(customer_id):
        raise ValueError("create_invoice requires exactly one of client_id or customer_id")

    recipient = get_client(client_id) if client_id else get_customer(customer_id)
    if not recipient:
        raise ValueError("Recipient not found")

    profile = get_settings("billing_company_profile")
    company_state = profile.get("state", "")
    company_gstin = profile.get("gstin", "")
    recipient_state = recipient.get("state", "")
    recipient_gstin = recipient.get("gstin", "")

    line_rows = []
    taxable_total = cgst_total = sgst_total = igst_total = 0.0
    for idx, item in enumerate(items or []):
        qty = float(item.get("quantity", 1) or 1)
        unit_price = float(item.get("unit_price", 0) or 0)
        discount_percent = float(item.get("discount_percent", 0) or 0)
        gross = qty * unit_price
        discount_amount = round(gross * discount_percent / 100, 2)
        taxable_amount = round(gross - discount_amount, 2)
        gst_rate = float(item.get("gst_rate_percent", 18) or 0)
        split = compute_gst_split(company_state, recipient_state, taxable_amount, gst_rate)
        line_total = round(taxable_amount + split["cgst"] + split["sgst"] + split["igst"], 2)
        line_rows.append({
            "description":      item.get("description", ""),
            "hsn_sac_code":     item.get("hsn_sac_code", ""),
            "quantity":         qty,
            "unit_price":       unit_price,
            "discount_percent": discount_percent,
            "discount_amount":  discount_amount,
            "taxable_amount":   taxable_amount,
            "gst_rate_percent": gst_rate,
            "cgst_amount":      split["cgst"],
            "sgst_amount":      split["sgst"],
            "igst_amount":      split["igst"],
            "line_total":       line_total,
            "sort_order":       idx,
        })
        taxable_total += taxable_amount
        cgst_total += split["cgst"]
        sgst_total += split["sgst"]
        igst_total += split["igst"]

    total_tax = round(cgst_total + sgst_total + igst_total, 2)
    total = round(taxable_total + total_tax, 2)

    invoice_payload = {
        "client_id":             client_id,
        "customer_id":           customer_id,
        "order_id":              order_id,
        "subscription_id":       subscription_id,
        "invoice_number":        next_invoice_number(),
        "status":                "draft",
        "issue_date":            issue_date or datetime.now(timezone.utc).date().isoformat(),
        "due_date":              due_date,
        "currency":              "INR",
        "company_gstin":         company_gstin,
        "company_state":         company_state,
        "recipient_gstin":       recipient_gstin,
        "recipient_state":       recipient_state,
        "place_of_supply_state": recipient_state,
        "taxable_amount":        round(taxable_total, 2),
        "cgst_amount":           round(cgst_total, 2),
        "sgst_amount":           round(sgst_total, 2),
        "igst_amount":           round(igst_total, 2),
        "total_tax":             total_tax,
        "total":                 total,
        "amount_paid":           0,
        "notes":                 notes or "",
    }
    res = sb.table("billing_invoices").insert(invoice_payload).execute()
    invoice = (res.data or [{}])[0]
    if not invoice.get("id"):
        return invoice

    for row in line_rows:
        row["invoice_id"] = invoice["id"]
    if line_rows:
        sb.table("billing_invoice_items").insert(line_rows).execute()

    if order_id:
        update_order(order_id, status="invoiced")

    return get_invoice(invoice["id"])


def update_invoice(invoice_id: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in kwargs.items() if k in _INVOICE_UPDATE_FIELDS}
    payload["updated_at"] = _now()
    sb.table("billing_invoices").update(payload).eq("id", invoice_id).execute()
    return get_invoice(invoice_id)


def delete_invoice(invoice_id: str) -> bool:
    """Only draft invoices can be deleted — anything sent/paid is a real
    financial record, not scratch data."""
    sb = _sb()
    if not sb:
        return False
    invoice = get_invoice(invoice_id)
    if not invoice or invoice.get("status") != "draft":
        raise ValueError("Only draft invoices can be deleted")
    sb.table("billing_invoices").delete().eq("id", invoice_id).execute()
    return True


def mark_invoice_sent(invoice_id: str) -> dict:
    sb = _sb()
    if not sb:
        return {}
    now = _now()
    sb.table("billing_invoices").update(
        {"status": "sent", "sent_at": now, "updated_at": now}
    ).eq("id", invoice_id).execute()
    return get_invoice(invoice_id)


# ── PDF generation (Playwright HTML->PDF — reuses the exact tool design_audit.py
#    already uses for screenshotting; no new PDF library needed) ────────────────

def _find_system_chromium() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def build_invoice_html(invoice: dict, company_profile: dict, recipient: dict) -> str:
    """Branded to match the actual site (lumynor-website/tailwind.config.js):
    accent #7c3aed / secondary #00f0ff brand gradient, Space Grotesk for display
    text, DM Sans for body — same fonts and colors a visitor sees on the site,
    just on a white page since a printed/emailed legal document needs to stay
    printable and readable in any mail client, not a dark UI surface."""
    items = invoice.get("items", [])
    is_interstate = float(invoice.get("igst_amount") or 0) > 0
    is_paid = invoice.get("status") == "paid"
    balance_due = float(invoice.get("total") or 0) - float(invoice.get("amount_paid") or 0)

    rows_html = "".join(f"""
        <tr>
          <td class="mono muted">{i + 1:02d}</td>
          <td>{it.get('description', '')}</td>
          <td class="mono muted">{it.get('hsn_sac_code', '') or '—'}</td>
          <td class="num">{it.get('quantity', '')}</td>
          <td class="num">₹{_fmt_amount(it.get('unit_price'))}</td>
          <td class="num muted">{_fmt_amount(it.get('discount_percent'))}%</td>
          <td class="num">₹{_fmt_amount(it.get('taxable_amount'))}</td>
          <td class="num muted">{_fmt_amount(it.get('gst_rate_percent'))}%</td>
          <td class="num">₹{_fmt_amount((it.get('cgst_amount') or 0) + (it.get('sgst_amount') or 0) + (it.get('igst_amount') or 0))}</td>
          <td class="num strong">₹{_fmt_amount(it.get('line_total'))}</td>
        </tr>
    """ for i, it in enumerate(items))

    tax_rows = (
        f'<div class="totrow"><span>IGST</span><span>₹{_fmt_amount(invoice.get("igst_amount"))}</span></div>'
        if is_interstate else
        f'<div class="totrow"><span>CGST</span><span>₹{_fmt_amount(invoice.get("cgst_amount"))}</span></div>'
        f'<div class="totrow"><span>SGST</span><span>₹{_fmt_amount(invoice.get("sgst_amount"))}</span></div>'
    )

    bank = ""
    if company_profile.get("bank_account_number"):
        upi = f'<div class="totrow"><span>UPI</span><span>{company_profile.get("upi_id")}</span></div>' if company_profile.get("upi_id") else ""
        bank = f"""
          <div class="panel">
            <div class="panel-label">Payment Details</div>
            <div class="totrow"><span>Account Name</span><span>{company_profile.get('bank_account_name', '')}</span></div>
            <div class="totrow"><span>Bank</span><span>{company_profile.get('bank_name', '')}</span></div>
            <div class="totrow"><span>Account No.</span><span class="mono">{company_profile.get('bank_account_number', '')}</span></div>
            <div class="totrow"><span>IFSC</span><span class="mono">{company_profile.get('bank_ifsc', '')}</span></div>
            {upi}
          </div>
        """

    balance_html = (
        '<div class="totrow balance paid"><span>Balance Due</span><span>PAID IN FULL</span></div>'
        if is_paid else
        f'<div class="totrow balance"><span>Balance Due</span><span>₹{_fmt_amount(balance_due)}</span></div>'
    )

    logo = _logo_data_uri()
    logo_html = f'<img class="logo" src="{logo}" alt="Lumynor Systems" />' if logo else '<div class="logo-fallback">LUMYNOR</div>'

    notes_html = f'<div class="panel"><div class="panel-label">Notes</div><p class="muted">{invoice.get("notes", "")}</p></div>' if invoice.get("notes") else ""

    return f"""<!doctype html><html><head><meta charset="utf-8"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{ --accent: #7c3aed; --secondary: #00f0ff; --ink: #16181d; --muted: #6b7280; --line: #e6e4ee; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'DM Sans', -apple-system, Helvetica, Arial, sans-serif; color: var(--ink); margin: 0; font-size: 12.5px; }}
  .accent-bar {{ height: 6px; background: linear-gradient(90deg, var(--secondary), var(--accent)); }}
  .page {{ padding: 36px 44px 44px; }}
  h1, .display {{ font-family: 'Space Grotesk', 'DM Sans', sans-serif; }}
  .muted {{ color: var(--muted); }}
  .mono {{ font-family: 'SF Mono', Menlo, monospace; }}
  .strong {{ font-weight: 700; }}

  .header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 30px; }}
  .logo {{ height: 34px; }}
  .logo-fallback {{ font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 22px;
    background: linear-gradient(90deg, var(--accent), var(--secondary)); -webkit-background-clip: text; color: transparent; }}
  .company-meta {{ margin-top: 10px; font-size: 11px; line-height: 1.6; color: var(--muted); }}
  .invoice-meta {{ text-align: right; }}
  .invoice-kicker {{ text-transform: uppercase; letter-spacing: 0.14em; font-size: 10px; font-weight: 700; color: var(--accent); }}
  .invoice-number {{ font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 22px; margin: 4px 0 8px; }}
  .invoice-dates {{ font-size: 11px; line-height: 1.6; color: var(--muted); }}

  .divider {{ height: 1px; background: var(--line); margin: 0 0 24px; }}

  .bill-to-label {{ text-transform: uppercase; letter-spacing: 0.1em; font-size: 9.5px; font-weight: 700; color: var(--accent); margin-bottom: 6px; }}
  .bill-to-name {{ font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 15px; margin-bottom: 3px; }}
  .bill-to-detail {{ font-size: 11.5px; color: var(--muted); line-height: 1.6; }}

  table {{ width: 100%; border-collapse: collapse; margin: 26px 0 20px; }}
  thead th {{ text-align: left; text-transform: uppercase; letter-spacing: 0.06em; font-size: 8.5px; font-weight: 700;
    color: var(--accent); background: rgba(124,58,237,0.06); padding: 9px 10px; border-bottom: 1.5px solid var(--accent); }}
  th.num, td.num {{ text-align: right; }}
  tbody td {{ padding: 9px 10px; border-bottom: 1px solid var(--line); font-size: 11.5px; vertical-align: top; }}
  tbody tr:last-child td {{ border-bottom: 1.5px solid var(--ink); }}

  .totals-row {{ display: flex; justify-content: flex-end; }}
  .totals {{ width: 280px; }}
  .totrow {{ display: flex; justify-content: space-between; padding: 5px 0; font-size: 11.5px; }}
  .totrow.grand {{ font-weight: 700; font-size: 14px; border-top: 1.5px solid var(--ink); padding-top: 10px; margin-top: 6px; }}
  .totrow.balance {{ font-weight: 700; font-size: 13px; color: #dc2626; background: rgba(220,38,38,0.06); padding: 8px 10px; margin-top: 8px; }}
  .totrow.balance.paid {{ color: #059669; background: rgba(5,150,105,0.08); }}

  .panel {{ margin-top: 22px; padding: 14px 16px; background: #fafafa; border: 1px solid var(--line); max-width: 320px; }}
  .panel-label {{ text-transform: uppercase; letter-spacing: 0.1em; font-size: 9px; font-weight: 700; color: var(--accent); margin-bottom: 8px; }}
  .panel .totrow {{ font-size: 11px; }}

  .footer {{ margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--line); font-size: 9.5px; color: var(--muted); text-align: center; }}
</style></head>
<body>
  <div class="accent-bar"></div>
  <div class="page">
    <div class="header">
      <div>
        {logo_html}
        <div class="company-meta">
          {company_profile.get('legal_name', '')}<br/>
          {company_profile.get('address', '')}<br/>
          {company_profile.get('state', '')}
          {('&nbsp;·&nbsp;GSTIN ' + company_profile.get('gstin', '')) if company_profile.get('gstin') else ''}
        </div>
      </div>
      <div class="invoice-meta">
        <div class="invoice-kicker">Tax Invoice</div>
        <div class="invoice-number mono">{invoice.get('invoice_number', '')}</div>
        <div class="invoice-dates">
          Issue Date: {invoice.get('issue_date', '')}<br/>
          Due Date: {invoice.get('due_date', '')}
        </div>
      </div>
    </div>

    <div class="divider"></div>

    <div>
      <div class="bill-to-label">Billed To</div>
      <div class="bill-to-name">{party_display_name(recipient)}</div>
      <div class="bill-to-detail">
        {recipient.get('billing_address', '')}<br/>
        {recipient.get('state', '')}
        {('&nbsp;·&nbsp;GSTIN ' + recipient.get('gstin', '')) if recipient.get('gstin') else ''}
      </div>
    </div>

    <table>
      <thead><tr>
        <th style="width:28px">#</th><th>Description</th><th>HSN/SAC</th><th class="num">Qty</th>
        <th class="num">Rate</th><th class="num">Disc.</th><th class="num">Taxable</th>
        <th class="num">GST</th><th class="num">Tax</th><th class="num">Total</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>

    <div class="totals-row">
      <div class="totals">
        <div class="totrow"><span>Taxable Amount</span><span>₹{_fmt_amount(invoice.get('taxable_amount'))}</span></div>
        {tax_rows}
        <div class="totrow grand"><span>Total</span><span>₹{_fmt_amount(invoice.get('total'))}</span></div>
        <div class="totrow"><span class="muted">Amount Paid</span><span>₹{_fmt_amount(invoice.get('amount_paid'))}</span></div>
        {balance_html}
      </div>
    </div>

    {bank}
    {notes_html}

    <div class="footer">{company_profile.get('legal_name', 'Lumynor Systems')} · Generated via the Billing &amp; Accounts OS</div>
  </div>
</body></html>"""


def get_payments(invoice_id: str = None) -> list:
    sb = _sb()
    if not sb:
        return []
    q = sb.table("billing_payments").select("*").order("paid_at", desc=True)
    if invoice_id:
        q = q.eq("invoice_id", invoice_id)
    return q.execute().data or []


def record_payment(invoice_id: str, amount: float, paid_at: str, method: str = "bank_transfer",
                    reference_note: str = None, recorded_by: str = None) -> dict:
    """Inserts the payment, recomputes amount_paid from the sum of all payments
    against this invoice (source of truth is the payments table, not a running
    counter, so this stays correct even if a payment is later deleted by hand
    in Supabase), and syncs the invoice + its linked order once fully settled."""
    sb = _sb()
    if not sb:
        return {}
    invoice = get_invoice(invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")

    sb.table("billing_payments").insert({
        "invoice_id":     invoice_id,
        "amount":         float(amount),
        "paid_at":        paid_at,
        "method":         method,
        "reference_note": reference_note or "",
        "recorded_by":    recorded_by or "",
    }).execute()

    payments = get_payments(invoice_id)
    amount_paid = round(sum(float(p.get("amount") or 0) for p in payments), 2)
    total = float(invoice.get("total") or 0)
    new_status = "paid" if amount_paid >= total else ("partially_paid" if amount_paid > 0 else invoice["status"])

    sb.table("billing_invoices").update({
        "amount_paid": amount_paid,
        "status":      new_status,
        "updated_at":  _now(),
    }).eq("id", invoice_id).execute()

    if new_status == "paid" and invoice.get("order_id"):
        update_order(invoice["order_id"], status="paid")

    return get_invoice(invoice_id)


# ── Reminders & delivery (manual-assisted WhatsApp, real email send) ────────────
# WhatsApp reminders stay manual-assisted, not automated: the only WhatsApp send
# capability in this codebase (atlas_brain.send_whatsapp, Twilio) is a personal
# sandbox tied to the founder's own phone with a 24h session window — not usable
# for arbitrary client numbers. So this drafts the message and hands back a
# pre-filled wa.me link for the founder to review and send themselves, same
# pattern as WhatsAppFloating.jsx.

def get_recipient(invoice: dict) -> dict | None:
    """The client or customer this invoice bills — whichever is set."""
    if invoice.get("client_id"):
        return get_client(invoice["client_id"])
    if invoice.get("customer_id"):
        return get_customer(invoice["customer_id"])
    return None


def build_reminder_wa_link(invoice: dict, recipient: dict) -> str:
    number = (recipient.get("whatsapp_number") or "").strip()
    name = recipient.get("contact_name") or recipient.get("name") or ""
    balance = round(float(invoice.get("total") or 0) - float(invoice.get("amount_paid") or 0), 2)
    text = (
        f"Hi {name}, this is a reminder that invoice {invoice.get('invoice_number')} "
        f"for ₹{_fmt_amount(balance)} was due on {invoice.get('due_date')}. "
        f"Please let us know once it's paid. Thank you — Lumynor Systems"
    )
    return f"https://wa.me/{number}?text={quote(text)}"


def build_invoice_email_body(invoice: dict, recipient: dict) -> tuple[str, str]:
    name = recipient.get("contact_name") or recipient.get("name") or ""
    subject = f"Invoice {invoice.get('invoice_number')} from Lumynor Systems"
    body = (
        f"Hi {name},\n\n"
        f"Please find attached invoice {invoice.get('invoice_number')} for "
        f"₹{_fmt_amount(invoice.get('total'))}, due {invoice.get('due_date')}.\n\n"
        f"Thanks,\nLumynor Systems"
    )
    return subject, body


# ── Pending-payment alerts ───────────────────────────────────────────────────
# Materialized rows (billing_alerts), not a live query — the UI needs a stable
# badge count and a dismiss/acknowledge state, same reasoning as lumy_reminders.

def get_overdue_invoices() -> list:
    sb = _sb()
    if not sb:
        return []
    today = date.today().isoformat()
    return sb.table("billing_invoices").select("*") \
        .lt("due_date", today) \
        .not_.in_("status", ["paid", "cancelled"]) \
        .execute().data or []


def get_due_soon_invoices(days: int = 3) -> list:
    sb = _sb()
    if not sb:
        return []
    today = date.today().isoformat()
    from datetime import timedelta
    horizon = (date.today() + timedelta(days=days)).isoformat()
    return sb.table("billing_invoices").select("*") \
        .gte("due_date", today).lte("due_date", horizon) \
        .not_.in_("status", ["paid", "cancelled"]) \
        .execute().data or []


def refresh_billing_alerts() -> dict:
    """Daemon entry point. Upserts on (invoice_id, alert_type) so re-running
    every 60s never duplicates a row, and flips overdue invoices/orders."""
    sb = _sb()
    if not sb:
        return {"overdue": 0, "due_soon": 0}

    overdue = get_overdue_invoices()
    for inv in overdue:
        sb.table("billing_alerts").upsert({
            "invoice_id":   inv["id"],
            "alert_type":   "overdue",
            "message":      f"Invoice {inv['invoice_number']} is overdue (due {inv['due_date']}).",
            "acknowledged": False,
        }, on_conflict="invoice_id,alert_type").execute()
        if inv["status"] != "overdue":
            sb.table("billing_invoices").update({"status": "overdue", "updated_at": _now()}) \
                .eq("id", inv["id"]).execute()
            if inv.get("order_id"):
                update_order(inv["order_id"], status="overdue")

    due_soon = get_due_soon_invoices()
    for inv in due_soon:
        sb.table("billing_alerts").upsert({
            "invoice_id":   inv["id"],
            "alert_type":   "due_soon",
            "message":      f"Invoice {inv['invoice_number']} is due {inv['due_date']}.",
            "acknowledged": False,
        }, on_conflict="invoice_id,alert_type").execute()

    return {"overdue": len(overdue), "due_soon": len(due_soon)}


def get_active_alerts() -> list:
    sb = _sb()
    if not sb:
        return []
    return sb.table("billing_alerts").select("*, billing_invoices(invoice_number,total,due_date)") \
        .eq("acknowledged", False).order("created_at", desc=True).execute().data or []


def acknowledge_alert(alert_id: str) -> bool:
    sb = _sb()
    if not sb:
        return False
    sb.table("billing_alerts").update({"acknowledged": True}).eq("id", alert_id).execute()
    return True


def generate_invoice_pdf(invoice_id: str) -> bytes:
    from playwright.sync_api import sync_playwright

    invoice = get_invoice(invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")
    recipient = get_client(invoice["client_id"]) if invoice.get("client_id") else get_customer(invoice["customer_id"])
    company_profile = get_settings("billing_company_profile")
    html = build_invoice_html(invoice, company_profile, recipient or {})

    chromium_path = _find_system_chromium()
    with sync_playwright() as pw:
        launch_kwargs = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        }
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        pdf_bytes = page.pdf(format="A4", print_background=True,
                              margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        browser.close()
    return pdf_bytes
