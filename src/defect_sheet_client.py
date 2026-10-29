"""Appends a row to the team's hand-maintained "Defect" tracking sheet --
https://docs.google.com/spreadsheets/d/179utCdJnvBzVj_7b4fY9Lq7pCd-Ggo3Li9Ak-nbzIKk
-- a manual, reviewed action (a button in pipeline_ui.py), never automatic,
since every row on this sheet is otherwise entered and maintained by hand.

Confirmed live against the real sheet: it has three tabs --
  - "Active": 23 columns, the default destination, with strict dropdown
    validation on ODR / Loss / Action Stage (the exact accepted strings are
    hardcoded below, confirmed via the sheet's own data-validation rules,
    not guessed).
  - "Archived" / "Archived - Green Section": used for already-resolved
    cases. Per the team's own rule, a 2nd-appeal order that's both "not
    sent" AND "ODR - No" goes straight to "Archived - Green Section"
    instead of "Active" -- there's nothing left to action, so it only needs
    the handful of columns that tab actually has (confirmed live: Order
    date, Defect received date, Order ID, Customer Chase up, Customer Chase
    up action date, ODR, Loss -- no Action Stage column at all there).

Reuses asana_task_formatter.classify_claim_projects/classify_decision_projects's
own project-classification keys (odr_yes/odr_no/odr_unknown, loss_yes/loss_no,
action_stage_to_be_made/action_stage_amz_claim_in_progress/
customer_payment_contact_required) as the single source of truth for this
sheet's ODR/Loss/Action-Stage values too, instead of re-deriving the same
Sent/Not-sent x ODR x Loss rules a second time here.
"""

from __future__ import annotations

import google_sheets_client as gs

SPREADSHEET_ID = "179utCdJnvBzVj_7b4fY9Lq7pCd-Ggo3Li9Ak-nbzIKk"
ACTIVE_TAB = "Active"
ARCHIVE_TAB = "Archived"
ARCHIVE_GREEN_TAB = "Archived - Green Section"

# "Order ID" is column C on all three real tabs (confirmed live from each
# tab's own header row).
_ORDER_ID_RANGE = "C:C"


def find_existing_row(order_number: str) -> str | None:
    """Which tab (if any) already has a row for this order -- checked
    fresh against the real sheet every call, before ever offering to log a
    new one, so the same order never gets duplicated onto the sheet. Never
    memoized: someone else adding a row by hand between two checks in the
    same session must be seen, not missed until a restart."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    for tab in (ACTIVE_TAB, ARCHIVE_TAB, ARCHIVE_GREEN_TAB):
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!{_ORDER_ID_RANGE}"
        ).execute()
        for row in result.get("values", []):
            if row and row[0].strip() == order_number.strip():
                return tab
    return None


def find_row_in_tab(order_number: str, tab: str) -> bool:
    """Whether this specific tab already has a row for the order (find_existing_row
    only reports the first tab it finds, Active first)."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!{_ORDER_ID_RANGE}"
    ).execute()
    return any(row and row[0].strip() == order_number.strip() for row in result.get("values", []))


def remove_from_active(order_number: str) -> int:
    """Deletes every row for this order from the Active tab (the whole row,
    not just its cells) and returns how many were removed. Only for moving an
    order to an archive tab -- the caller must have confirmed the archive row
    exists first. Deletes bottom-up so earlier row indexes stay valid."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{ACTIVE_TAB}'!{_ORDER_ID_RANGE}"
    ).execute()
    row_indexes = [
        index for index, row in enumerate(result.get("values", []))  # 0-based, matches the API's startIndex
        if row and row[0].strip() == order_number.strip()
    ]
    if not row_indexes:
        return 0
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID, fields="sheets.properties").execute()
    sheet_id = next(sh["properties"]["sheetId"] for sh in meta["sheets"] if sh["properties"]["title"] == ACTIVE_TAB)
    requests = [
        {"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": i, "endIndex": i + 1}}}
        for i in sorted(row_indexes, reverse=True)
    ]
    service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": requests}).execute()
    return len(row_indexes)


def load_all_logged_orders() -> dict[str, str]:
    """Same lookup find_existing_row does, but for every order at once --
    one API read per tab (3 total) instead of one read per tab PER ORDER.
    Confirmed as the real cause of the orders list taking a long time to
    finish drawing after a fetch: pipeline_ui.py called find_existing_row
    once per unlogged card the first time each rendered this session, up
    to ~50 live Sheets reads just for this one sheet (and as many again for
    the Sage/carrier sheets) -- sequential network round trips, each one
    visibly delaying the cards further down the list. Returns
    {order_number: tab}, used to seed every card's cache at once instead."""
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    found: dict[str, str] = {}
    for tab in (ACTIVE_TAB, ARCHIVE_TAB, ARCHIVE_GREEN_TAB):
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!{_ORDER_ID_RANGE}"
        ).execute()
        for row in result.get("values", []):
            if row and row[0].strip():
                found[row[0].strip()] = tab
    return found

