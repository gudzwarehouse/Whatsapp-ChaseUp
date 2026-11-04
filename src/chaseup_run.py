"""Entry point for the scheduled chase-up run (7am/4pm) -- run_chaseup.bat
calls this. Syncs new orders, checks WhatsApp availability, sends due
messages, checks for replies, then emails an HTML summary either way.
"""
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaseup_sync as sync_mod
import chaseup_send as send_mod
import chaseup_reply_check as reply_mod
import chaseup_notifications as notify


def main() -> None:
    run_label = datetime.now().strftime("%Y-%m-%d %H:%M")
    errors = []

    sync_result = {"added": [], "lookup_failed": []}
    try:
        sync_result = sync_mod.sync()
    except Exception:
        errors.append(f"Sync step failed: {traceback.format_exc(limit=1)}")

    wa_result = {"checked": [], "no_whatsapp": [], "errors": []}
    send_result = {"sent": [], "exhausted": [], "errors": []}
    if not send_mod._bridge_connected():
        errors.append("WhatsApp bridge not connected (whatsapp-service not running or not logged in) -- skipped sending.")
    else:
        try:
            wa_result = send_mod.check_whatsapp_availability()
            errors.extend(wa_result["errors"])
        except Exception:
            errors.append(f"WhatsApp check step failed: {traceback.format_exc(limit=1)}")
        try:
            send_result = send_mod.send_due_messages()
            errors.extend(send_result["errors"])
        except Exception:
            errors.append(f"Send step failed: {traceback.format_exc(limit=1)}")

    replied_rows = []
    try:
        replied_rows = reply_mod.check_replies()
    except Exception:
        errors.append(f"Reply-check step failed: {traceback.format_exc(limit=1)}")

    all_errors = errors + sync_result["lookup_failed"]
    result = notify.send_chaseup_summary_email(
        run_label=run_label,
        new_orders_added=len(sync_result["added"]),
        whatsapp_checked=len(wa_result["checked"]),
        no_whatsapp=len(wa_result["no_whatsapp"]),
        messages_sent=len(send_result["sent"]),
        replied=len(replied_rows),
        exhausted=len(send_result["exhausted"]),
        sent_details=send_result["sent"],
        replied_details=[f"{r['Order ID']} ({r['Phone']})" for r in replied_rows],
        exhausted_details=send_result["exhausted"],
        no_whatsapp_details=wa_result["no_whatsapp"],
        error_details=all_errors,
    )
    print(f"Run complete. Email sent: {result['success']}" + (f" ({result['error']})" if not result["success"] else ""))


if __name__ == "__main__":
    main()
