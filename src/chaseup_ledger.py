"""Google Sheet ledger for the WhatsApp customer chase-up flow.

One spreadsheet, one tab, one row per order. Lives in its own Drive folder
("Customer Chase-Up") so it's easy to find/share, same find-or-create-once
pattern as google_drive_client.EVIDENCE_FOLDER_NAME.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
CLIENT_SECRET_PATH = ".data/google_credentials/oauth_client.json"
TOKEN_PATH = ".data/google_credentials/token.json"

FOLDER_NAME = "Customer Chase-Up"
SPREADSHEET_NAME = "Customer Chase-Up Ledger"
TAB_NAME = "Ledger"

MAX_ATTEMPTS = 4
CHASE_INTERVAL_DAYS = 7

HEADERS = [
    "Order ID", "Phone", "Customer Name", "Has WhatsApp",
    "Attempt", "Last Sent Date", "Next Due Date",
    "Replied", "Reply Date", "Status",
]
# Status values
STATUS_PENDING = "Pending"
STATUS_REPLIED = "Replied"
STATUS_EXHAUSTED = "Exhausted"
STATUS_NO_WHATSAPP = "No WhatsApp"
STATUS_IGNORED = "Ignored"

_spreadsheet_id: str | None = None


def _credentials() -> Credentials:
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            f"No Google token found at {TOKEN_PATH} -- run `python src/_google_auth_once.py` once first."
        )
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def _drive_service():
    return build("drive", "v3", credentials=_credentials())


def _sheets_service():
    return build("sheets", "v4", credentials=_credentials())


def _get_or_create_folder_id() -> str:
    drive = _drive_service()
    existing = drive.files().list(
        q=f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id)", spaces="drive",
    ).execute().get("files", [])
    if existing:
        return existing[0]["id"]
    folder = drive.files().create(
        body={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return folder["id"]


def _get_or_create_spreadsheet_id() -> str:
    global _spreadsheet_id
    if _spreadsheet_id is not None:
        return _spreadsheet_id

    drive = _drive_service()
    folder_id = _get_or_create_folder_id()
    existing = drive.files().list(
        q=(
            f"name='{SPREADSHEET_NAME}' and mimeType='application/vnd.google-apps.spreadsheet' "
            f"and '{folder_id}' in parents and trashed=false"
        ),
        fields="files(id)", spaces="drive",
    ).execute().get("files", [])
    if existing:
        _spreadsheet_id = existing[0]["id"]
        return _spreadsheet_id

    sheets = _sheets_service()
    created = sheets.spreadsheets().create(
        body={"properties": {"title": SPREADSHEET_NAME}, "sheets": [{"properties": {"title": TAB_NAME}}]},
        fields="spreadsheetId",
    ).execute()
    spreadsheet_id = created["spreadsheetId"]

    # Move it into the shared folder (created in root by default)
    file = drive.files().get(fileId=spreadsheet_id, fields="parents").execute()
    prev_parents = ",".join(file.get("parents", []))
    drive.files().update(
        fileId=spreadsheet_id, addParents=folder_id, removeParents=prev_parents, fields="id",
    ).execute()

    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"'{TAB_NAME}'!A1",
        valueInputOption="RAW", body={"values": [HEADERS]},
    ).execute()

    _spreadsheet_id = spreadsheet_id
    return spreadsheet_id


def _col_letter(index: int) -> str:
    """0-indexed column number -> sheet column letter (A, B, ... J)."""
    return chr(ord("A") + index)


def get_all_rows() -> list[dict]:
    """Every ledger row as a dict keyed by HEADERS, plus '_row_number' (1-indexed sheet row)."""
    spreadsheet_id = _get_or_create_spreadsheet_id()
    sheets = _sheets_service()
    last_col = _col_letter(len(HEADERS) - 1)
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{TAB_NAME}'!A2:{last_col}"
    ).execute()
    rows = result.get("values", [])
    out = []
    for i, row in enumerate(rows):
        padded = row + [""] * (len(HEADERS) - len(row))
        item = dict(zip(HEADERS, padded))
        item["_row_number"] = i + 2
        out.append(item)
    return out


def find_row(order_id: str) -> dict | None:
    for row in get_all_rows():
        if row["Order ID"] == order_id:
            return row
    return None


def add_row(order_id: str, phone: str, customer_name: str) -> None:
    spreadsheet_id = _get_or_create_spreadsheet_id()
    sheets = _sheets_service()
    values = [order_id, phone, customer_name, "Unknown", "0", "", "", "No", "", STATUS_PENDING]
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range=f"'{TAB_NAME}'!A:A",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()


def update_row(row_number: int, fields: dict) -> None:
    """fields: {header_name: value}. Writes each changed cell individually
    (sparse updates, not a full-row overwrite) via a single batchUpdate."""
    spreadsheet_id = _get_or_create_spreadsheet_id()
    sheets = _sheets_service()
    data = []
    for header, value in fields.items():
        col = _col_letter(HEADERS.index(header))
        data.append({"range": f"'{TAB_NAME}'!{col}{row_number}", "values": [[value]]})
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def mark_sent(row_number: int, attempt: int) -> None:
    today = date.today()
    next_due = today + timedelta(days=CHASE_INTERVAL_DAYS)
    status = STATUS_EXHAUSTED if attempt >= MAX_ATTEMPTS else STATUS_PENDING
    update_row(row_number, {
        "Attempt": str(attempt),
        "Last Sent Date": today.isoformat(),
        "Next Due Date": "" if status == STATUS_EXHAUSTED else next_due.isoformat(),
        "Status": status,
    })


def mark_replied(row_number: int) -> None:
    update_row(row_number, {
        "Replied": "Yes",
        "Reply Date": date.today().isoformat(),
        "Status": STATUS_REPLIED,
    })


def mark_whatsapp_status(row_number: int, has_whatsapp: bool) -> None:
    fields = {"Has WhatsApp": "Yes" if has_whatsapp else "No"}
    if not has_whatsapp:
        fields["Status"] = STATUS_NO_WHATSAPP
    update_row(row_number, fields)


if __name__ == "__main__":
    sid = _get_or_create_spreadsheet_id()
    print(f"Ledger spreadsheet ready: https://docs.google.com/spreadsheets/d/{sid}")
    print(f"{len(get_all_rows())} existing row(s).")