# Exact strings confirmed live from the sheet's own data-validation rules on
# the "Active" tab (strict dropdowns) -- note the real, slightly odd
# spacing ("ODR -No" has no space before "No") must be matched exactly or
# a strict-validated cell can reject/flag the write.
ODR_SHEET_LABELS = {"odr_yes": "ODR - Yes", "odr_no": "ODR -No", "odr_unknown": "ODR - Unknown"}
LOSS_SHEET_LABELS = {"loss_yes": "Loss - Yes", "loss_no": "Loss - No"}
# The "Actual Parcel Status " column's own dropdown, confirmed live from
# the real sheet (NOT the same vocabulary as carrier_claims_sheet_client's
# Evri/RM "Parcel Status" dropdown -- that one was wrongly reused here
# before, producing values this column's own validation doesn't even
# accept): Delivered to customer / Lost in Transist / Recall / Return
# Arranged / Lost After Delivery / Damaged / Retuned to sender -- the two
# odd spellings ("Transist", "Retuned") are exactly how the real dropdown
# has them, not typos to fix. Only the first three mapped values below have
# any tracking_verification equivalent; Recall/Return Arranged/Lost After
# Delivery are later, carrier-outcome statuses nothing in tracking data can
# tell us up front, so they're never auto-filled.
ACTUAL_PARCEL_STATUS_LABELS = {
    "delivered": "Delivered to customer",
    "Lost in transit": "Lost in Transist",
    "Damaged in transit": "Damaged",
    "Returned to sender": "Retuned to sender",
}


def actual_parcel_status_from_tracking(tracking_verification: dict | None) -> str | None:
    """Maps the REAL carrier tracking result to THIS sheet's own Actual
    Parcel Status vocabulary -- never guessed from the claim's own text.
    None (leave the cell blank) for a case with no equivalent value on
    this dropdown (e.g. "Delivery attempt failed", or no issue classified
    at all while still genuinely undelivered)."""
    tv = tracking_verification or {}
    if tv.get("delivered") is True:
        return ACTUAL_PARCEL_STATUS_LABELS["delivered"]
    issue_type = tv.get("issue_type")
    return ACTUAL_PARCEL_STATUS_LABELS.get(issue_type)


ACTION_STAGE_SHEET_LABELS = {
    "action_stage_to_be_made": "Carrier Claim - To be Made",
    # The Asana-side key name says "in progress", but that's this project's
    # OWN internal label for that Asana classification project, not the
    # defect sheet's convention -- a row being logged here for the first
    # time hasn't had any claim made yet, so it starts as "to be Made"
    # (confirmed against the team's own wording: "Amazon claim to be made"
    # for both the not-sent+ODR-Yes and the sent case), matching
    # "Carrier Claim - To be Made"'s own same not-yet-started phrasing.
    "action_stage_amz_claim_in_progress": "Amazon Claim to be Made",
    "customer_payment_contact_required": "Customer Payment/Contact Required",
    # Per explicit instruction: a claim Amazon closed outright while
    # having already debited the seller's account. Confirmed live against
    # the real "Active" tab's own Action Stage dropdown (ONE_OF_LIST,
    # strict=True) -- it already has this exact option, just in Title
    # Case, not the lowercase phrasing first guessed here.
    "closed_debited": "Amazon Funded - Money Never Left Us",
}

