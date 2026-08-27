"""Website-Export-Kanal (``portal: WEBSITE``): JSON-Payload statt OpenImmo-XML,
signierter HTTPS-POST statt FTPS-Upload.

Gegenstück: WordPress-Plugin ``interperform-website-export`` — konkret
``includes/rest-api.php`` (Signatur-Prüfung, Route) und ``includes/mapping.php``
(Payload-Schema), siehe ``BRAIN-IPR/2026-08-26-konzept-website-export.md``
Abschnitt 3/4 für den vollständigen Kontext.

Schema-Wechsel 26.08. (zweiter Durchgang): Das WordPress-ACF-Schema wurde von
einer PropStack-Feld-Spiegelung auf ein komplett Twenty-natives Schema
umgestellt (siehe ``interperform-website-export/includes/acf-fields.php``).
``build_payload`` ist dadurch fast reines Durchreichen statt Übersetzung —
keine Adress-Zerlegung, keine Ausstattungs-Aggregation, keine Enum-Umbenennung
mehr nötig, weil die Feldnamen und Auswahlwerte auf beiden Seiten identisch
sind (Twenty-Rohwerte, z. B. "SANIERUNGSBEDUERFTIG").

Sicherheitsmodell (analog ``portals.py``): das Secret steht NIE im Code, nur
der Name der Env-Var (``SECRET_ENV``). ``aktiv`` bleibt ``False``, bis Secret,
Endpoint und WordPress-Route produktiv durchgetestet sind (Konzept Schritt 6),
analog zum GLOIM-Vorgehen in ``portals.py``.
"""

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "https://immobilientenor.de/wp-json/interperform/v1/immobilie"
URL_ENV = "WEBSITE_EXPORT_URL"
# Getrennter Endpoint für DELETE (Konzept Abschnitt 4b) — der Upsert-Endpoint
# verlangt zwingend title/fields, die ein DELETE-Auftrag (nur objektnummer,
# s. build_delete_payload) nicht hat.
DEFAULT_DELETE_URL = "https://immobilientenor.de/wp-json/interperform/v1/immobilie/entfernen"
DELETE_URL_ENV = "WEBSITE_DELETE_URL"
SECRET_ENV = "WEBSITE_WEBHOOK_SECRET"
SIGNATURE_HEADER = "X-Interperform-Signature"

# Erst nach Konzept-Schritt 6 (End-to-End-Test mit [TEST]-Objekt) auf True
# stellen — analog "aktiv": False bei GLOIM in portals.py.
AKTIV = False

# Twenty-Feldname → Payload-Key ist identisch (1:1-Schema) für alle Felder in
# diesen Listen. Muss mit interperform-website-export/includes/mapping.php
# (iprwe_number_fields/iprwe_boolean_fields/iprwe_text_fields/
# iprwe_array_fields/iprwe_rich_text_fields) übereinstimmen.

CURRENCY_FIELDS = (
    "kaufpreis", "nettokaltmiete", "betriebskosten", "gesamtmiete", "pacht",
    "bewertungVon", "bewertungBis", "tatsaechlicherPreis",
)

# Plain-NUMBER-Felder (keine Currency-Objekte in Twenty, s. Metadata-API 26.08.).
NUMBER_FIELDS = (
    "baujahr", "wohnflaeche", "grundstuecksflaeche", "zimmer", "schlafzimmer",
    "badezimmer", "stellplaetze", "anzahlWohneinheiten", "maklerprovision",
    "hausgeld", "energieverbrauchskennwert", "treibhausgasemission",
)

BOOLEAN_FIELDS = (
    "erbpacht", "kellerraumVorhanden", "aufzugVorhanden", "einbaukuecheVorhanden",
    "gartenVorhanden", "terrasseVorhanden", "denkmalschutz", "barrierefrei",
    "klimaanlageVorhanden", "kaminVorhanden", "poolVorhanden", "gaesteWcVorhanden",
    "einliegerwohnungVorhanden", "seniorenaufzugVorhanden",
    "tiefgaragenstellplatzVorhanden", "loggiaVorhanden", "vermietet", "energieausweis",
)

TEXT_FIELDS = (
    "adresse", "bezirk", "vermarktungsart", "objektzustand", "etage",
    "ausstattungsstandard", "vermarktungslinie", "maklerprovisionProzent",
    "grundbuchstand", "energieausweisArt", "energieeffizienzklasse",
    "energietraeger", "heizungsart", "erschliessungszustand",
    "flaechennutzungsart", "bebauungsplan", "beschreibung",
    "ausstattungsbeschreibung", "besonderheiten", "lagebeschreibung",
)

ARRAY_FIELDS = ("objektart", "merkmale")

RICH_TEXT_FIELDS = ("exposeText",)


class WebsiteExportError(Exception):
    """Transport-/HTTP-Fehler beim Website-Export — enthält nie das Secret."""


