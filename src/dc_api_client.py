"""Looks up an order on Helm (the DC/warehouse platform) via Helm's own
Public API -- https://<tenant>.myhelm.app/public-api -- instead of scraping
the logged-in dashboard with a headless browser (helm_order.py). A couple of
fast REST calls per lookup instead of two full page loads + DOM reads.

Auth: POST /auth/login with {email, password} returns a JWT Bearer token
that (per Helm's own docs) "remains valid until invalidated by the server" --
no fixed expiry to track, unlike Evri's. Cached in-memory per process; a 401
on any call means the cached token went stale, so it's cleared and the call
retried once with a fresh login.

Order lookup: GET /orders?filters[search_field]=search_order_id&filters[search]=<order_number>
(confirmed live -- returns an exact single match, not a substring search).
The DC status itself (id -> name, e.g. 5 -> "Despatched") comes from a
separate GET /statuses list, cached after the first call. Tracking code/URL
live on the matching /shipments record instead of the order record (same
filter syntax, keyed by the shipment's own label_reference field, which is
the Amazon order number).
"""

from __future__ import annotations

import os
from datetime import date as date_type, timedelta

import requests
from dotenv import load_dotenv

LOGIN_PATH = "/auth/login"
ORDERS_PATH = "/orders"
SHIPMENTS_PATH = "/shipments"
STATUSES_PATH = "/statuses"
PURCHASE_ORDERS_PATH = "/purchase_orders"
INVENTORY_PATH = "/inventory"

# The label a PO's own status_description must match (case-insensitive) to
# count as append-worthy evidence for the cancellation tracker sheet --
# Draft/On Hold/Cancelled/Archived/Processing/Partially Completed/Completed
# are all NOT submitted-as-evidence.
SUBMITTED_PO_LABEL = "submitted"

_cached_token: str | None = None
_status_names_by_id: dict[int, str] | None = None


def _config() -> tuple[str, str, str]:
    load_dotenv(override=True)
    base_url = os.getenv("DC_API_BASE_URL", "").strip()
    email = os.getenv("DC_EMAIL", "").strip()
    password = os.getenv("DC_PASSWORD", "").strip()
    if not all([base_url, email, password]):
        raise RuntimeError("DC_API_BASE_URL / DC_EMAIL / DC_PASSWORD must all be set in .env to use the Helm Public API.")
    return base_url, email, password


