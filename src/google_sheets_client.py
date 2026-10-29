"""Appends cancellation rows to the shared Google Sheet that tracks orders
cancelled in the DC because they were never actually dispatched.

Auth is a one-time interactive OAuth flow (see _google_auth_once.py) whose
refresh token is cached at .data/google_credentials/token.json; every call
here just refreshes that token as needed, the same pattern as the other
clients in this project (asana_client, channeladvisor_client) load their own
credentials fresh rather than caching at import time.
"""

from __future__ import annotations

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Matches _google_auth_once.py's SCOPES exactly -- the saved token is shared
# with google_drive_client.py (same TOKEN_PATH), and a Credentials object
# only has the scopes it was actually granted at consent time regardless of
# what's listed here, so this list exists to stay honest about what the
# token can do, not to grant anything on its own.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
CLIENT_SECRET_PATH = ".data/google_credentials/oauth_client.json"
TOKEN_PATH = ".data/google_credentials/token.json"

SPREADSHEET_ID = "1pXSlVrBc3hQoNqp1Mzwi2a5GYUsJiWsYSGVsCkPGF2o"
SHEET_GID = 0

ROW_COLUMNS = ["Order ID", "SKU", "Quantity", "Purchase Order", "Supplier", "Reason", "Notes"]


def _credentials() -> Credentials:
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            f"No Google token found at {TOKEN_PATH} -- run `python src/_google_auth_once.py` "
            "once first to sign in and grant access."
        )
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def _sheet_title(service, spreadsheet_id: str, gid: int) -> str:
    """The gid in the sheet's URL is a numeric sheet id, not its tab name --
    the Sheets API's values.append needs the tab name (or an A1 range using
    it), so this resolves gid -> title once per call."""
    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("sheetId") == gid:
            return properties["title"]
    raise RuntimeError(f"No sheet with gid={gid} found in spreadsheet {spreadsheet_id}")


def append_cancellation_rows(rows: list[dict]) -> None:
    """Appends one row per dict in ``rows`` to the cancellation tracker sheet.
    Each dict should have the keys in ROW_COLUMNS (missing keys become blank
    cells); extra keys are ignored. Appends after the sheet's existing last
    row -- never overwrites anything already there."""
    if not rows:
        return
    service = build("sheets", "v4", credentials=_credentials())
    title = _sheet_title(service, SPREADSHEET_ID, SHEET_GID)
    values = [[str(row.get(column, "") or "") for column in ROW_COLUMNS] for row in rows]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{title}'!A:G",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


if __name__ == "__main__":
    import sys

    # Manual smoke test: python src/google_sheets_client.py <order_id> <sku> <qty> <po> <supplier> <reason> <notes>
    row = dict(zip(ROW_COLUMNS, sys.argv[1:]))
    append_cancellation_rows([row])
    print("Appended:", row)
