"""Portal-Export-Worker: Poll-Loop mit FIFO und Dedup (plan.md §6, TASK-009).

Zyklus: offene Aufträge laden → Dedup (FR-014, jüngster je Gruppe gewinnt,
ältere werden UEBERHOLT) → Verarbeitung strikt in createdAt-Reihenfolge.
process_order (TASK-014) verdrahtet Validierung → XML/ZIP → Upload →
Statusrückschreibung inkl. Retry/Backoff für Infrastrukturfehler (FR-016).
"""

import argparse
import ftplib
import logging
import os
import re
import time
from datetime import datetime, timezone

import portals
from openimmo import build_delete_xml, build_upsert_xml, build_zip
from twenty_client import BildFehler, TwentyClient, TwentyClientError, lade_bilder
from validate import validate

log = logging.getLogger("worker")

DEFAULT_POLL_INTERVAL = 60

# FR-016: Backoff-Stufen 60s / 5min / 15min, max. 3 Versuche. Indiziert nach
# dem AKTUELLEN versuchszaehler (0 → erste Wartezeit nach dem 1. Fehlschlag).
BACKOFF_SEKUNDEN = (60, 300, 900)
MAX_VERSUCHE = len(BACKOFF_SEKUNDEN)

# portal-SELECT-Wert (Twenty, Großschreibung) → portals.PORTALS-Registry-Key.
# Explizite Tabelle statt blindem .lower(), damit ein künftiger SELECT-Wert
# ohne 1:1-Kleinschreibungs-Entsprechung (z.B. Abkürzungen) nicht stillschweigend
# auf einen falschen Registry-Key fällt.
PORTAL_KEY_MAPPING = {
    "MEINESTADT": "meinestadt",
    "GLOIM": "gloim",
    "IMMOSCOUT24": "immoscout24",
}

# Statischer OpenImmo-Kontakt: Twenty hat (Stand TASK-014) kein strukturiertes
# Makler-Kontaktfeld an der Immobilie — Override je Umgebung über die
# PORTAL_KONTAKT_*-Variablen, Default ist der Geschäftsführer Interperform.
_KONTAKT_ENV_DEFAULTS = {
    "PORTAL_KONTAKT_EMAIL": "paul@interperform.de",
    "PORTAL_KONTAKT_NAME": "Hörmann",
    "PORTAL_KONTAKT_VORNAME": "Paul",
    "PORTAL_KONTAKT_FIRMA": "Interperform Real Estate UG",
}

# Anbietername für den OpenImmo-<anbieter>-Block — Override über Env, falls
# ein Portal (z.B. GLOIM) unter einer anderen Firma laufen sollte.
_PORTAL_FIRMA_DEFAULT = "Interperform Real Estate UG"

_PLZ_RE = re.compile(r"\b\d{5}\b")

# Twenty liefert DateTime-Strings mit wechselnder Fraktions-Stellenzahl —
# datetime.fromisoformat() lehnt das vor Python 3.11 ab, daher eigener,
# bewusst simpler Parser (Zeitzone wird als UTC angenommen, s. _parse_iso_utc).
_ISO_RE = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[T ]"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?:\.(?P<f>\d+))?"
)


def _dedup_key(order):
    # immobilieId ist stabiler als objektnummer; letztere ist der Fallback,
    # damit auch DELETE-Aufträge ohne Relation dedupliziert werden.
    return (order.get("immobilieId") or order.get("objektnummer"), order.get("portal"))


def plan_zyklus(orders):
    """Pure Dedup-/Sortierlogik: liefert (to_process, to_supersede).

    Je Gruppe (Immobilie×Portal) überlebt nur der jüngste Auftrag —
    ein neuerer Auftrag macht ältere fachlich gegenstandslos (FR-014).
    Die Verarbeitungsreihenfolge bleibt strikt FIFO nach createdAt,
    damit ein DELETE einen älteren UPSERT nie überholen kann.
    """
    sorted_orders = sorted(orders, key=lambda o: (o.get("createdAt") or "", o.get("id") or ""))
    latest_per_group = {}
    for order in sorted_orders:
        latest_per_group[_dedup_key(order)] = order["id"]

    to_process = []
    to_supersede = []
    for order in sorted_orders:
        if latest_per_group[_dedup_key(order)] == order["id"]:
            to_process.append(order)
        else:
            to_supersede.append(order)
    return to_process, to_supersede


def _parse_iso_utc(ts):
    """Robuster ISO-8601-Parser für Twenty-Zeitstempel, s. Modulkopf.

    Twenty liefert ausschließlich UTC ("Z"-Suffix bzw. äquivalent) — die
    Zeitzone wird deshalb bewusst nicht aus dem String gelesen, sondern
    fix auf UTC gesetzt (einfachste robuste Variante, s. Rückgabepunkt ②).
    """
    if not ts:
        return None
    m = _ISO_RE.match(ts)
    if not m:
        return None
    frac = (m.group("f") or "0")
    micros = int((frac + "000000")[:6])
    return datetime(
        int(m.group("y")), int(m.group("mo")), int(m.group("d")),
        int(m.group("h")), int(m.group("mi")), int(m.group("s")),
        micros, tzinfo=timezone.utc,
    )


