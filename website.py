"""Website-Export-Kanal (``portal: WEBSITE``): JSON-Payload statt OpenImmo-XML,
signierter HTTPS-POST statt FTPS-Upload.

Gegenstück: WordPress-Plugin ``interperform-website-export`` — konkret
``includes/rest-api.php`` (Signatur-Prüfung, Route) und ``includes/mapping.php``
(Payload-Schema), siehe ``BRAIN-IPR/2026-08-26-konzept-website-export.md``
Abschnitt 3/4 für den vollständigen Kontext.

Sicherheitsmodell (analog ``portals.py``): das Secret steht NIE im Code, nur
der Name der Env-Var (``SECRET_ENV``). ``aktiv`` bleibt ``False``, bis Secret,
Endpoint und WordPress-Route produktiv durchgetestet sind (Konzept Schritt 6),
analog zum GLOIM-Vorgehen in ``portals.py``.
"""

import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_URL = "https://immobilientenor.de/wp-json/interperform/v1/immobilie"
URL_ENV = "WEBSITE_EXPORT_URL"
SECRET_ENV = "WEBSITE_WEBHOOK_SECRET"
SIGNATURE_HEADER = "X-Interperform-Signature"

# Erst nach Konzept-Schritt 6 (End-to-End-Test mit [TEST]-Objekt) auf True
# stellen — analog "aktiv": False bei GLOIM in portals.py.
AKTIV = False

_PLZ_RE = re.compile(r"\b\d{5}\b")
# Straße + Hausnummer: Hausnummer ist eine Zahl (ggf. mit Buchstaben-Suffix
# wie "12a") am Ende des Textes vor der PLZ.
_HAUSNR_RE = re.compile(r"^(?P<strasse>.*?)\s+(?P<hausnr>\d+\s*[a-zA-Z]?)$")

# ACF-Checkbox-Werte von "furnishings" (interperform-website-export/includes/
# acf-fields.php, field_6971ca06797dc) — feste Zuordnung zu den Twenty-
# Ausstattungs-Booleans, die "direkt kompatibel" sind (Konzept Abschnitt 4).
_FURNISHING_FLAGS = (
    ("aufzugVorhanden", "lift"),
    ("kellerraumVorhanden", "cellar"),
    ("denkmalschutz", "monument"),
    ("barrierefrei", "barrier_free"),
    ("gaesteWcVorhanden", "guest_toilet"),
    ("einbaukuecheVorhanden", "built_in_kitchen"),
    ("terrasseVorhanden", "balcony"),
    ("gartenVorhanden", "garden"),
)


class WebsiteExportError(Exception):
    """Transport-/HTTP-Fehler beim Website-Export — enthält nie das Secret."""


def _parse_adresse(adresse):
    """Zerlegt Twentys Freitext-``adresse`` in street/house_number/zip_code/city.

    Gleiche Regel wie im OpenImmo-Pfad (worker.immobilie_zu_openimmo_dict):
    ohne PLZ-Treffer wird nichts geraten, alle vier Felder bleiben leer.
    """
    if not isinstance(adresse, str):
        adresse = ""
    plz_match = _PLZ_RE.search(adresse)
    if not plz_match:
        return {"street": "", "house_number": "", "zip_code": "", "city": ""}

    zip_code = plz_match.group(0)
    vorspann = adresse[: plz_match.start()].strip(" ,")
    rest = adresse[plz_match.end():].strip(" ,")
    city = rest.split(",")[0].strip() if rest else ""

    hausnr_match = _HAUSNR_RE.match(vorspann)
    if hausnr_match:
        street = hausnr_match.group("strasse").strip(" ,")
        house_number = hausnr_match.group("hausnr").strip()
    else:
        street = vorspann
        house_number = ""

    return {"street": street, "house_number": house_number, "zip_code": zip_code, "city": city}


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


def _energieausweis_status(immobilie):
    vorhanden = immobilie.get("energieausweis")
    if vorhanden is None:
        return None
    if vorhanden is False:
        return "NICHT_VORHANDEN"
    return immobilie.get("energieausweisArt") or "VORHANDEN"


