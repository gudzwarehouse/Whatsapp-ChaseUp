"""Pulls orders from the ODR sheet's 'Active' tab into the chase-up ledger.

For every Order ID on Active that isn't already a ledger row, looks up its
phone number via the Helm DC API (invoice_phone) and adds it with Attempt=0,
Status=Pending. Orders already in the ledger are left untouched here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaseup_ledger as cl
import dc_api_client as dc
import track_new_active_rows as tracker

# Helm's invoice_phone/shipping_phone come back in whatever format the
# customer typed at checkout -- some already E.164 ("+447903902006"), some
# UK local format with a leading 0 and no country code ("07805075356").
# WhatsApp's API needs a full E.164 number (no leading 0, country code
# present), so a bare UK-local number is assumed UK and converted; anything
# else with a leading 0 is left alone since we can't guess its country.
def normalize_phone(phone: str) -> str:
    phone = (phone or "").strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        return phone
    if phone.startswith("00"):
        return "+" + phone[2:]
    if not phone.isdigit():
        return phone
    # Any other UK-local number (leading 0, mobile or landline) -- assume UK.
    if phone.startswith("0") and len(phone) in (10, 11):
        return "+44" + phone[1:]
    # Already has a country code but is missing the leading '+' (e.g. Helm
    # exported "44 7960 355154" -> "447960355154" once spaces are stripped).
    if len(phone) >= 10:
        return "+" + phone
    return phone


def sync() -> dict:
    """Only considers orders added to the Active tab AFTER the row-count
    baseline tracker.get_new_rows() already has on file -- rows that
    existed before that baseline are deliberately ignored (per explicit
    instruction: don't chase up pre-existing orders, only ones added from
    now on). Returns {'added': [...], 'lookup_failed': [...]}."""
    new_rows = tracker.get_new_rows()["new_rows"]
    order_ids = [row["values"][2].strip() for row in new_rows if len(row["values"]) > 2 and row["values"][2].strip()]
    existing_ids = {row["Order ID"] for row in cl.get_all_rows()}

    added = []
    lookup_failed = []
    for order_id in order_ids:
        if order_id in existing_ids:
            continue
        try:
            dc_order = dc.get_dc_order(order_id)
        except Exception as exc:
            lookup_failed.append(f"{order_id}: {exc}")
            continue
        if dc_order is None:
            lookup_failed.append(f"{order_id}: not found in Helm")
            continue
        phone = dc_order.get("invoice_phone") or dc_order.get("shipping_phone")
        if not phone:
            lookup_failed.append(f"{order_id}: no phone number on file")
            continue
        phone = normalize_phone(phone)
        customer_name = (dc_order.get("shipping_address") or {}).get("name") or ""
        cl.add_row(order_id, phone, customer_name)
        added.append(order_id)

    return {"added": added, "lookup_failed": lookup_failed}


if __name__ == "__main__":
    result = sync()
    print(f"Added {len(result['added'])} new order(s): {result['added']}")
    if result["lookup_failed"]:
        print(f"{len(result['lookup_failed'])} lookup failure(s):")
        for item in result["lookup_failed"]:
            print(f"  {item}")
