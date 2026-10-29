"""Appends a row to the team's "evri claims sheet" --
https://docs.google.com/spreadsheets/d/1s5WYtlV8itszp05bz4JMFytlYdM9uv-TyMO_F3nn-2o
-- alongside the defect sheet and the Sage refund sheet, whenever "Log to
defect sheet" is clicked for an order whose parcel was actually dispatched
(a carrier claim only makes sense once something was handed to a carrier to
lose/damage/return in the first place).

Confirmed live against the real sheet: two tabs matter here --
  - "Evri Claims " (20 columns, header row 1): ORDER NO is column F,
    Claim Reason column C, claim status column H, Parcel Status column I.
  - "RM Claims " (12 columns, header row 1): Order ID is column B,
    Claim Reason column D, Claim Status column E, Parcel Status column F.
Routed by carrier (tracking_verification's own 'carrier' text) -- any
carrier that's neither Evri/Hermes nor Royal Mail is skipped entirely,
since neither tab applies.

Only 4 cells are ever filled, everything else left blank for whoever
manages this sheet day to day to fill in themselves:
  - Claim Reason: "A-Z" on both tabs (these are all A-to-z Guarantee
    claims) -- confirmed as the right value via explicit instruction,
    matching the same "A-Z" convention already used elsewhere in this
    project (e.g. defect_sheet_client's cancellation rows).
  - Claim status: "Claim To Be Made" -- confirmed live from the sheet's
    own real dropdown (a chip-style data validation that doesn't show up
    in the Sheets API's own dataValidation metadata, so this exact
    capitalisation was confirmed from a screenshot of the actual dropdown,
    not guessed) -- the one value in that dropdown meaning "not yet
    decided", matching every other value being a later carrier outcome
    (Claim In Progress / Claim - Approved / Claim - Rejected / etc.).
  - Parcel Status: derived from the REAL tracking_verification result
    (never guessed from the claim's own text) -- see
    _parcel_status_from_tracking below. Confirmed live from the sheet's
    own dropdown: Lost / Damaged / Delivered / Returned / Recall -- only
    the first four are ever produced here; "Recall" has no equivalent in
    tracking_verification's own issue_type classification, so it's never
    written by this tool.
"""

from __future__ import annotations

import google_sheets_client as gs

SPREADSHEET_ID = "1s5WYtlV8itszp05bz4JMFytlYdM9uv-TyMO_F3nn-2o"
EVRI_TAB = "Evri Claims "
RM_TAB = "RM Claims "

_TAB_COLUMNS = {
    EVRI_TAB: [
        "Date raised", "despatched date", "Claim Reason", "Reasons", "HERMES BARCODE",
        "ORDER NO", "Postcode ", "claim status", "Parcel Status ", "portal status ",
        "1st Appeal", "2nd Appeal reference", "3rd Appeal reference", "Claim Approved Amount",
        "Credited Amount / Invoice  ( Evri )", "Amount raised ", "Order Amount",
        "Assigned To", "Description", "Raised with Liga",
    ],
    RM_TAB: [
        "Column 1", "Order ID", "Tracking Number", "Claim Reason ", "Claim Status ",
        "Parcel Status ", "platform status ", "Claim Reference", "Re - Appelaed Ref",
        "Claim Approved Amount", "Order Amount", "Assigned To",
    ],
}
# Which column holds the order number, per tab -- "ORDER NO" on Evri,
# "Order ID" on RM, confirmed live from each tab's own header row.
_ORDER_COLUMN = {EVRI_TAB: "ORDER NO", RM_TAB: "Order ID"}
_CLAIM_REASON_COLUMN = {EVRI_TAB: "Claim Reason", RM_TAB: "Claim Reason "}
_CLAIM_STATUS_COLUMN = {EVRI_TAB: "claim status", RM_TAB: "Claim Status "}
_PARCEL_STATUS_COLUMN = {EVRI_TAB: "Parcel Status ", RM_TAB: "Parcel Status "}

