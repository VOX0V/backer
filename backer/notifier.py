"""
Envoi d'e-mails d'alerte (SMTP, par exemple Gmail avec un mot de passe d'application).

Réglages dans le .env :
  SMTP_HOST      (défaut smtp.gmail.com)
  SMTP_PORT      (défaut 587 ; 465 = SSL direct)
  SMTP_USER      adresse de connexion
  SMTP_PASSWORD  mot de passe d'application
  ALERT_FROM     expéditeur (défaut : SMTP_USER)
  ALERT_TO       destinataire(s), séparés par des virgules
"""
import os
import smtplib
import ssl
from email.message import EmailMessage


def _recipients():
    return [a.strip() for a in os.environ.get("ALERT_TO", "").split(",") if a.strip()]


def is_configured():
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD") and _recipients())


def send_email(subject, body):
    """Retourne (True, None) si envoyé, sinon (False, message d'erreur)."""
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        return False, "SMTP_PORT must be a number."
    user = os.environ.get("SMTP_USER", "")
    # Google affiche les mots de passe d'application par blocs séparés par des espaces.
    password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "")
    to = _recipients()
    sender = os.environ.get("ALERT_FROM", "") or user

    if not (user and password and to):
        return False, "SMTP_USER, SMTP_PASSWORD and ALERT_TO must be set in .env."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(user, password)
                server.send_message(msg)
    except Exception as exc:  # noqa: BLE001 -- on renvoie le message tel quel à l'interface
        return False, str(exc)
    return True, None