def _jetzt_iso_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_backoff(order):
    """True, wenn der letzte Fehlversuch jünger ist als die Backoff-Stufe.

    Persistenz-Variante (② der Aufgabe): kein eigenes Zeitfeld nötig — jeder
    Fehlversuch schreibt versuchszaehler per update_order, was Twentys
    updatedAt automatisch mitzieht. updatedAt dient damit als "Zeitpunkt des
    letzten Fehlversuchs"; das übersteht Worker-Neustarts, weil es serverseitig
    in Twenty steht statt im Prozessspeicher.
    """
    versuch = order.get("versuchszaehler") or 0
    if versuch <= 0 or versuch > len(BACKOFF_SEKUNDEN):
        return False
    letzter_versuch = _parse_iso_utc(order.get("updatedAt"))
    if letzter_versuch is None:
        return False
    stufe = BACKOFF_SEKUNDEN[versuch - 1]
    vergangen = (datetime.now(timezone.utc) - letzter_versuch).total_seconds()
    return vergangen < stufe


def _portal_key(order):
    portal = (order.get("portal") or "").upper()
    return PORTAL_KEY_MAPPING.get(portal, portal.lower())


def _openimmo_config(portal_registry_entry):
    """Baut aus einem portals.PORTALS-Eintrag (FTP-/Upload-Registry: host,
    port, user, password_env, encoding, anbieternr, aktiv) die openimmo.
    PortalConfig, die build_upsert_xml/build_delete_xml erwarten.

    Sauberes Mapping statt direkter Weitergabe: die beiden Dicts haben
    unterschiedliche Felder (openimmo.PortalConfig kennt z.B. "firma" und
    keine FTP-Zugangsdaten) — PortalConfig(**portal_registry_entry) würde
    an den zusätzlichen FTP-Keys (host/port/user/...) mit TypeError scheitern.
    """
    return {
        "anbieternr": portal_registry_entry["anbieternr"],
        "firma": os.environ.get("PORTAL_FIRMA", _PORTAL_FIRMA_DEFAULT),
        "encoding": portal_registry_entry.get("encoding", "utf-8"),
    }


def immobilie_zu_openimmo_dict(immobilie):
    """Adapter Twenty-Immobilie-Dict → Feld-Dict für openimmo.build_upsert_xml.

    Pure Funktion (③ der Aufgabe) — keine Twenty-/Netzzugriffe, nur Env für
    den statischen Kontakt (s. _KONTAKT_ENV_DEFAULTS), deshalb ohne Mocks testbar.
    """
    adresse = immobilie.get("adresse") or ""
    plz_match = _PLZ_RE.search(adresse)
    plz = plz_match.group(0) if plz_match else ""
    # Ort: Twenty hat kein eigenes Ort-Feld — grobe Heuristik über den Text
    # nach der PLZ; ohne PLZ-Treffer bleibt ort bewusst leer statt zu raten.
    ort = ""
    if plz_match:
        rest = adresse[plz_match.end():].strip(" ,")
        ort = rest.split(",")[0].strip() if rest else ""

    ist_miete = immobilie.get("vermarktungsart") == "MIETE"
    # Die validierte Vorlage (TASK-011) kennt nur EINEN Preisknoten
    # (preise/kaufpreis) — bei MIETE landet die Nettokaltmiete pragmatisch
    # im selben Feld, weil das Portal so oder so einen Preis erwartet
    # (dokumentierte Einschränkung, s. Rückgabepunkt ⑦ offene Punkte).
    betrag = immobilie.get("nettokaltmiete" if ist_miete else "kaufpreis") or {}
    micros = betrag.get("amountMicros") if isinstance(betrag, dict) else None
    preis = (micros / 1_000_000) if micros else None
    waehrung = (betrag.get("currencyCode") if isinstance(betrag, dict) else None) or "EUR"

    return {
        "plz": plz,
        "ort": ort,
        "bundesland": "",  # in Twenty (Stand TASK-014) nicht separat gepflegt
        "kontakt_email": os.environ.get("PORTAL_KONTAKT_EMAIL", _KONTAKT_ENV_DEFAULTS["PORTAL_KONTAKT_EMAIL"]),
        "kontakt_name": os.environ.get("PORTAL_KONTAKT_NAME", _KONTAKT_ENV_DEFAULTS["PORTAL_KONTAKT_NAME"]),
        "kontakt_vorname": os.environ.get("PORTAL_KONTAKT_VORNAME", _KONTAKT_ENV_DEFAULTS["PORTAL_KONTAKT_VORNAME"]),
        "kontakt_firma": os.environ.get("PORTAL_KONTAKT_FIRMA", _KONTAKT_ENV_DEFAULTS["PORTAL_KONTAKT_FIRMA"]),
        "kaufpreis": preis,
        "waehrung": waehrung,
        "wohnflaeche": immobilie.get("wohnflaeche"),
        "anzahl_zimmer": immobilie.get("zimmer"),
        "objekttitel": immobilie.get("name") or "",
        "objektbeschreibung": "",  # kein Freitext-Feld an der Twenty-Immobilie
        "nutzungsart_wohnen": True,   # Interperform vermittelt ausschließlich Wohnimmobilien
        "nutzungsart_gewerbe": False,
        "vermarktungsart_kauf": not ist_miete,
        "vermarktungsart_miete_pacht": ist_miete,
        "wohnungtyp": "ETAGE",  # kein Objektart-Feld an der Twenty-Immobilie, s. offene Punkte
    }


