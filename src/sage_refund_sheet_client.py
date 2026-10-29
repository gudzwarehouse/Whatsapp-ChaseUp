"""Appends a row to the team's "ODR - Sage refund" tracking sheet --
https://docs.google.com/spreadsheets/d/1OJtAezw2wN8Mq33854qqIdqbnKKMTQlDxKgf3M4j00s
-- alongside (not instead of) the existing defect sheet, whenever "Log to
defect sheet" is clicked.

Confirmed live against the real sheet: one tab ("Sheet1"), two columns
("Order Id", "Parcel status "), no dropdown validation on either -- plain
free-text columns, currently empty (a brand-new sheet). "Parcel status" is
the DC's own order status text (dc_order['status'], e.g. "Despatched",
"Cancelled", "Pregen Success", "Despatch Ready") -- the same field the rest
of this project already treats as the authoritative parcel status,
confirmed as the right value for this column via explicit instruction
rather than guessed from the sheet alone (it carries no further hint of
its own -- no validation, no header comment, no existing rows).
"""

from __future__ import annotations

import google_sheets_client as gs

SPREADSHEET_ID = "1OJtAezw2wN8Mq33854qqIdqbnKKMTQlDxKgf3M4j00s"
SHEET_TAB = "Sheet1"

# "Order Id" is column A (confirmed live from the real header row).
_ORDER_ID_RANGE = "A:A"


def find_existing_row(order_number: str) -> bool:
    """Whether this order already has a row on the sheet -- checked fresh
    against the real sheet every call (never cached), same duplicate-
    prevention policy as defect_sheet_client.find_existing_row."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_TAB}'!{_ORDER_ID_RANGE}"
    ).execute()
    for row in result.get("values", []):
        if row and row[0].strip() == order_number.strip():
            return True
    return False


def load_all_logged_orders() -> set[str]:
    """Same lookup find_existing_row does, but for every order at once --
    one API read total instead of one read PER ORDER (see
    defect_sheet_client.load_all_logged_orders, same motivation)."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_TAB}'!{_ORDER_ID_RANGE}"
    ).execute()
    return {row[0].strip() for row in result.get("values", []) if row and row[0].strip()}


def append_refund_row(order_number: str, parcel_status: str | None) -> dict:
    """Appends one [Order Id, Parcel status] row. Never called
    automatically -- wired to the same manual "Log to defect sheet" button
    in pipeline_ui.py as the defect sheet itself, so both get logged
    together from one reviewed click.

    Returns {'appended': True}."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_TAB}'!A:B",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [[order_number, parcel_status or ""]]},
    ).execute()
    return {"appended": True}