def _betrag_euro(currency):
    """Currency-Objekt (amountMicros) → ganze/anteilige Euro, oder None.

    Ganze Beträge werden als int gesendet (WordPress zeigt den Rohwert an,
    "450000" statt "450000.0"), Nicht-ganze als float.
    """
    if not isinstance(currency, dict):
        return None
    micros = currency.get("amountMicros")
    if micros is None:
        return None
    euro = micros / 1_000_000
    return int(euro) if euro.is_integer() else euro


def build_payload(immobilie, attachments):
    """Baut den JSON-Payload für den WordPress-Endpoint (mapping.php-Schema).

    Pure Funktion, kein Netzzugriff — ``attachments`` ist bereits die Liste
    aus ``twenty_client.get_attachments`` (nicht heruntergeladen: WordPress
    lädt die signierten URLs selbst per ``media_sideload_image``).

    Jedes Feld aus CURRENCY/NUMBER/BOOLEAN/TEXT/ARRAY/RICH_TEXT_FIELDS wird
    IMMER gesetzt (auch ``null``/leeres Array), damit ein in Twenty geleertes
    Feld die Website-Angabe ebenfalls leert (WordPress-Seite: ``mapping.php``
    löscht die Meta explizit bei ``null`` statt den Altwert stehen zu lassen).
    ``verfuegbarAb`` wird auf den reinen Datumsanteil gekürzt, `highlight`
    ist bewusst NICHT Teil des Payloads (website-eigenes Redaktionsfeld).
    """
    fields = {}

    for feld in CURRENCY_FIELDS:
        fields[feld] = _betrag_euro(immobilie.get(feld))

    for feld in NUMBER_FIELDS:
        fields[feld] = immobilie.get(feld)

    for feld in BOOLEAN_FIELDS:
        fields[feld] = bool(immobilie.get(feld)) if immobilie.get(feld) is not None else None

    for feld in TEXT_FIELDS:
        fields[feld] = immobilie.get(feld)

    for feld in ARRAY_FIELDS:
        fields[feld] = immobilie.get(feld) or []

    for feld in RICH_TEXT_FIELDS:
        fields[feld] = immobilie.get(feld)

    verfuegbar_ab = immobilie.get("verfuegbarAb")
    fields["verfuegbarAb"] = verfuegbar_ab[:10] if verfuegbar_ab else None

    images = [
        {
            "twenty_attachment_id": att.get("id"),
            "url": att.get("url"),
            "title": att.get("name") or "",
        }
        for att in attachments
        if att.get("fileCategory") == "IMAGE" and att.get("url")
    ]

    return {
        "objektnummer": "IPR-%s" % immobilie["id"],
        "title": immobilie.get("name") or "",
        "vermarktungsstatus": immobilie.get("vermarktungsstatus") or "",
        "fields": fields,
        "images": images,
    }


def build_delete_payload(objektnummer):
    """Payload für den Entfernen-Endpoint — bewusst NUR die objektnummer.

    Kein immobilie-Dict als Eingabe: DELETE-Aufträge dürfen laut worker.py-Regel
    nie die Immobilie aus Twenty laden, denn sie kann dort bereits gelöscht
    sein. objektnummer kommt daher immer aus order["objektnummer"].
    """
    return {"objektnummer": objektnummer}


def _sign(body, secret):
    """HMAC-SHA256 über die rohen Body-Bytes, hex — muss exakt zu WordPress'
    ``hash_hmac('sha256', $request->get_body(), IPRWE_TWENTY_WEBHOOK_KEY)``
    passen (gleicher Algorithmus, gleiche Bytes, gleiche Ausgabeform)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post(payload, timeout=30, url=None):
    """Signiert und sendet ``payload`` an den WordPress-Endpoint.

    Body wird genau einmal serialisiert und exakt dieselben Bytes signiert
    UND gesendet (keine zweite json.dumps-Stelle, die abweichen könnte).
    ``url`` überschreibt den Upsert-Default — s. ``delete()`` für den
    Entfernen-Endpoint.
    """
    if not AKTIV:
        raise RuntimeError("Website-Kanal ist nicht aktiviert (website.AKTIV=False)")

    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise RuntimeError(
            "Umgebungsvariable %s ist nicht gesetzt (Website-Webhook-Secret)" % SECRET_ENV
        )

    url = url or os.environ.get(URL_ENV) or DEFAULT_URL
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = _sign(body, secret)

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            SIGNATURE_HEADER: signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise WebsiteExportError("HTTP %s beim Website-Export: %s" % (exc.code, detail)) from None
    except urllib.error.URLError as exc:
        raise WebsiteExportError("Netzwerkfehler beim Website-Export: %s" % exc.reason) from None

    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def delete(objektnummer, timeout=30):
    """Entfernt (Soft-Remove) die Website-Veröffentlichung für ``objektnummer``.

    Eigener Endpoint statt post() mit Upsert-URL — s. build_delete_payload/
    DEFAULT_DELETE_URL. Nutzt dieselbe Signatur-/Fehlerbehandlung wie post().
    """
    delete_url = os.environ.get(DELETE_URL_ENV) or DEFAULT_DELETE_URL
    return post(build_delete_payload(objektnummer), timeout=timeout, url=delete_url)
