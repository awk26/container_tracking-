"""Sends a container-tracking summary to a customer over email.

Configured entirely via environment variables (loaded from a local .env
file if present, via python-dotenv) so no real credentials ever need to
live in the repo:
  SMTP_HOST      - default smtp.gmail.com (this project's mailbox,
                   aisupport@jmbaxi.com, is Google Workspace-hosted; set
                   this explicitly if that ever changes)
  SMTP_PORT      - default 587 (STARTTLS)
  SMTP_USERNAME  - login user, e.g. aisupport@jmbaxi.com
  APP_PASSWORD   - Google-style per-app password (preferred - required
                   once 2FA is on, which Workspace accounts normally have)
  SMTP_PASSWORD  - plain account password, used only if APP_PASSWORD isn't set
  SMTP_FROM      - the From: address (defaults to SMTP_USERNAME)
  SMTP_USE_TLS   - "true"/"false", default "true"

The email body is always built here from a tracker result this server
fetched itself (see run_tracker() in app.py) - never from client-supplied
content - so this can't be used as a generic "send arbitrary text to
arbitrary addresses" relay.
"""

import os
import re
import smtplib
from email.message import EmailMessage
from html import escape as _esc

from dotenv import load_dotenv

load_dotenv()


class EmailConfigError(Exception):
    pass


class EmailSendError(Exception):
    pass


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str) -> bool:
    value = (value or "").strip()
    if not value or len(value) > 254 or "\n" in value or "\r" in value:
        return False
    return bool(_EMAIL_RE.match(value))


def _smtp_settings():
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME") or None
    # APP_PASSWORD (Google's term for a per-app password under 2FA) takes
    # priority over a plain SMTP_PASSWORD when both happen to be set.
    password = os.environ.get("APP_PASSWORD") or os.environ.get("SMTP_PASSWORD") or None
    from_addr = os.environ.get("SMTP_FROM") or username
    use_tls = os.environ.get("SMTP_USE_TLS", "true").strip().lower() not in ("false", "0", "no")

    if not from_addr:
        raise EmailConfigError(
            "Email sharing isn't configured on this server yet "
            "(SMTP_USERNAME / SMTP_FROM is not set)."
        )
    if not password:
        raise EmailConfigError(
            "Email sharing isn't configured on this server yet "
            "(APP_PASSWORD / SMTP_PASSWORD is not set)."
        )
    return host, port, username, password, from_addr, use_tls


def _format_body(result, note):
    lines = []
    if note:
        lines.append(note.strip())
        lines.append("")

    lines.append(
        f"{result.get('line_name', 'Carrier')} tracking for container "
        f"{result.get('container_number', '-')}"
    )
    lines.append("")

    for field in result.get("summary_fields") or []:
        if not isinstance(field, dict):
            continue
        label, value = field.get("label"), field.get("value")
        if label and value:
            lines.append(f"{label}: {value}")

    events = result.get("events") or []
    event_columns = result.get("event_columns") or []
    if events:
        lines.append("")
        lines.append("Recent events:")
        for row in events[:10]:
            if isinstance(row, dict):
                values = [str(row.get(col, "")) for col in event_columns]
            else:
                values = [str(v) for v in row]
            line = " | ".join(v for v in values if v and v != "-")
            if line:
                lines.append(f"  - {line}")

    source_url = result.get("source_url")
    if source_url:
        lines.append("")
        lines.append(f"View directly on the carrier's site: {source_url}")

    return "\n".join(lines)


def _format_html_body(result, note):
    parts = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;">']

    if note:
        parts.append(f"<p>{_esc(note)}</p>")

    parts.append(
        f"<h2 style='margin-bottom:8px;'>{_esc(result.get('line_name', 'Carrier'))} tracking "
        f"for container {_esc(result.get('container_number', '-'))}</h2>"
    )

    summary_fields = [
        f for f in (result.get("summary_fields") or [])
        if isinstance(f, dict) and f.get("label") and f.get("value")
    ]
    if summary_fields:
        rows = "".join(
            f"<tr>"
            f"<td style='padding:4px 12px 4px 0;color:#666;white-space:nowrap;'>{_esc(f['label'])}</td>"
            f"<td style='padding:4px 0;font-weight:600;'>{_esc(f['value'])}</td>"
            f"</tr>"
            for f in summary_fields
        )
        parts.append(f"<table style='border-collapse:collapse;margin-bottom:18px;'>{rows}</table>")

    events = result.get("events") or []
    event_columns = result.get("event_columns") or []
    if events:
        header = "".join(
            f"<th style='text-align:left;padding:6px 10px;border-bottom:2px solid #ccc;"
            f"background:#1d242a;color:#fff;'>{_esc(col)}</th>"
            for col in event_columns
        )
        body_rows = []
        for row in events[:20]:
            if isinstance(row, dict):
                values = [row.get(col, "") for col in event_columns]
            else:
                values = list(row)
            cells = "".join(
                f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{_esc(str(v)) if v else '-'}</td>"
                for v in values
            )
            body_rows.append(f"<tr>{cells}</tr>")
        parts.append(
            "<p style='margin-bottom:6px;font-weight:600;'>Recent events</p>"
            f"<table style='border-collapse:collapse;width:100%;'>"
            f"<thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
        )

    source_url = result.get("source_url")
    if source_url:
        safe_url = _esc(source_url)
        parts.append(f"<p style='margin-top:18px;'><a href='{safe_url}'>View directly on the carrier's site</a></p>")

    parts.append("</div>")
    return "".join(parts)


def send_tracking_email(to_email: str, result: dict, note: str = "") -> None:
    if not is_valid_email(to_email):
        raise ValueError("Please enter a valid email address.")

    host, port, username, password, from_addr, use_tls = _smtp_settings()

    msg = EmailMessage()
    msg["Subject"] = (
        f"Container tracking update - {result.get('container_number', '')} "
        f"({result.get('line_name', '')})"
    )
    msg["From"] = from_addr
    msg["To"] = to_email
    msg.set_content(_format_body(result, note))
    msg.add_alternative(_format_html_body(result, note), subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            if use_tls:
                server.starttls()
            if username and password:
                server.login(username, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(f"Couldn't send the email: {exc}") from exc
