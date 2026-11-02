"""Run-notification emails for the WhatsApp chase-up pipeline -- HTML +
plaintext multipart over Gmail SMTP. Same look/markup/subject convention as
the Amazon A-Z project's src/email_notifications.py (render_run_notification_html,
ported verbatim) so every automated notification this team gets looks the
same regardless of which project sent it.
"""
from __future__ import annotations

import os
import smtplib
from datetime import date, datetime
from email.message import EmailMessage
from html import escape

from dotenv import load_dotenv

load_dotenv()

PROJECT_NAME = "WhatsApp Chase-Up"


def _config() -> tuple[str, str, list[str]] | None:
    load_dotenv(override=True)
    notify_from = os.getenv("NOTIFY_EMAIL_FROM", "").strip()
    notify_password = os.getenv("NOTIFY_EMAIL_APP_PASSWORD", "").strip()
    notify_to_raw = os.getenv("NOTIFY_EMAIL_TO", "").strip()
    if not notify_from or not notify_password or not notify_to_raw:
        return None
    recipients = [r.strip() for r in notify_to_raw.split(",") if r.strip()]
    if not recipients:
        return None
    return notify_from, notify_password, recipients


def email_subject(status: str, report_date: date | None = None) -> str:
    return f"{PROJECT_NAME} | {status} | {(report_date or date.today()).strftime('%d/%m')}"


