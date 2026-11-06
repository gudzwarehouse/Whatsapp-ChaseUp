"""Checks WhatsApp availability for new ledger rows, and sends chase-up
messages to rows that are due -- via the Node HTTP bridge
(whatsapp-service/index.js) on http://localhost:3001.
"""
import sys
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaseup_ledger as cl
import dc_api_client

BRIDGE_URL = "http://localhost:3001"

MESSAGE_TEMPLATE = (
    "Hello,\n\n"
    "We wanted to inform you that by the time we received your cancellation "
    "request your order had already shipped. As part of our commitment to "
    "providing excellent customer service, we processed and shipped your "
    "order promptly.\n\n"
    "If you have already received your order, please kindly confirm with us "
    "at your earliest convenience so we can assist you further.\n\n"
    "Please give us the opportunity to resolve your issue, and rest assured "
    "we will resolve any problem. We are a small family run business and "
    "this is very important to us.\n\n"
    "Kind Regards,\nGudz"
)


DESPATCHED_STATUSES = {"despatched", "dispatched"}


def _is_despatched(order_id: str) -> tuple[bool, str | None]:
    """Looks the order up on Helm. Returns (despatched, status_name)."""
    order = dc_api_client.get_dc_order(order_id)
    status = (order or {}).get("status")
    return (status or "").strip().lower() in DESPATCHED_STATUSES, status


def _bridge_connected() -> bool:
    try:
        r = httpx.get(f"{BRIDGE_URL}/status", timeout=10)
        return r.json().get("connected", False)
    except httpx.HTTPError:
        return False


def check_whatsapp_availability() -> dict:
    """For every ledger row with Has WhatsApp = Unknown, checks it via the
    bridge. Returns {'checked': [...], 'no_whatsapp': [...], 'errors': [...]}."""
    checked, no_whatsapp, errors = [], [], []
    for row in cl.get_all_rows():
        if row["Status"] != cl.STATUS_PENDING or row["Has WhatsApp"] != "Unknown":
            continue
        try:
            r = httpx.get(f"{BRIDGE_URL}/check-whatsapp", params={"phone": row["Phone"]}, timeout=20)
            r.raise_for_status()
            exists = r.json().get("exists", False)
        except httpx.HTTPError as exc:
            errors.append(f"{row['Order ID']}: WhatsApp check failed ({exc})")
            continue
        cl.mark_whatsapp_status(row["_row_number"], exists)
        checked.append(row["Order ID"])
        if not exists:
            no_whatsapp.append(row["Order ID"])
    return {"checked": checked, "no_whatsapp": no_whatsapp, "errors": errors}


def send_due_messages() -> dict:
    """Sends the chase-up message to every ledger row that's due (Attempt=0,
    or Next Due Date <= today), Status=Pending, Has WhatsApp=Yes.
    Returns {'sent': [...], 'exhausted': [...], 'errors': [...]}."""
    today = date.today().isoformat()
    sent, exhausted, errors, not_despatched = [], [], [], []

    for row in cl.get_all_rows():
        if row["Status"] != cl.STATUS_PENDING or row["Has WhatsApp"] != "Yes":
            continue
        attempt = int(row["Attempt"] or 0)
        if attempt >= cl.MAX_ATTEMPTS:
            continue
        due = (attempt == 0) or (row["Next Due Date"] and row["Next Due Date"] <= today)
        if not due:
            continue

        # Only chase customers whose order Helm shows as Despatched. Not
        # despatched (or not found) -> skip; attempt count is untouched so
        # it's retried on the next run.
        try:
            despatched, status = _is_despatched(row["Order ID"])
        except Exception as exc:
            errors.append(f"{row['Order ID']}: Helm status check failed ({exc})")
            continue
        if not despatched:
            not_despatched.append(f"{row['Order ID']} ({status or 'not found on Helm'})")
            continue

        try:
            r = httpx.post(
                f"{BRIDGE_URL}/send-message",
                json={"phone": row["Phone"], "message": MESSAGE_TEMPLATE},
                timeout=30,
            )
            r.raise_for_status()
            if not r.json().get("sent"):
                raise RuntimeError(r.text)
        except Exception as exc:
            errors.append(f"{row['Order ID']}: send failed ({exc})")
            continue

        new_attempt = attempt + 1
        cl.mark_sent(row["_row_number"], new_attempt)
        sent.append(f"{row['Order ID']} (attempt {new_attempt})")
        if new_attempt >= cl.MAX_ATTEMPTS:
            exhausted.append(row["Order ID"])

    return {"sent": sent, "exhausted": exhausted, "errors": errors, "not_despatched": not_despatched}


if __name__ == "__main__":
    if not _bridge_connected():
        print("WhatsApp bridge not connected -- is whatsapp-service/index.js running and logged in?")
        sys.exit(1)
    wa = check_whatsapp_availability()
    print(f"WhatsApp checked: {wa['checked']}, no WhatsApp: {wa['no_whatsapp']}")
    result = send_due_messages()
    print(f"Sent: {result['sent']}")
    print(f"Exhausted: {result['exhausted']}")
    print(f"Skipped (not despatched on Helm): {result['not_despatched']}")
    if result["errors"]:
        print(f"Errors: {result['errors']}")
