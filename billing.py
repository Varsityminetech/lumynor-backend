"""
Lumynor Billing & Accounts OS
Client project invoicing + product/subscription billing, GST-compliant invoice
PDFs, manual payment tracking, and pending-payment alerts.

Clients (project/service engagements) and Customers (product subscribers) are
deliberately two separate tables/concepts, not one shared "account" — an
invoice bills exactly one or the other (see billing_invoices' CHECK constraint).
"""
from datetime import datetime, timezone
from db import _sb

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