# Ausnahmen, die einen Upload-Fehlversuch als Infrastrukturfehler qualifizieren
# (FR-016) — TwentyClientError (Statusrückschreibung könnte scheitern, wird
# hier aber nicht gefangen, s. _upload_mit_retry) plus alles, was ftplib bei
# Netzwerk-/Verbindungsproblemen wirft (Error, OSError, EOFError).
_INFRA_FEHLER = (TwentyClientError,) + ftplib.all_errors


def _infra_fehlversuch(order, twenty, exc):
    """Erhöht versuchszaehler persistent; ab MAX_VERSUCHE → endgültig FEHLER.

    Kein time.sleep() hier (Poll-Kompatibilität, s. Modulkopf/_in_backoff):
    der Auftrag bleibt AUSSTEHEND, der nächste Zyklus überspringt ihn per
    _in_backoff, bis die Backoff-Stufe verstrichen ist.
    """
    versuch = (order.get("versuchszaehler") or 0) + 1
    meldung = "Transienter Fehler bei Versuch %d/%d: %s" % (versuch, MAX_VERSUCHE, exc)
    if versuch >= MAX_VERSUCHE:
        twenty.update_order(order["id"], status="FEHLER", fehlermeldung=meldung,
                           versuchszaehler=versuch)
        return "fehler (Retries ausgeschoepft nach %d Versuchen): %s" % (versuch, exc)
    twenty.update_order(order["id"], versuchszaehler=versuch, fehlermeldung=meldung)
    return "transienter Fehler, Versuch %d/%d (Backoff bis naechster Zyklus): %s" % (
        versuch, MAX_VERSUCHE, exc)


def _merge_warnhinweis(bestehender, warnungen):
    """Hängt neue Warnungen an einen evtl. vorhandenen warnhinweis an.

    RA-1 kann den Erst-Übermittlungs-Hinweis (FR-007a) vorbefüllt haben —
    der bleibt als Präfix erhalten, GEG-/Bild-Warnungen werden nur ergänzt,
    nie überschrieben.
    """
    teile = []
    bestehender = (bestehender or "").strip()
    if bestehender:
        teile.append(bestehender)
    if warnungen:
        teile.append("; ".join(warnungen))
    return " | ".join(teile) if teile else None


def _process_delete(order, twenty, portals, dry_run):
    """DELETE-Kern: NIE get_immobilie, KEINE Validierung, KEINE Bilder —
    ausschließlich order["objektnummer"] (Review-Befund plan.md §3/WF-4)."""
    if not dry_run and _in_backoff(order):
        return "uebersprungen (Backoff aktiv, versuchszaehler=%s)" % order.get("versuchszaehler")

    portal_key = _portal_key(order)
    config = _openimmo_config(portals.PORTALS[portal_key])

    xml_bytes = build_delete_xml(order, config)
    zip_name, zip_bytes = build_zip(xml_bytes, [], config, order["objektnummer"])

    if dry_run:
        return "dry-run: DELETE-XML/ZIP gebaut (%s) — kein Upload, kein Statusschreiben" % zip_name

    try:
        portals.upload(portal_key, zip_name, zip_bytes)
    except _INFRA_FEHLER as exc:
        return _infra_fehlversuch(order, twenty, exc)

    twenty.update_order(order["id"], status="ENTFERNT", letzterExport=_jetzt_iso_utc(),
                       fehlermeldung=None)
    return "entfernt (%s)" % zip_name


