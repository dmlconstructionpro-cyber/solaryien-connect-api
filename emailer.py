"""
Email sender (stdlib smtplib).

Sends from EMAIL_FROM (default Circe-lilitu@solaryien.com). If SMTP_HOST is not
configured, sending is a logged no-op so the app runs fine without an email
provider — wire SMTP_* env vars to enable real delivery.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage

import config

log = logging.getLogger("solaryien.emailer")


def send(to, subject, body, from_addr=None):
    """Return True if an email was actually dispatched, False if skipped."""
    from_addr = from_addr or config.EMAIL_FROM
    if not to:
        return False
    if not config.SMTP_HOST:
        log.info("EMAIL (SMTP not configured) from %s -> %s: %s", from_addr, to, subject)
        return False
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
            s.starttls(context=ssl.create_default_context())
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASS or "")
            s.send_message(msg)
        log.info("EMAIL sent from %s -> %s: %s", from_addr, to, subject)
        return True
    except Exception as e:
        log.warning("EMAIL send failed -> %s: %s (%s)", to, subject, e)
        return False