# Column order confirmed live from the real "Active" tab header row.
ACTIVE_COLUMNS = [
    "Order date",
    "Defect received date",
    "Order ID",
    "Customer Chase up (FAO of Nishani Only)",
    "Customer Chase up action date ",
    "ODR - Yes/No/Unknown",
    "Loss",
    "Dummy tracking ",
    "Action Stage",
    "Actual Parcel Status ",
    "Amount Aproved by Carrier",
    "Amount Aproved by Amazon",
    "Courier Tracking ",
    "A-to-z Guarantee claims",
    "Chargeback claims",
    "Impacts Account Health",
    "Have we sent it?",
    "Country",
    "Date to Appeal by?",
    "Customer Name",
    "Order Total",
    "Country",
    "Carrier Claim Action",
]

# Column order confirmed live from the real "Archived - Green Section" tab
# -- only these 7 columns exist there at all.
ARCHIVE_GREEN_COLUMNS = [
    "Order date",
    "Defect received date",
    "Order ID",
    "Customer Chase up (FAO of Nishani Only)",
    "Customer Chase up action date",
    "ODR - Yes/No/Unknown",
    "Loss",
]


def is_archive_case(projects: list[str]) -> bool:
    """True for the original case the team defined as needing no further
    action at all: a 2nd-appeal order that's both "not sent" (loss_no) and
    "ODR - No" (odr_no) -- classify_decision_projects only ever produces
    this pair together with no action_stage_* key, since an ODR-affected
    not-sent order always gets action_stage_amz_claim_in_progress appended
    instead. 1st representation can never match this (classify_claim_projects
    only ever uses odr_unknown, never odr_no), consistent with the team's
    own rule only naming this for 2nd appeal.

    Also true for a claim Amazon closed outright that was never debited
    (the "closed_not_debited" marker bulk_scheduler.process_order adds to
    ``projects`` itself, independent of the odr/loss classification above)
    -- per explicit instruction, nothing is left to action there either,
    so it goes to the same tab.

    Never true when "closed_debited" is present, even if odr_no/loss_no
    also happen to match -- a closed-but-debited claim must always land
    on Active with its own Action Stage (see _row_values), since money
    actually being taken is exactly the thing that still needs a human to
    notice, unlike every other archive case."""
    if "closed_debited" in projects:
        return False
    return ("odr_no" in projects and "loss_no" in projects) or "closed_not_debited" in projects


def _row_values(order_number: str, projects: list[str], fields: dict, force_active: bool = False) -> tuple[str, list[str]]:
    """Builds the row to append and picks the destination tab, from the
    SAME classification keys already computed for Asana filing (projects)
    plus whatever raw order fields the caller has on hand. Returns
    (tab_name, row_values)."""
    # "ODR - Unknown" genuinely belongs to 1st representation (Amazon
    # hasn't decided the claim yet at that stage, so ODR impact really is
    # unknown) -- but classify_decision_projects (2nd appeal) never emits
    # an explicit odr_unknown key, it just omits odr_yes/odr_no when the
    # decision email's own ODR Affect wording couldn't be parsed. Falling
    # back to the same "ODR - Unknown" label there too (rather than a
    # blank cell) keeps every logged row holding one of the sheet's three
    # real dropdown states, never an empty one.
    odr_label = next((ODR_SHEET_LABELS[k] for k in projects if k in ODR_SHEET_LABELS), ODR_SHEET_LABELS["odr_unknown"])
    loss_label = next((LOSS_SHEET_LABELS[k] for k in projects if k in LOSS_SHEET_LABELS), "")

    if is_archive_case(projects) and not force_active:
        values_by_column = {
            "Order date": fields.get("order_date") or "",
            "Defect received date": fields.get("defect_received_date") or "",
            "Order ID": order_number,
            "ODR - Yes/No/Unknown": odr_label,
            "Loss": loss_label,
        }
        return ARCHIVE_GREEN_TAB, [values_by_column.get(column, "") for column in ARCHIVE_GREEN_COLUMNS]

    # "closed_debited" is exclusive -- a claim that's already closed has
    # nothing left to action, so it must never combine with e.g. "Carrier
    # Claim - To be Made" (which assumes the claim is still being worked).
    if "closed_debited" in projects:
        action_stage = ACTION_STAGE_SHEET_LABELS["closed_debited"]
    else:
        action_stage = ", ".join(ACTION_STAGE_SHEET_LABELS[k] for k in projects if k in ACTION_STAGE_SHEET_LABELS)
    values_by_column = {
        "Order date": fields.get("order_date") or "",
        "Defect received date": fields.get("defect_received_date") or "",
        "Order ID": order_number,
        "ODR - Yes/No/Unknown": odr_label,
        "Loss": loss_label,
        "Action Stage": action_stage,
        "Actual Parcel Status ": fields.get("actual_parcel_status") or "",
        "Courier Tracking ": fields.get("tracking_number") or "",
        "Have we sent it?": "Yes" if fields.get("sent") else "No",
        "Country": fields.get("country") or "",
        "Date to Appeal by?": fields.get("appeal_by") or "",
        "Customer Name": fields.get("customer_name") or "",
        "Order Total": fields.get("order_total") or "",
    }
    return ACTIVE_TAB, [values_by_column.get(column, "") for column in ACTIVE_COLUMNS]


