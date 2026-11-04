# WhatsApp Chase-Up

Automated WhatsApp customer chase-up for Amazon A-Z claim orders stuck in
"Action Stage- AMZ Claim in Progress". Pulls new orders from the ODR sheet,
finds the customer's phone via Helm, and messages them every 7 days (up to
4 attempts) until they reply.

## Components

```
whatsapp-service/        Node.js Baileys WhatsApp client + HTTP bridge (port 3001)
  index.js               Connects to WhatsApp, exposes /status /qr /check-whatsapp /send-message,
                          logs every incoming message to incoming_messages.json
  auth_info/              Baileys session (gitignored) -- delete to force a fresh QR login

src/
  chaseup_run.py          Orchestrator -- run this (via run_chaseup.bat) on a schedule
  chaseup_sync.py          Pulls new orders from the ODR 'Active' tab into the ledger
  chaseup_send.py          Checks WhatsApp availability + sends due messages, via the bridge
  chaseup_reply_check.py   Matches incoming WhatsApp messages to ledger rows, marks Replied
  chaseup_ledger.py        Google Sheet ledger client (the shared "Customer Chase-Up Ledger")
  chaseup_notifications.py HTML/plaintext summary email after each run
  track_new_active_rows.py Row-count-based "what's new since last check" tracker for the Active tab

  dc_api_client.py         Helm (DC) Public API client -- order lookup, phone numbers, etc.
  google_sheets_client.py, google_drive_client.py, _google_auth_once.py
                            Shared Google OAuth (Sheets + Drive), copied from the Amazon A-Z project
  defect_sheet_client.py    ODR sheet client (Active/Archived tabs, SPREADSHEET_ID)
  carrier_claims_sheet_client.py, sage_refund_sheet_client.py
                            Other sheet clients copied over, not currently used by chase-up
  asana_client.py           Asana API client, copied over (project GIDs in .env)

run_chaseup.bat            Entry point for Windows Task Scheduler
.env                        Credentials (gitignored) -- see below
.data/google_credentials/   Google OAuth token + client secret (gitignored)
.data/state/                Local cursors: active_row_count.json, reply_check_cursor.json
```

## One-time setup

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt     # or: google-auth google-auth-oauthlib
                                                   #     google-api-python-client httpx python-dotenv

cd whatsapp-service
npm install
```

`.env` must have (see `.env` in this repo for current values):
- `ASANA_*` -- Asana access token + project GIDs
- `DC_API_BASE_URL`, `DC_EMAIL`, `DC_PASSWORD` -- Helm Public API
- `NOTIFY_EMAIL_FROM`, `NOTIFY_EMAIL_APP_PASSWORD`, `NOTIFY_EMAIL_TO` -- run-summary emails

Google auth: run `python src/_google_auth_once.py` once to create
`.data/google_credentials/token.json` (needed by every sheet/drive client).

## Running the WhatsApp service

```
cd whatsapp-service
node index.js
```

First run (or after deleting `auth_info/`) needs a fresh QR scan: open
**http://localhost:3001/qr** in a browser (auto-refreshes every 20s), or
read the ASCII QR printed to the terminal. Scan with WhatsApp -> Linked
Devices -> Link a Device. Must stay running continuously -- it's the only
thing capturing incoming replies in real time (logged to
`whatsapp-service/incoming_messages.json`), and the only way messages get sent.

Check it's connected: `curl http://localhost:3001/status` -> `{"connected":true}`.

## Running the chase-up pipeline

```
run_chaseup.bat
```

(or `venv\Scripts\python.exe src\chaseup_run.py` directly). Requires the
WhatsApp service to be running and connected -- if it's not, sync and the
summary email still happen, but sending/checking is skipped and flagged as
an error in the email.

### Scheduling (7am / 4pm daily)

```
schtasks /create /tn "WhatsApp ChaseUp 7AM" /tr "\"C:\Users\theekshana.n\Projects\Whatsapp Chase up\run_chaseup.bat\"" /sc daily /st 07:00 /f
schtasks /create /tn "WhatsApp ChaseUp 4PM" /tr "\"C:\Users\theekshana.n\Projects\Whatsapp Chase up\run_chaseup.bat\"" /sc daily /st 16:00 /f
```

The WhatsApp service itself is **not** scheduled this way -- it needs to run
continuously (24/7), separately from the twice-daily chase-up job, so it
doesn't miss replies in between runs.

## The ledger

A Google Sheet ("Customer Chase-Up Ledger", in a "Customer Chase-Up" Drive
folder), one row per order:

| Column | Meaning |
|---|---|
| Order ID | Amazon order number |
| Phone | Customer phone, normalized to E.164 (`+<country code><number>`) |
| Customer Name | From Helm's shipping address |
| Has WhatsApp | Yes / No / Unknown |
| Attempt | How many chase-up messages sent so far (0-4) |
| Last Sent Date | Date of the most recent send |
| Next Due Date | When attempt #(n+1) is due (7 days after Last Sent Date) |
| Replied | Yes/No |
| Reply Date | When they replied |
| Status | Pending / Replied / Exhausted (4 attempts, no reply) / No WhatsApp / Ignored |

`Ignored` is used for orders that existed before the row-number baseline was
set and are deliberately excluded from chase-up (see `track_new_active_rows.py`).

## Flow per run

1. **Sync** -- any Active-tab rows added since the last run get a Helm phone
   lookup and a new `Pending` ledger row.
2. **WhatsApp check** -- `Unknown` rows get checked once; `No` -> `Status: No WhatsApp`, never sent to.
3. **Send** -- any `Pending` + `Has WhatsApp: Yes` row that's due gets messaged; attempt count and next due date advance; attempt 4 -> `Status: Exhausted`.
4. **Reply check** -- any `Pending`/`Exhausted` row whose phone sent a WhatsApp message since the last check -> `Status: Replied` (stops chase-up for every order tied to that phone, not just one).
5. **Email summary** -- HTML run report sent regardless of outcome.

## Known limitations

- Reply matching is by phone number only -- one reply stops chase-up for
  *every* order tied to that customer's number, not just the one they're
  replying about (by design, see conversation history).
- If the WhatsApp service is down, replies sent during that gap are
  permanently missed -- there's no way to recover them after the fact.
- A handful of phone numbers in Helm's own data are malformed (extra
  digits, duplicated country code) and can't be auto-corrected; these just
  fail the WhatsApp check harmlessly.

## Web UI

`python src/chaseup_ui.py` then open **http://localhost:8111** -- shows the ledger and WhatsApp status, with **Fetch new orders** and **Chase up** buttons (Chase up asks for confirmation before sending).