def _process_upsert(order, twenty, portals, dry_run):
    if not dry_run and _in_backoff(order):
        return "uebersprungen (Backoff aktiv, versuchszaehler=%s)" % order.get("versuchszaehler")

    portal_key = _portal_key(order)
    config = _openimmo_config(portals.PORTALS[portal_key])

    immobilie = twenty.get_immobilie(order["immobilieId"])
    blockers, warnungen = validate(immobilie)
    if blockers:
        meldung = "; ".join(blockers)
        if dry_run:
            return "dry-run: Validierungsfehler (kein Statusschreiben): %s" % meldung
        twenty.update_order(order["id"], status="FEHLER", fehlermeldung=meldung)
        return "fehler (Validierung, kein Retry): %s" % meldung

    if dry_run:
        # Bilder-Download bewusst übersprungen: Dry-Run soll rein lesend
        # bleiben, ohne Attachment-URLs von Twenty zu ziehen (s. Rückgabe ⑤).
        bilder, bild_warnungen = [], []
    else:
        try:
            bilder, bild_warnungen = lade_bilder(twenty, order["immobilieId"])
        except BildFehler as exc:
            twenty.update_order(order["id"], status="FEHLER", fehlermeldung=str(exc))
            return "fehler (Bildfehler, kein Retry): %s" % exc

    feld_dict = immobilie_zu_openimmo_dict(immobilie)
    xml_bytes = build_upsert_xml(feld_dict, order, config, attachments=bilder)
    zip_name, zip_bytes = build_zip(xml_bytes, bilder, config, order["objektnummer"])

    if dry_run:
        return ("dry-run: XML/ZIP gebaut (%s), %d Blocker/%d Warnungen — kein Upload"
                % (zip_name, len(blockers), len(warnungen)))

    try:
        portals.upload(portal_key, zip_name, zip_bytes)
    except _INFRA_FEHLER as exc:
        return _infra_fehlversuch(order, twenty, exc)

    neuer_hinweis = _merge_warnhinweis(order.get("warnhinweis"), list(warnungen) + list(bild_warnungen))
    twenty.update_order(
        order["id"],
        status="UEBERMITTELT",
        letzterExport=_jetzt_iso_utc(),
        warnhinweis=neuer_hinweis,
        fehlermeldung=None,
    )
    return "uebermittelt (%s)" % zip_name


def process_order(order, twenty, portals, dry_run):
    """Verarbeitet einen Auftrag; liefert einen Ergebnis-String fürs Log.

    DELETE-Regel (kritisch): ein DELETE lädt NIE die Immobilie und
    durchläuft KEINE Validierung — es zählt allein order["objektnummer"],
    denn die Immobilie kann im CRM bereits gelöscht/verändert sein.
    """
    if order.get("aktion") == "DELETE":
        return _process_delete(order, twenty, portals, dry_run)
    return _process_upsert(order, twenty, portals, dry_run)


def run_cycle(twenty, dry_run):
    orders = twenty.fetch_open_orders()
    log.info("Zyklus: %d offene Aufträge geladen", len(orders))
    to_process, to_supersede = plan_zyklus(orders)

    for order in to_supersede:
        if dry_run:
            log.info(
                "[dry-run] Auftrag %s (%s/%s, objektnummer=%s) würde auf UEBERHOLT gesetzt",
                order["id"], order.get("portal"), order.get("aktion"), order.get("objektnummer"),
            )
            continue
        try:
            twenty.update_order(order["id"], status="UEBERHOLT")
            log.info("Auftrag %s auf UEBERHOLT gesetzt (jüngerer Auftrag vorhanden)", order["id"])
        except TwentyClientError as exc:
            # Fehler beim Supersede darf den Zyklus nicht stoppen — der
            # Auftrag bleibt AUSSTEHEND und wird im nächsten Lauf erneut geplant.
            log.error("UEBERHOLT-Update für %s fehlgeschlagen: %s", order["id"], exc)

    for order in to_process:
        start = time.monotonic()
        try:
            ergebnis = process_order(order, twenty, portals, dry_run)
        except Exception as exc:  # noqa: BLE001 — ein Auftrag darf den Loop nie crashen
            ergebnis = "fehler: %s" % exc
            log.exception("Auftrag %s fehlgeschlagen", order["id"])
        dauer = time.monotonic() - start
        log.info(
            "order_id=%s portal=%s aktion=%s ergebnis=%r dauer=%.2fs",
            order["id"], order.get("portal"), order.get("aktion"), ergebnis, dauer,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Portal-Export-Worker für Twenty CRM")
    parser.add_argument("--once", action="store_true", help="nur einen Zyklus ausführen")
    parser.add_argument("--dry-run", action="store_true",
                        help="keine Schreibvorgänge in Twenty, nur Log")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("WORKER_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL))
    twenty = TwentyClient()

    while True:
        try:
            run_cycle(twenty, dry_run=args.dry_run)
        except TwentyClientError as exc:
            # Transiente API-/Netzfehler: loggen und nächsten Poll abwarten.
            log.error("Zyklus fehlgeschlagen: %s", exc)
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
