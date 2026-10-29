"""Uploads a 2nd-appeal evidence PDF to Google Drive and returns a public
(anyone-with-the-link) view URL -- so the evidence is reachable from any
machine/reviewer, not just whoever has the local file.

Auth is the same one-time interactive OAuth flow as google_sheets_client.py
(see _google_auth_once.py), sharing the same token file -- its SCOPES list
includes both Sheets and Drive access.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
CLIENT_SECRET_PATH = ".data/google_credentials/oauth_client.json"
TOKEN_PATH = ".data/google_credentials/token.json"

# A dedicated folder keeps evidence PDFs out of the account's root Drive --
# created once on first upload and reused after that (its id is cached in
# this file so later runs don't recreate it every time).
EVIDENCE_FOLDER_NAME = "A-to-z Claims Evidence"
_evidence_folder_id: str | None = None


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


def _get_evidence_folder_id(service) -> str:
    global _evidence_folder_id
    if _evidence_folder_id is not None:
        return _evidence_folder_id

    existing = service.files().list(
        q=(
            f"name='{EVIDENCE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        ),
        fields="files(id)",
        spaces="drive",
    ).execute()
    files = existing.get("files", [])
    if files:
        _evidence_folder_id = files[0]["id"]
        return _evidence_folder_id

    folder = service.files().create(
        body={"name": EVIDENCE_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    _evidence_folder_id = folder["id"]
    return _evidence_folder_id


def upload_evidence_pdf(order_number: str, pdf_path: str) -> dict:
    """Uploads the PDF at pdf_path, names it after the order number, makes it
    viewable by anyone with the link, and returns
    {'file_id': str, 'view_url': str}."""
    service = build("drive", "v3", credentials=_credentials())
    folder_id = _get_evidence_folder_id(service)

    media = MediaFileUpload(pdf_path, mimetype="application/pdf")
    file = service.files().create(
        body={"name": f"{order_number} - 2nd appeal evidence.pdf", "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()

    service.permissions().create(
        fileId=file["id"],
        body={"role": "reader", "type": "anyone"},
    ).execute()

    # webViewLink from the create() call can be stale/missing the share
    # state that was just granted -- re-fetch it after setting the
    # permission so the returned link is guaranteed to actually be public.
    refreshed = service.files().get(fileId=file["id"], fields="webViewLink").execute()
    return {"file_id": file["id"], "view_url": refreshed["webViewLink"]}


_FILE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")


def download_evidence_pdf(file_id_or_link: str, dest_path: str) -> str:
    """Downloads a previously-uploaded evidence PDF back from Drive to
    dest_path -- the self-healing counterpart to upload_evidence_pdf, for
    when the local copy that's supposed to live at orders_store's
    manual_evidence.pdf_path has since been deleted/moved (confirmed live
    on orders 206-0278323-8514714 and 026-5957607-4757916: the recorded
    pdf_path no longer existed on disk, so fill_appeal_to_amazon silently
    attached nothing and Amazon's Submit button stayed disabled). Accepts
    either a bare file id or the full webViewLink -- the id is whatever
    sits between "/d/" and the next "/" in that link."""
    match = _FILE_ID_RE.search(file_id_or_link)
    file_id = match.group(1) if match else file_id_or_link

    service = build("drive", "v3", credentials=_credentials())
    request = service.files().get_media(fileId=file_id)
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest_path


if __name__ == "__main__":
    import sys

    order_number, pdf_path = sys.argv[1], sys.argv[2]
    result = upload_evidence_pdf(order_number, pdf_path)
    print(result)