def render_run_notification_html(
    *,
    status: str,
    details: list[tuple[str, object]],
    summary_groups: list[tuple[str, str, list[tuple[str, object]]]] | None = None,
    stage: str | None = None,
    error: str | None = None,
    title: str = f"{PROJECT_NAME} Run Report",
    footer: str = "This is an automated notification from the WhatsApp chase-up pipeline.",
) -> str:
    def rows(items: list[tuple[str, object]]) -> str:
        return "".join(
            '<tr><td style="padding:6px 10px;color:#6b7280;vertical-align:top;'
            'border-bottom:1px solid #e5e7eb;">'
            f'{escape(label)}</td><td style="padding:6px 10px;font-weight:600;'
            'text-align:right;white-space:nowrap;overflow-wrap:anywhere;'
            'border-bottom:1px solid #e5e7eb;">'
            f'{escape(str(value))}</td></tr>'
            for label, value in items
        )

    color = {"SUCCESS": "#15803d", "RETRYING": "#b45309", "PARTIAL": "#b45309", "WARNING": "#b45309"}.get(status, "#b91c1c")
    metadata = "<br>".join(
        f"{escape(label)}: <strong>{escape(str(value))}</strong>"
        for label, value in details
    )
    sections = ""
    for heading, accent, group_rows in summary_groups or []:
        sections += (
            f'<div style="background:#fafafa;border:1px solid #e5e7eb;border-left:4px solid {accent};'
            'border-radius:8px;padding:10px 14px;margin-bottom:10px;">'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;'
            f'color:{accent};margin-bottom:4px;">{escape(heading)}</div>'
            '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
            f'{rows(group_rows)}</table></div>'
        )
    if stage is not None or error is not None:
        sections += (
            '<div style="background:#fef2f2;border:1px solid #fca5a5;padding:12px;'
            'border-radius:6px;font-size:13px;color:#991b1b;margin-bottom:20px;">'
            f'<strong>Failed while:</strong> {escape(stage or "Unknown stage")}<br>'
            '<strong>Reason:</strong><pre style="white-space:pre-wrap;overflow-wrap:anywhere;'
            f'font-family:inherit;margin:4px 0 0;">{escape(error or "Unknown error")}</pre></div>'
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937;line-height:1.5;margin:0;padding:20px;background:#ffffff;">
<div style="max-width:700px;margin:0 auto;">
<div style="border-bottom:2px solid #e5e7eb;padding-bottom:12px;margin-bottom:16px;">
<h2 style="margin:0 0 6px;color:#111827;">{escape(title)}</h2>
<div style="font-size:13px;color:#6b7280;">{metadata}</div>
<div style="margin-top:8px;"><span style="background:{color};color:#ffffff;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;">STATUS: {escape(status)}</span></div>
</div>{sections}
<div style="font-size:12px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:12px;">{escape(footer)}</div>
</div></body></html>"""


def _send(subject: str, body: str, html_body: str) -> dict:
    cfg = _config()
    if cfg is None:
        return {"success": False, "error": "NOTIFY_EMAIL_FROM/APP_PASSWORD/TO not configured in .env"}
    notify_from, notify_password, recipients = cfg
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = notify_from
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        msg.add_alternative(html_body, subtype="html")

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(notify_from, notify_password)
            smtp.send_message(msg)

        return {"success": True, "error": None}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def send_chaseup_summary_email(
    *,
    run_label: str,
    new_orders_added: int,
    whatsapp_checked: int,
    no_whatsapp: int,
    messages_sent: int,
    replied: int,
    exhausted: int,
    sent_details: list[str] | None = None,
    replied_details: list[str] | None = None,
    exhausted_details: list[str] | None = None,
    no_whatsapp_details: list[str] | None = None,
    error_details: list[str] | None = None,
) -> dict:
    """Sends a summary of one chaseup_run.py run (the 7am/4pm scheduled job)."""
    sent_details = sent_details or []
    replied_details = replied_details or []
    exhausted_details = exhausted_details or []
    no_whatsapp_details = no_whatsapp_details or []
    error_details = error_details or []
    status = "SUCCESS" if not error_details else "PARTIAL"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = email_subject(status)

    body = "\n".join(
        [
            f"WhatsApp Chase-Up Run Summary -- {run_label}",
            f"Timestamp: {now_str}",
            "=" * 60,
            "",
            f"New orders added to ledger: {new_orders_added}",
            f"WhatsApp availability checked: {whatsapp_checked}",
            f"No WhatsApp (skipped): {no_whatsapp}",
            f"Chase-up messages sent: {messages_sent}",
            f"New replies detected: {replied}",
            f"Exhausted (4 attempts, no reply): {exhausted}",
            f"Errors: {len(error_details)}",
        ]
        + (["", "Messages sent:"] + [f"- {e}" for e in sent_details] if sent_details else [])
        + (["", "Replied:"] + [f"- {e}" for e in replied_details] if replied_details else [])
        + (["", "Exhausted:"] + [f"- {e}" for e in exhausted_details] if exhausted_details else [])
        + (["", "No WhatsApp:"] + [f"- {e}" for e in no_whatsapp_details] if no_whatsapp_details else [])
        + (["", "Errors:"] + [f"- {e}" for e in error_details] if error_details else [])
        + ["", "=" * 60, "This is an automated notification from the WhatsApp chase-up pipeline."]
    )

    html_body = render_run_notification_html(
        status=status,
        details=[("Run", run_label), ("Timestamp", now_str)],
        summary_groups=[
            (
                "Ledger",
                "#2563eb",
                [("New orders added", new_orders_added), ("WhatsApp checked", whatsapp_checked), ("No WhatsApp", no_whatsapp)],
            ),
            (
                "This run",
                "#15803d" if not error_details else "#b91c1c",
                [("Messages sent", messages_sent), ("New replies", replied), ("Exhausted (maxed out)", exhausted)],
            ),
        ]
        + ([("Messages sent", "#2563eb", [(f"{i + 1}", e) for i, e in enumerate(sent_details)])] if sent_details else [])
        + ([("Replied", "#15803d", [(f"{i + 1}", e) for i, e in enumerate(replied_details)])] if replied_details else [])
        + ([("Exhausted", "#b45309", [(f"{i + 1}", e) for i, e in enumerate(exhausted_details)])] if exhausted_details else [])
        + ([("No WhatsApp", "#6b7280", [(f"{i + 1}", e) for i, e in enumerate(no_whatsapp_details)])] if no_whatsapp_details else [])
        + ([("Errors", "#b91c1c", [(f"{i + 1}", e) for i, e in enumerate(error_details)])] if error_details else []),
        title=f"WhatsApp Chase-Up Run Summary -- {run_label}",
    )
    return _send(subject, body, html_body)


if __name__ == "__main__":
    print(send_chaseup_summary_email(
        run_label="Test",
        new_orders_added=0, whatsapp_checked=0, no_whatsapp=0,
        messages_sent=0, replied=0, exhausted=0,
    ))
