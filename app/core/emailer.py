# import smtplib
# from email.mime.text import MIMEText
# from app.core.config import settings

# def send_email(to_email: str, subject: str, body: str) -> None:
#     msg = MIMEText(body, "plain", "utf-8")
#     msg['Subject'] = subject
#     msg['From'] = settings.SMTP_FROM
#     msg['To'] = to_email

#     with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
#         server.starttls()
#         if settings.SMTP_USER:
#             server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
#         server.send_message(msg)

# FILE: app/core/emailer.py
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, List, Optional, Tuple

from app.core.config import settings

# (filename, bytes_content, mime_type)
Attachment = Tuple[str, bytes, str]


def _get_from_email() -> str:
    """
    Decide FROM email:
    - Prefer settings.SMTP_FROM
    - Fallback to settings.SMTP_USER
    """
    from_email = getattr(settings, "SMTP_FROM", None) or getattr(
        settings, "SMTP_USER", None)
    if not from_email:
        raise RuntimeError(
            "No FROM email configured. Set SMTP_FROM or SMTP_USER in settings."
        )
    return from_email


def _build_message(
    to_email: str,
    subject: str,
    body: str,
    attachments: Optional[List[Attachment]] = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _get_from_email()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    if attachments:
        for filename, content, mime_type in attachments:
            maintype, _, subtype = (mime_type or
                                    "application/octet-stream").partition("/")
            if not maintype:
                maintype = "application"
                subtype = "octet-stream"
            msg.add_attachment(
                content,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

    return msg


def send_email(*args: Any, **kwargs: Any) -> None:
    """
    Flexible email helper.

    Supported styles:

    1) Old style (your current project):
       send_email("to@example.com", "Subject", "Body")

    2) Keyword style:
       send_email(to_email="to@example.com", subject="Subject", body="Body")

    3) With attachments:
       send_email(
           email_to="to@example.com",
           subject="Subject",
           body="Body",
           attachments=[("file.pdf", pdf_bytes, "application/pdf")]
       )

    Accepted recipient keys: to_email, email_to, to
    """

    # --- Extract attachments (if any) ---
    attachments: Optional[List[Attachment]] = kwargs.pop("attachments", None)

    # --- Positional args first (backward compatible) ---
    to_email: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None

    if len(args) >= 1:
        to_email = args[0]
    if len(args) >= 2:
        subject = args[1]
    if len(args) >= 3:
        body = args[2]

    # --- Then fall back to keyword args if needed ---
    if to_email is None:
        to_email = (kwargs.pop("to_email", None)
                    or kwargs.pop("email_to", None) or kwargs.pop("to", None))

    if subject is None:
        subject = kwargs.pop("subject", "")

    if body is None:
        body = kwargs.pop("body", "")

    if to_email is None:
        raise ValueError(
            "send_email: recipient is required (to_email / email_to / to)")

    # --- SMTP config from settings ---
    host = getattr(settings, "SMTP_HOST", None)
    port = int(getattr(settings, "SMTP_PORT", 587))
    user = getattr(settings, "SMTP_USER", None)
    password = getattr(settings, "SMTP_PASSWORD", None)
    use_tls = getattr(settings, "SMTP_TLS", True)

    if not host:
        raise RuntimeError("SMTP_HOST is not configured")

    msg = _build_message(to_email, subject, body, attachments=attachments)

    if use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.starttls(context=context)
            if user and password:
                server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as server:
            if user and password:
                server.login(user, password)
            server.send_message(msg)
