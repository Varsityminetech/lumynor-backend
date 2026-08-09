"""
Lumynor Billing & Accounts OS
Client project invoicing + product/subscription billing, GST-compliant invoice
PDFs, manual payment tracking, and pending-payment alerts.

Clients (project/service engagements) and Customers (product subscribers) are
deliberately two separate tables/concepts, not one shared "account" — an
invoice bills exactly one or the other (see billing_invoices' CHECK constraint).
"""
import shutil
from datetime import datetime, timezone
from db import _sb, get_settings, save_settings

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
    "name", "legal_name", "contact_name", "email", "phone", "whatsapp_number",
    "billing_address", "state", "gstin", "notes", "status",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    payload.setdefault("status", "active")
    res = sb.table("billing_clients").insert(payload).execute()
    return (res.data or [{}])[0]


def update_client(client_id: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in kwargs.items() if k in _PARTY_FIELDS}
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
    payload.setdefault("status", "active")
    res = sb.table("billing_customers").insert(payload).execute()
    return (res.data or [{}])[0]


def update_customer(customer_id: str, **kwargs) -> dict:
    sb = _sb()
    if not sb:
        return {}
    payload = {k: v for k, v in kwargs.items() if k in _PARTY_FIELDS}
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
    items = invoice.get("items", [])
    is_interstate = float(invoice.get("igst_amount") or 0) > 0

    rows_html = "".join(f"""
        <tr>
          <td>{i + 1}</td>
          <td>{it.get('description', '')}</td>
          <td>{it.get('hsn_sac_code', '') or '-'}</td>
          <td class="num">{it.get('quantity', '')}</td>
          <td class="num">₹{_fmt_amount(it.get('unit_price'))}</td>
          <td class="num">{_fmt_amount(it.get('discount_percent'))}%</td>
          <td class="num">₹{_fmt_amount(it.get('taxable_amount'))}</td>
          <td class="num">{_fmt_amount(it.get('gst_rate_percent'))}%</td>
          <td class="num">₹{_fmt_amount((it.get('cgst_amount') or 0) + (it.get('sgst_amount') or 0) + (it.get('igst_amount') or 0))}</td>
          <td class="num">₹{_fmt_amount(it.get('line_total'))}</td>
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
        upi = f' · UPI: {company_profile.get("upi_id")}' if company_profile.get("upi_id") else ""
        bank = f"""
          <div class="bank">
            <strong>Bank Details</strong><br/>
            {company_profile.get('bank_account_name', '')}<br/>
            {company_profile.get('bank_name', '')} · A/C {company_profile.get('bank_account_number', '')}<br/>
            IFSC: {company_profile.get('bank_ifsc', '')}{upi}
          </div>
        """

    balance_due = float(invoice.get("total") or 0) - float(invoice.get("amount_paid") or 0)
    notes_html = f'<div class="meta" style="margin-top:20px;">{invoice.get("notes", "")}</div>' if invoice.get("notes") else ""

    return f"""<!doctype html><html><head><meta charset="utf-8"/>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; color: #1a1a1a; padding: 40px; font-size: 12px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .row {{ display: flex; justify-content: space-between; margin-bottom: 24px; }}
  .block {{ max-width: 45%; }}
  .label {{ text-transform: uppercase; font-size: 9px; letter-spacing: 0.08em; color: #888; margin-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; font-size: 11px; }}
  th {{ background: #f4f4f4; text-transform: uppercase; font-size: 9px; letter-spacing: 0.04em; }}
  td.num, th.num {{ text-align: right; }}
  .totals {{ margin-top: 16px; margin-left: auto; width: 280px; }}
  .totrow {{ display: flex; justify-content: space-between; padding: 4px 0; }}
  .grand {{ font-weight: 700; font-size: 14px; border-top: 2px solid #1a1a1a; padding-top: 8px; margin-top: 4px; }}
  .bank {{ margin-top: 32px; font-size: 11px; color: #444; }}
  .meta {{ font-size: 11px; color: #444; }}
</style></head>
<body>
  <div class="row">
    <div class="block">
      <h1>{company_profile.get('legal_name', '')}</h1>
      <div class="meta">{company_profile.get('address', '')}<br/>
      {company_profile.get('state', '')}<br/>
      {('GSTIN: ' + company_profile.get('gstin', '')) if company_profile.get('gstin') else ''}</div>
    </div>
    <div class="block" style="text-align:right;">
      <div class="label">Invoice</div>
      <h1>{invoice.get('invoice_number', '')}</h1>
      <div class="meta">Issue Date: {invoice.get('issue_date', '')}<br/>Due Date: {invoice.get('due_date', '')}</div>
    </div>
  </div>

  <div class="row">
    <div class="block">
      <div class="label">Billed To</div>
      <strong>{recipient.get('legal_name') or recipient.get('name', '')}</strong><br/>
      {recipient.get('billing_address', '')}<br/>
      {recipient.get('state', '')}<br/>
      {('GSTIN: ' + recipient.get('gstin', '')) if recipient.get('gstin') else ''}
    </div>
  </div>

  <table>
    <thead><tr>
      <th>#</th><th>Description</th><th>HSN/SAC</th><th class="num">Qty</th>
      <th class="num">Rate</th><th class="num">Disc.</th><th class="num">Taxable</th>
      <th class="num">GST</th><th class="num">Tax Amt</th><th class="num">Total</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>

  <div class="totals">
    <div class="totrow"><span>Taxable Amount</span><span>₹{_fmt_amount(invoice.get('taxable_amount'))}</span></div>
    {tax_rows}
    <div class="totrow grand"><span>Total</span><span>₹{_fmt_amount(invoice.get('total'))}</span></div>
    <div class="totrow"><span>Amount Paid</span><span>₹{_fmt_amount(invoice.get('amount_paid'))}</span></div>
    <div class="totrow" style="font-weight:700;"><span>Balance Due</span><span>₹{_fmt_amount(balance_due)}</span></div>
  </div>

  {bank}
  {notes_html}
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
