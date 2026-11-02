"""Detects rows newly added to the ODR sheet's 'Active' tab since the last check.

Tracks progress by row count, not by Order ID, so this only works correctly
if rows are append-only (new rows added at the bottom). If the tab shrinks
(rows removed/archived) between checks, we can't tell which old rows moved,
so we reset the baseline to the current count and flag that in the result.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import google_sheets_client as gs
import defect_sheet_client as dc
from googleapiclient.discovery import build

STATE_PATH = Path(__file__).resolve().parent.parent / ".data" / "state" / "active_row_count.json"
TAB = "Active"


def _load_last_count() -> int:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text()).get("row_count", 0)
    return 0


def _save_count(count: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"row_count": count}))


def get_new_rows() -> dict:
    """Returns {'new_rows': [...], 'reset': bool, 'previous_count': int, 'current_count': int}.

    Each item in new_rows is {'row_number': int, 'values': [...]} (1-indexed
    sheet row, header row excluded from numbering/results).
    """
    creds = gs._credentials()
    service = build("sheets", "v4", credentials=creds)
    result = service.spreadsheets().values().get(
        spreadsheetId=dc.SPREADSHEET_ID, range=f"'{TAB}'!A2:N"
    ).execute()
    rows = result.get("values", [])
    current_count = len(rows)
    previous_count = _load_last_count()

    reset = current_count < previous_count
    if reset:
        new_rows = []
    else:
        new_rows = [
            {"row_number": i + 2, "values": rows[i]}
            for i in range(previous_count, current_count)
        ]

    _save_count(current_count)

    return {
        "new_rows": new_rows,
        "reset": reset,
        "previous_count": previous_count,
        "current_count": current_count,
    }


if __name__ == "__main__":
    result = get_new_rows()
    if result["reset"]:
        print(f"Row count shrank ({result['previous_count']} -> {result['current_count']}); "
              f"baseline reset, can't tell what was removed/archived.")
    elif not result["new_rows"]:
        print("No new rows since last check.")
    else:
        print(f"{len(result['new_rows'])} new row(s):")
        for item in result["new_rows"]:
            order_id = item["values"][2] if len(item["values"]) > 2 else "?"
            print(f"  Row {item['row_number']}: Order ID {order_id}")