CLAIM_REASON = "A-Z"
# Exact casing confirmed from the real dropdown chip: "made" is lowercase,
# unlike every other value in that list ("Claim In Progress",
# "Claim - Approved", etc.) -- corrected per explicit instruction after an
# initial guess got the capitalisation wrong.
CLAIM_STATUS_TO_BE_MADE = "Claim To Be made"


def tab_for_carrier(carrier: str | None) -> str | None:
    """Which tab this order's claim belongs on, purely by carrier name --
    None (skip entirely) for any carrier that isn't Evri/Hermes or Royal
    Mail, since neither tab applies to it."""
    text = (carrier or "").strip().lower()
    if "evri" in text or "hermes" in text:
        return EVRI_TAB
    if "royal mail" in text:
        return RM_TAB
    return None


def parcel_status_from_tracking(tracking_verification: dict | None) -> str | None:
    """Maps the REAL carrier tracking result to the sheet's own Parcel
    Status vocabulary -- never guessed from the claim's own text. None
    (leave the cell blank) for a case with no equivalent value on the
    sheet (e.g. "Delivery attempt failed", or no issue classified at
    all while still genuinely undelivered)."""
    tv = tracking_verification or {}
    if tv.get("delivered") is True:
        # Trailing space confirmed from the real dropdown chip -- the
        # sheet's own stored value is "Delivered " (with a space), not
        # "Delivered", corrected per explicit instruction.
        return "Delivered "
    issue_type = tv.get("issue_type")
    return {
        "Damaged in transit": "Damaged",
        "Lost in transit": "Lost",
        "Returned to sender": "Returned",
    }.get(issue_type)


def find_existing_row(order_number: str, tab: str) -> bool:
    """Whether this order already has a row on the given tab -- checked
    fresh against the real sheet every call (never cached), same
    duplicate-prevention policy as defect_sheet_client.find_existing_row."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    order_column_letter = chr(ord("A") + _TAB_COLUMNS[tab].index(_ORDER_COLUMN[tab]))
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!{order_column_letter}:{order_column_letter}"
    ).execute()
    for row in result.get("values", []):
        if row and row[0].strip() == order_number.strip():
            return True
    return False


def load_all_logged_orders() -> dict[str, str]:
    """Same lookup find_existing_row does, but for every order at once --
    one API read per tab (2 total, Evri + RM) instead of one read PER
    ORDER (see defect_sheet_client.load_all_logged_orders, same
    motivation). Returns {order_number: tab}."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    found: dict[str, str] = {}
    for tab in (EVRI_TAB, RM_TAB):
        order_column_letter = chr(ord("A") + _TAB_COLUMNS[tab].index(_ORDER_COLUMN[tab]))
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!{order_column_letter}:{order_column_letter}"
        ).execute()
        for row in result.get("values", []):
            if row and row[0].strip():
                found[row[0].strip()] = tab
    return found


def append_claim_row(order_number: str, tab: str, parcel_status: str | None) -> dict:
    """Appends one row to the given tab, filling only Claim Reason/Claim
    status/Parcel Status/order-number columns, everything else blank.
    Never called automatically -- wired to the same manual "Log to defect
    sheet" button in pipeline_ui.py as the other two sheets, so all three
    get logged together from one reviewed click.

    Returns {'appended': True}."""
    columns = _TAB_COLUMNS[tab]
    values_by_column = {
        _ORDER_COLUMN[tab]: order_number,
        _CLAIM_REASON_COLUMN[tab]: CLAIM_REASON,
        _CLAIM_STATUS_COLUMN[tab]: CLAIM_STATUS_TO_BE_MADE,
        _PARCEL_STATUS_COLUMN[tab]: parcel_status or "",
    }
    row = [values_by_column.get(column, "") for column in columns]
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    last_column_letter = chr(ord("A") + len(columns) - 1)
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab}'!A:{last_column_letter}",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return {"appended": True}