def _login() -> str:
    base_url, email, password = _config()
    response = requests.post(
        base_url + LOGIN_PATH,
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["token"]


def _get(path: str, params: dict | None = None, _retried: bool = False) -> dict:
    global _cached_token
    base_url = _config()[0]
    if _cached_token is None:
        _cached_token = _login()

    response = requests.get(
        base_url + path,
        headers={"Authorization": f"Bearer {_cached_token}", "Accept": "application/json"},
        params=params,
        timeout=20,
    )
    if response.status_code == 401 and not _retried:
        _cached_token = None
        return _get(path, params=params, _retried=True)
    response.raise_for_status()
    return response.json()


def _post(path: str, _retried: bool = False) -> dict:
    global _cached_token
    base_url = _config()[0]
    if _cached_token is None:
        _cached_token = _login()

    response = requests.post(
        base_url + path,
        headers={"Authorization": f"Bearer {_cached_token}", "Accept": "application/json"},
        timeout=20,
    )
    if response.status_code == 401 and not _retried:
        _cached_token = None
        return _post(path, _retried=True)
    response.raise_for_status()
    return response.json()


def _status_name(status_id) -> str | None:
    """Order status id -> name (e.g. 5 -> "Despatched"), the same strings
    asana_task_formatter.py's _SENT_STATUSES/_NOT_SENT_STATUSES already
    expect -- confirmed live against the real /statuses list. Fetched once
    per process and cached; there are only 46 of them and they don't change
    at runtime."""
    global _status_names_by_id
    if _status_names_by_id is None:
        body = _get(STATUSES_PATH)
        _status_names_by_id = {s["id"]: s["name"] for s in body.get("data", []) if s.get("type") == "order"}
    return _status_names_by_id.get(status_id)


def _search_one(path: str, order_number: str) -> dict | None:
    params = {"filters[search_field]": "search_order_id", "filters[search]": order_number}
    body = _get(path, params=params)
    data = body.get("data") or []
    return data[0] if data else None


def _norm(value) -> str | None:
    """Flattens every field to either a trimmed string or None -- the API
    returns empty strings/nulls/raw numbers for "not set" fields that the
    browser-scraped version (helm_order._field/_text) always returned as a
    clean trimmed string or None, so downstream code's `if part`/`or None`
    checks keep working identically either way."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def get_dc_order(order_number: str) -> dict | None:
    """Same return shape as helm_order.get_dc_order (the browser-scraped
    version) -- a drop-in replacement backed by Helm's real Public API.
    Returns None if the order isn't found there at all."""
    order = _search_one(ORDERS_PATH, order_number)
    if order is None:
        return None
    shipment = _search_one(SHIPMENTS_PATH, order_number)

    return {
        "order_number": order_number,
        "helm_internal_id": _norm(order.get("id")),
        "dc_alt_id": _norm((shipment or {}).get("alt_reference")),
        "status": _status_name(order.get("status_id")),
        "has_pregenerated_label": bool(order.get("pregenerated_label")),
        "shipping_cost": _norm(order.get("shipping_paid")),
        "total_discount": _norm(order.get("total_discount")),
        "total_tax": _norm(order.get("total_tax")),
        "order_total": _norm(order.get("total_paid")),
        "order_weight": _norm(order.get("total_weight")),
        "ship_by_date": _norm(order.get("ship_by_date")),
        "payment_method": _norm(order.get("payment_method")),
        "payment_reference": _norm(order.get("payment_ref")),
        "tracking_code": _norm((shipment or {}).get("shipping_tracking_code")),
        "tracking_url": _norm((shipment or {}).get("shipping_tracking_urls")),
        "customer_email": _norm(order.get("email")),
        "invoice_phone": _norm(order.get("phone_one")),
        "shipping_phone": _norm(order.get("phone_two")),
        "shipping_address": {
            "name": _norm(order.get("shipping_name")),
            "line_one": _norm(order.get("shipping_address_line_one")),
            "line_two": _norm(order.get("shipping_address_line_two")),
            "city": _norm(order.get("shipping_address_city")),
            "county": _norm(order.get("shipping_address_county")),
            "postcode": _norm(order.get("shipping_address_postcode")),
            "country": _norm(order.get("shipping_address_country")),
        },
    }


def get_tracking_url_from_dc(tracking_number: str) -> dict:
    """API replacement for helm_order.get_tracking_url_from_dc (which drove
    Helm's global search box in a browser): GET /shipments with
    filters[search_field]=search_shipping_tracking_code -- confirmed live to
    return an exact single match. Returns {'found': bool, 'tracking_code':
    str | None, 'tracking_url': str | None}."""
    params = {"filters[search_field]": "search_shipping_tracking_code", "filters[search]": tracking_number}
    data = _get(SHIPMENTS_PATH, params=params).get("data") or []
    if not data:
        return {"found": False, "tracking_code": None, "tracking_url": None}
    shipment = data[0]
    return {
        "found": True,
        "tracking_code": _norm(shipment.get("shipping_tracking_code")),
        "tracking_url": _norm(shipment.get("shipping_tracking_urls")),
    }


def get_shipment_postcode_by_tracking(tracking_number: str) -> str | None:
    """The postcode Helm actually has on file for whichever shipment THIS
    EXACT tracking number really belongs to -- confirmed live this can
    genuinely differ from the order's own claimed postcode when a
    tracking number has been reused/misattributed across orders (see
    dc_check.issue_type's "Tracking number also claimed on another Amazon
    order" case), which the order-number-keyed lookup (get_dc_order) has
    no way to catch since it only ever looks up by order number, not by
    tracking number.

    Per explicit instruction: this is what should be used to unlock the
    carrier's own postcode-verify step -- whatever postcode the carrier
    genuinely has on file for that exact tracking number is what it will
    actually accept, regardless of whose order it's "supposed" to belong
    to. The customer-facing name/address injected afterward for
    comparison still comes from Amazon's own order data separately (see
    pipeline_ui._carrier_lookup_payload) -- this is only ever used to get
    PAST the postcode gate, never shown as the customer's own address.
    Returns None if the tracking number isn't found at all."""
    params = {"filters[search_field]": "search_shipping_tracking_code", "filters[search]": tracking_number}
    data = _get(SHIPMENTS_PATH, params=params).get("data") or []
    if not data:
        return None
    return _norm(data[0].get("shipping_address_postcode"))


def get_orders_dispatched_between(start_date: date_type, end_date: date_type) -> list[dict]:
    """Every order Helm's own records show as despatched within
    [start_date, end_date] inclusive -- GET /orders?filters[
    dispatched_date_range]=DD/MM/YYYY,DD/MM/YYYY. Confirmed live: exact
    100% match (579/579 order numbers, zero missing/extra either way)
    against a real downloaded Shipping Report CSV for the same day.

    This is NOT the same as /shipments' own 'date_range' filter, which
    matches when the shipment record/label was CREATED, not the real
    despatch date -- for pre-generated-label orders those can differ by
    days, which silently dropped ~30% of real despatches when checked
    against the CSV. dispatched_date_range matches the real date_dispatched
    field instead.

    The end bound sent to Helm is end_date + 1 day -- confirmed live this
    is what's actually needed to include end_date's own despatches (an
    equal start/end date is silently ignored and returns everything
    unfiltered, so the two bounds must differ).

    Each returned order record already embeds its own shipment (tracking
    code, carrier, address) under the 'shipment' key -- no second call
    needed per order, unlike the per-order-number lookups elsewhere in this
    module."""
    start_str = start_date.strftime("%d/%m/%Y")
    end_str = (end_date + timedelta(days=1)).strftime("%d/%m/%Y")
    orders: list[dict] = []
    page = 1
    while True:
        body = _get(
            ORDERS_PATH,
            params={"filters[dispatched_date_range]": f"{start_str},{end_str}", "per_page": 249, "page": page},
        )
        orders.extend(body.get("data") or [])
        last_page = body.get("last_page", page)
        if page >= last_page:
            break
        page += 1
    return orders


def get_order_line_items(order_number: str) -> list[dict]:
    """Returns [{'sku', 'inventory_id', 'quantity'}] straight off the
    order's own 'inventory' array -- confirmed live this is already present
    on the /orders record itself, so unlike the browser-scraped version
    (which had to load the order's edit page and read its product table)
    this needs no separate call at all. Returns [] if the order isn't
    found."""
    order = _search_one(ORDERS_PATH, order_number)
    if order is None:
        return []
    return [
        {
            "sku": _norm(item.get("sku")),
            "inventory_id": _norm(item.get("inventory_id")),
            "quantity": _norm(item.get("quantity")),
        }
        for item in (order.get("inventory") or [])
    ]


def get_stock_availability(inventory_id: str) -> dict | None:
    """Current warehouse stock for this SKU -- GET /inventory/<id> (singular
    "inventory", confirmed live; "/inventories" 404s). Returns
    {'sku', 'stock_level', 'stock_level_available'}:
      - stock_level: total physical stock on hand right now.
      - stock_level_available: how much of that is actually free to use for
        a new order/claim (stock_level minus whatever's already reserved
        against other orders) -- the number that actually answers "could
        we send a replacement/padding parcel from this right now", not
        just "do we have any in the building at all".
    Returns None if this inventory_id doesn't exist."""
    try:
        item = _get(f"{INVENTORY_PATH}/{inventory_id}")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    return {
        "sku": _norm(item.get("sku")),
        "stock_level": item.get("stock_level"),
        "stock_level_available": item.get("stock_level_available"),
    }


def get_po_info_for_sku(inventory_id: str) -> dict | None:
    """The most relevant Purchase Order for this SKU -- confirmed live:
    GET /purchase_orders?filters[inventory_id]=<id> returns the most
    recently created PO first (same "first row" semantics the browser-
    scraped version relied on from Helm's own PO list page), and each
    record's own status_description is already the human-readable label
    (e.g. "Submitted", "Completed") -- no separate status lookup needed.
    Returns None if the SKU has no PO at all.

    Returns {'po_number', 'supplier', 'status', 'is_submitted'}. is_submitted
    is True ONLY for a literal "Submitted" status -- Draft, On Hold,
    Cancelled, Archived, and the derived in-progress labels (Processing/
    Partially Completed/Completed) are all NOT append-worthy evidence."""
    body = _get(PURCHASE_ORDERS_PATH, params={"filters[inventory_id]": inventory_id, "per_page": 1})
    data = body.get("data") or []
    if not data:
        return None
    po = data[0]
    status_text = _norm(po.get("status_description")) or ""
    return {
        "po_number": _norm(po.get("purchase_order_id")),
        "supplier": _norm(po.get("supplier_name")),
        "status": status_text,
        "is_submitted": status_text.strip().lower() == SUBMITTED_PO_LABEL,
    }


def attach_tag(order_summary_id: str, tag: str) -> dict:
    """Adds a tag to an order via Helm's own tagging API -- confirmed live
    against the real endpoint (POST /order_tags/<tag>/attach/<order_id>),
    same tag string the browser-automation version (helm_order_cancellation.
    add_order_tag) used. Returns the created tag record
    {'id', 'order_summary_id', 'tag', ...}."""
    from urllib.parse import quote

    return _post(f"/order_tags/{quote(tag, safe='')}/attach/{order_summary_id}")


def detach_tag(order_summary_id: str, tag: str) -> dict:
    """Removes a tag from an order -- the undo for attach_tag (confirmed
    live against the real endpoint)."""
    from urllib.parse import quote

    return _post(f"/order_tags/{quote(tag, safe='')}/detach/{order_summary_id}")


def cancel_order(order_summary_id: str) -> dict:
    """Cancels an order via Helm's own API -- POST /order/<id>/cancel, where
    <id> is the Helm internal order id (same value as order_summary_id used
    by attach_tag/detach_tag), NOT the Amazon order number. Given to this
    module, never live-tested here (a real, one-way action on a real order --
    confirmed working by the person who provided the endpoint instead).

    Returns {'status': 'success' | 'fail', 'ref': str | None, 'message': str
    | None}. Raises RuntimeError if status isn't 'success', so a caller that
    doesn't check the return value explicitly still can't silently treat a
    failed cancellation as done."""
    result = _post(f"/order/{order_summary_id}/cancel")
    if result.get("status") != "success":
        raise RuntimeError(f"Helm order cancel failed for order {order_summary_id}: {result.get('message') or result}")
    return result


if __name__ == "__main__":
    import sys

    print(get_dc_order(sys.argv[1]))
