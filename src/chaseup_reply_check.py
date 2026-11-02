"""Checks whatsapp-service/incoming_messages.json (written continuously by
the Baileys 'messages.upsert' listener) against the ledger, and marks any
matching Pending/Exhausted order as Replied.

Run this daily (or anytime) -- it's safe to run repeatedly: already-processed
message entries are tracked by count in .data/state/reply_check_cursor.json
so nothing gets double-processed.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaseup_ledger as cl

INCOMING_LOG_PATH = Path(__file__).resolve().parent.parent / "whatsapp-service" / "incoming_messages.json"
CURSOR_PATH = Path(__file__).resolve().parent.parent / ".data" / "state" / "reply_check_cursor.json"


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _load_cursor() -> int:
    if CURSOR_PATH.exists():
        return json.loads(CURSOR_PATH.read_text()).get("processed_count", 0)
    return 0


def _save_cursor(count: int) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps({"processed_count": count}))


def check_replies() -> list[dict]:
    """Returns the list of ledger rows just marked as Replied."""
    if not INCOMING_LOG_PATH.exists():
        return []

    messages = json.loads(INCOMING_LOG_PATH.read_text())
    processed_count = _load_cursor()
    new_messages = messages[processed_count:]
    if not new_messages:
        return []

    senders = {_digits(m["from"].split("@")[0]) for m in new_messages if m.get("from")}

    ledger_rows = cl.get_all_rows()
    newly_replied = []
    for row in ledger_rows:
        if row["Status"] not in (cl.STATUS_PENDING, cl.STATUS_EXHAUSTED):
            continue
        if _digits(row["Phone"]) in senders:
            cl.mark_replied(row["_row_number"])
            newly_replied.append(row)

    _save_cursor(len(messages))
    return newly_replied


if __name__ == "__main__":
    replied = check_replies()
    if not replied:
        print("No new replies.")
    else:
        print(f"{len(replied)} order(s) marked Replied:")
        for row in replied:
            print(f"  {row['Order ID']} ({row['Phone']})")
