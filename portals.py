"""Portal-Registry und FTPS-Upload für den Portal-Export (plan.md §1/§9, ADR-005).

Sicherheitsmodell:
- Passwörter stehen NIE im Code — nur der Name der Env-Var (``password_env``).
  Zur Laufzeit wird das Passwort aus ``os.environ`` gelesen.
- Nur FTPS (explizites AUTH TLS + PROT P). Klartext-FTP ist bewusst nicht
  erreichbar — es gibt keinen Fallback-Pfad ohne TLS.
- Fehlermeldungen enthalten grundsätzlich keine Passwörter; ftplib-Exceptions
  werden unverändert durchgereicht (sie tragen keine Credentials).
"""

import ftplib
import io
import os
import ssl

# Registry aller Zielportale. ``password_env`` ist der NAME der Umgebungs-
# variable, nicht das Passwort selbst — Credentials bleiben aus dem Repo raus.
PORTALS = {
    "meinestadt": {
        "host": "ftp04.meinestadt.de",
        "port": 21,
        "user": "51266",
        "password_env": "MEINESTADT_FTP_PASSWORD",
        "encoding": "utf-8",
        "anbieternr": "51266",
        "aktiv": True,
    },
    # GLOIM ist vorbereitet, aber noch nicht freigeschaltet — Platzhalter,
    # bis Zugangsdaten vom Portal vorliegen.
    "gloim": {
        "host": "ftp.gloim.example",
        "port": 21,
        "user": "PLATZHALTER",
        "password_env": "GLOIM_FTP_PASSWORD",
        "encoding": "utf-8",
        "anbieternr": "PLATZHALTER",
        "aktiv": False,
    },
}


def _ssl_context() -> ssl.SSLContext:
    # Spike-S1-Befund (Stand 2026-07-23): ftp04.meinestadt.de liefert die
    # Zertifikatskette unvollständig aus UND das Serverzertifikat ist
    # abgelaufen. Peer-Verifikation würde daher jede Verbindung blockieren.
    # Bewusste Entscheidung (ADR-005): Verifikation deaktivieren, die
    # TLS-Verschlüsselung selbst (Kontroll- UND Datenkanal via PROT P)
    # bleibt vollständig aktiv.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def upload(portal_key: str, filename: str, data: bytes) -> None:
    """Lädt ``data`` als ``filename`` per FTPS auf das Portal ``portal_key``."""
    if portal_key not in PORTALS:
        raise KeyError(f"Unbekanntes Portal: {portal_key!r}")
    portal = PORTALS[portal_key]

    if not portal["aktiv"]:
        raise RuntimeError(f"Portal {portal_key} ist nicht aktiviert")

    password_env = portal["password_env"]
    password = os.environ.get(password_env)
    if not password:
        # Nur den Variablennamen nennen — nie einen Wert.
        raise RuntimeError(
            f"Umgebungsvariable {password_env} ist nicht gesetzt "
            f"(FTP-Passwort für Portal {portal_key})"
        )

    ftps = ftplib.FTP_TLS(context=_ssl_context())
    ftps.encoding = portal["encoding"]
    try:
        ftps.connect(portal["host"], portal["port"], timeout=60)
        # Explizites AUTH TLS vor dem Login, danach Datenkanal verschlüsseln.
        ftps.auth()
        ftps.login(portal["user"], password)
        ftps.prot_p()
        ftps.storbinary(f"STOR {filename}", io.BytesIO(data))
    finally:
        try:
            ftps.quit()
        except ftplib.all_errors:  # umfasst OSError und EOFError
            # Verbindung ist ggf. schon weg — Aufräumen darf nicht maskieren,
            # warum der Upload scheiterte.
            ftps.close()