def set_action_stage(order_number: str, action_stage: str) -> dict:
    """Overwrites (or creates) the Active tab's own "Action Stage" cell for
    this order -- only that one column, never ODR/Loss/anything else on an
    existing row, since every other column here is otherwise entered and
    maintained by hand. If the order isn't logged on the Active tab yet, a
    new row is appended with just Order ID + Action Stage filled in (every
    other column left blank rather than guessed).

    Unlike append_defect_row, this IS called automatically -- per explicit
    instruction, by reappeal_scheduler.py whenever a re-appeal attempt
    finds Amazon has genuinely closed the claim, which always writes the
    same fixed action_stage value regardless of whether the claim was
    actually debited or not (no attempt to tell those apart here).

    Returns {'updated': bool, 'tab': str, 'created_row': bool, 'error': str | None}."""
    try:
        service = gs.build("sheets", "v4", credentials=gs._credentials())
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'{ACTIVE_TAB}'!{_ORDER_ID_RANGE}"
        ).execute()
        rows = result.get("values", [])
        row_number = next(
            (i + 1 for i, row in enumerate(rows) if row and row[0].strip() == order_number.strip()),
            None,
        )
        action_stage_column = chr(ord("A") + ACTIVE_COLUMNS.index("Action Stage"))

        if row_number is not None:
            service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{ACTIVE_TAB}'!{action_stage_column}{row_number}",
                valueInputOption="USER_ENTERED",
                body={"values": [[action_stage]]},
            ).execute()
            return {"updated": True, "tab": ACTIVE_TAB, "created_row": False, "error": None}

        values_by_column = {"Order ID": order_number, "Action Stage": action_stage}
        row = [values_by_column.get(column, "") for column in ACTIVE_COLUMNS]
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{ACTIVE_TAB}'!A:{chr(ord('A') + len(row) - 1)}",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return {"updated": True, "tab": ACTIVE_TAB, "created_row": True, "error": None}
    except Exception as exc:
        return {"updated": False, "tab": ACTIVE_TAB, "created_row": False, "error": str(exc)}


def append_defect_row(order_number: str, projects: list[str], fields: dict, force_active: bool = False) -> dict:
    """Appends one row to whichever tab this order's classification
    (``projects`` -- the same list classify_claim_projects/
    classify_decision_projects already computed for Asana filing) belongs
    on. ``fields`` supplies whatever of order_date/defect_received_date/
    tracking_number/sent/country/appeal_by/customer_name/order_total the
    caller has available; anything missing is left blank rather than
    guessed.

    Never called automatically -- this is wired to a manual, reviewed
    button in pipeline_ui.py, since every other row on this sheet is
    entered and maintained by hand.

    Returns {'appended': True, 'tab': str}."""
    tab, row = _row_values(order_number, projects, fields, force_active=force_active)
    service = gs.build("sheets", "v4", credentials=gs._credentials())
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab}'!A:{chr(ord('A') + len(row) - 1)}",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return {"appended": True, "tab": tab}