def _furnishings(immobilie):
    """Immer als Liste (auch leer) — ein zurückgesetztes Flag in Twenty muss
    die Checkbox auf der Website ebenfalls leeren, nicht unverändert lassen."""
    return [token for feld, token in _FURNISHING_FLAGS if immobilie.get(feld) is True]


def build_payload(immobilie, attachments):
    """Baut den JSON-Payload für den WordPress-Endpoint (mapping.php-Schema).

    Pure Funktion, kein Netzzugriff — ``attachments`` ist bereits die Liste
    aus ``twenty_client.get_attachments`` (nicht heruntergeladen: WordPress
    lädt die signierten URLs selbst per ``media_sideload_image``, ein
    zusätzlicher Download/Re-Upload über diesen Worker wäre reine
    Verschwendung von Bandbreite für einen Kanal, der ohnehin nur die URL
    braucht).

    Feld-mit-Twenty-Quelle → Key wird IMMER gesetzt (auch ``null``, damit ein
    in Twenty geleertes Feld die Website-Angabe ebenfalls leert). Nur Felder
    OHNE jede Twenty-Entsprechung (region, bathroom, flooring_type,
    parking_space_types, price_on_inquiry, rent_subsidy, highlight,
    number_of_floors) fehlen im Payload ganz statt mit Platzhalter.

    Wertesatz-Übersetzung (objektzustand/apartment_type-Enums → deutsche
    Labels) ist bewusst noch NICHT Teil dieser Funktion — Twenty-Rohwerte
    (z. B. "GEPFLEGT") gehen 1:1 durch. Eigener Schritt, s. Konzept
    Abschnitt 7 Punkt 5.
    """
    objektart = immobilie.get("objektart") or []
    verfuegbar_ab = immobilie.get("verfuegbarAb")

    fields = {
        "number_of_rooms": immobilie.get("zimmer"),
        "price": _betrag_euro(immobilie.get("kaufpreis")),
        "base_rent": _betrag_euro(immobilie.get("nettokaltmiete")),
        "living_space": immobilie.get("wohnflaeche"),
        "free_from": verfuegbar_ab[:10] if verfuegbar_ab else None,
        "construction_year": immobilie.get("baujahr"),
        "condition": immobilie.get("objektzustand"),
        "heating_type": immobilie.get("heizungsart"),
        "firing_types": immobilie.get("energietraeger"),
        "energy_certificate_availability": _energieausweis_status(immobilie),
        "courtage": immobilie.get("maklerprovisionProzent")
        or immobilie.get("maklerprovision"),
        "description_note": immobilie.get("beschreibung"),
        "location_note": immobilie.get("lagebeschreibung"),
        "furnishing_note": immobilie.get("ausstattungsbeschreibung"),
        "other_note": immobilie.get("besonderheiten"),
        "apartment_type": objektart[0] if objektart else None,
        "floor": immobilie.get("etage"),
        "interior_quality": immobilie.get("ausstattungsstandard"),
        "number_of_bed_rooms": immobilie.get("schlafzimmer"),
        "number_of_bath_rooms": immobilie.get("badezimmer"),
        "number_of_parking_spaces": immobilie.get("stellplaetze"),
        "furnishings": _furnishings(immobilie),
    }
    fields.update(_parse_adresse(immobilie.get("adresse")))

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


def _sign(body, secret):
    """HMAC-SHA256 über die rohen Body-Bytes, hex — muss exakt zu WordPress'
    ``hash_hmac('sha256', $request->get_body(), IPRWE_TWENTY_WEBHOOK_KEY)``
    passen (gleicher Algorithmus, gleiche Bytes, gleiche Ausgabeform)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post(payload, timeout=30):
    """Signiert und sendet ``payload`` an den WordPress-Endpoint.

    Body wird genau einmal serialisiert und exakt dieselben Bytes signiert
    UND gesendet (keine zweite json.dumps-Stelle, die abweichen könnte).
    """
    if not AKTIV:
        raise RuntimeError("Website-Kanal ist nicht aktiviert (website.AKTIV=False)")

    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise RuntimeError(
            "Umgebungsvariable %s ist nicht gesetzt (Website-Webhook-Secret)" % SECRET_ENV
        )

    url = os.environ.get(URL_ENV) or DEFAULT_URL
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
