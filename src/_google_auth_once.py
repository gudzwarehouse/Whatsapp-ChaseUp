"""One-time interactive authorization: opens a browser for the user to sign in
and grant Sheets + Drive access, then saves a reusable token to
.data/google_credentials/token.json. Run this once; google_sheets_client.py and
google_drive_client.py both refresh the saved token automatically after that --
no need to run this again unless the token file is deleted, access is revoked,
or a scope is added (as happened when Drive access was added alongside Sheets --
re-run this once to pick up the new scope on the existing token file).
"""
from google_auth_oauthlib.flow import InstalledAppFlow

# "drive.file" (not the broader "drive") -- scoped to only files this app
# itself creates/opens via the API, not the user's whole Drive.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
CLIENT_SECRET_PATH = ".data/google_credentials/oauth_client.json"
TOKEN_PATH = ".data/google_credentials/token.json"

flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
creds = flow.run_local_server(port=0)

with open(TOKEN_PATH, "w", encoding="utf-8") as f:
    f.write(creds.to_json())

print(f"Saved token to {TOKEN_PATH}")
