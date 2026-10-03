"""Email delivery services for VEditor.

Provides email dispatch capabilities using Python standard library smtplib
with fallback to console logging when SMTP host is unset (local dev/testing).
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)


def send_verification_email(
    recipient: str,
    verify_url: str,
    expire_hours: int = 24,
) -> bool:
    """Send an account verification email with a signed one-click link.

    If SMTP is not configured (e.g. dev/CI environment), logs the link at INFO
    level and returns True to allow frictionless local development.
    """
    clean_email = recipient.strip().lower()

    if not settings.smtp_host:
        if settings.is_production:
            logger.error(
                "SMTP host is unconfigured in production; refusing verification email delivery to %s",
                clean_email,
            )
            return False
        logger.info(
            "SMTP host is unconfigured. Verification email for %s: %s",
            clean_email,
            verify_url,
        )
        return True

    msg = EmailMessage()
    msg["Subject"] = "Verify your email address - VEditor"
    msg["From"] = settings.smtp_from
    msg["To"] = clean_email

    text_body = (
        f"Welcome to VEditor!\n\n"
        f"Please verify your email address by clicking the link below:\n"
        f"{verify_url}\n\n"
        f"This link will expire in {expire_hours} hours.\n\n"
        f"If you did not create an account on VEditor, you can safely ignore this email."
    )
    msg.set_content(text_body)

    escaped_url = html.escape(verify_url, quote=True)
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #1e293b; background-color: #f8fafc; padding: 32px 16px; margin: 0;">
  <div style="max-width: 540px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 12px; padding: 36px 32px; background: #ffffff; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.04);">
    <h2 style="margin-top: 0; margin-bottom: 12px; color: #0f172a; font-size: 22px; font-weight: 700;">Verify your email address</h2>
    <p style="margin: 0 0 24px 0; color: #475569; font-size: 15px;">Welcome to VEditor! Please confirm your email address to activate your account.</p>
    <div style="text-align: center; margin: 32px 0;">
      <a href="{escaped_url}" style="background-color: #2563eb; color: #ffffff; text-decoration: none; padding: 13px 32px; border-radius: 8px; font-size: 15px; font-weight: 600; display: inline-block;">Verify Email</a>
    </div>
    <p style="color: #64748b; font-size: 13px; text-align: center; margin: 24px 0 8px 0;">This verification link will expire in {expire_hours} hours.</p>
    <p style="color: #64748b; font-size: 13px; text-align: center; margin: 0 0 28px 0;">
      If the button above does not work, you can also <a href="{escaped_url}" style="color: #2563eb; text-decoration: underline; font-weight: 500;">click here</a>.
    </p>
    <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 24px 0 16px 0;">
    <p style="color: #94a3b8; font-size: 12px; margin: 0; text-align: center;">If you did not create an account on VEditor, you can safely ignore this email.</p>
  </div>
</body>
</html>"""
    msg.add_alternative(html_body, subtype="html")

    ssl_context = ssl.create_default_context()
    try:
        if settings.smtp_ssl:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=ssl_context,
            ) as server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            ) as server:
                if settings.smtp_tls:
                    server.starttls(context=ssl_context)
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
        logger.info("Verification email sent successfully to %s", clean_email)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send verification email to %s: %s", clean_email, exc)
        return False
