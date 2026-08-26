"""Tests für website.py (Payload-Bau, Signatur, Transport-Fehler).

Schema-Wechsel 26.08. (zweiter Durchgang): build_payload ist jetzt fast
reines Durchreichen (Twenty-Feldname == Payload-Key == WordPress-ACF-
Feldname), keine Adress-Zerlegung/Ausstattungs-Aggregation mehr.

Kein Netzzugriff für build_payload/_sign — nur post() macht HTTP und wird per
monkeypatch auf urllib.request.urlopen isoliert getestet."""

import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import website  # noqa: E402


VOLLSTAENDIGE_IMMOBILIE = {
    "id": "immo-1",
    "name": "MFH Luckenwalde",
    "adresse": "Rudolf-Breitscheid-Straße 1, 14943 Luckenwalde, Brandenburg",
    "bezirk": "Luckenwalde",
    "vermarktungsart": "KAUF",
    "vermarktungsstatus": "AKTIV",
    "kaufpreis": {"amountMicros": 250_000_000_000, "currencyCode": "EUR"},
    "nettokaltmiete": None,
    "betriebskosten": None,
    "gesamtmiete": None,
    "pacht": None,
    "bewertungVon": None,
    "bewertungBis": None,
    "tatsaechlicherPreis": None,
    "wohnflaeche": 320.0,
    "grundstuecksflaeche": 800.0,
    "zimmer": 12,
    "schlafzimmer": 6,
    "badezimmer": 3,
    "stellplaetze": 3,
    "anzahlWohneinheiten": 6,
    "objektzustand": "SANIERT",
    "heizungsart": "Gas-Zentralheizung",
    "energietraeger": "GAS",
    "energieausweis": True,
    "energieausweisArt": "BEDARFSAUSWEIS",
    "energieeffizienzklasse": "C",
    "energieverbrauchskennwert": 85.0,
    "treibhausgasemission": None,
    "objektart": ["MFH", "GRUNDSTUECK"],
    "merkmale": ["DENKMALSCHUTZ"],
    "ausstattungsstandard": "GEHOBEN",
    "etage": None,
    "erschliessungszustand": None,
    "flaechennutzungsart": None,
    "bebauungsplan": None,
    "vermarktungslinie": "PREMIUM",
    "verfuegbarAb": "2026-09-01T00:00:00.000Z",
    "baujahr": 1920,
    "maklerprovisionProzent": "3,57% inkl. MwSt.",
    "maklerprovision": None,
    "hausgeld": None,
    "erbpacht": False,
    "grundbuchstand": None,
    "beschreibung": "Historisches MFH",
    "lagebeschreibung": "Zentrale Lage",
    "ausstattungsbeschreibung": "Parkett, Einbauküche",
    "besonderheiten": "Denkmalschutz-Fassade",
    "exposeText": "<p>Historisches Mehrfamilienhaus.</p>",
    "kellerraumVorhanden": True,
    "aufzugVorhanden": False,
    "denkmalschutz": None,
    "barrierefrei": False,
    "gaesteWcVorhanden": False,
    "einbaukuecheVorhanden": None,
    "terrasseVorhanden": True,
    "gartenVorhanden": None,
    "klimaanlageVorhanden": False,
    "kaminVorhanden": False,
    "poolVorhanden": False,
    "einliegerwohnungVorhanden": True,
    "seniorenaufzugVorhanden": None,
    "tiefgaragenstellplatzVorhanden": None,
    "loggiaVorhanden": None,
    "vermietet": None,
}


# --- build_payload: Grundstruktur -----------------------------------------

def test_payload_grundstruktur():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["objektnummer"] == "IPR-immo-1"
    assert p["title"] == "MFH Luckenwalde"
    assert p["vermarktungsstatus"] == "AKTIV"


# --- Currency-Felder --------------------------------------------------------

def test_payload_kaufpreis_als_int_ohne_nachkommastellen():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["kaufpreis"] == 250_000
    assert isinstance(p["fields"]["kaufpreis"], int)
    assert p["fields"]["nettokaltmiete"] is None


def test_payload_kaufpreis_mit_nachkommastellen_bleibt_float():
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE,
                      kaufpreis={"amountMicros": 250_500_000, "currencyCode": "EUR"})
    p = website.build_payload(immobilie, [])
    assert p["fields"]["kaufpreis"] == 250.5


def test_payload_alle_currency_felder_im_payload():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    for feld in website.CURRENCY_FIELDS:
        assert feld in p["fields"]


# --- Number/Text/Array-Felder — reines Durchreichen ------------------------

def test_payload_number_felder_werden_durchgereicht():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["wohnflaeche"] == 320.0
    assert p["fields"]["zimmer"] == 12
    assert p["fields"]["grundstuecksflaeche"] == 800.0


def test_payload_text_felder_werden_durchgereicht():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["objektzustand"] == "SANIERT"  # Twenty-Rohwert, keine Übersetzung
    assert p["fields"]["adresse"] == VOLLSTAENDIGE_IMMOBILIE["adresse"]
    assert p["fields"]["bezirk"] == "Luckenwalde"


def test_payload_array_felder_werden_durchgereicht():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["objektart"] == ["MFH", "GRUNDSTUECK"]
    assert p["fields"]["merkmale"] == ["DENKMALSCHUTZ"]


def test_payload_array_feld_fehlt_in_twenty_wird_leere_liste():
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE, objektart=None)
    p = website.build_payload(immobilie, [])
    assert p["fields"]["objektart"] == []


def test_payload_rich_text_feld_wird_durchgereicht():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["exposeText"] == "<p>Historisches Mehrfamilienhaus.</p>"


# --- Boolean-Felder ----------------------------------------------------------

def test_payload_boolean_felder_true_und_false():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["kellerraumVorhanden"] is True
    assert p["fields"]["aufzugVorhanden"] is False


def test_payload_boolean_feld_none_bleibt_none_nicht_false():
    # Twenty liefert None (nie gesetzt) vs. explizit False — Unterschied bleibt
    # im Payload erhalten, WordPress entscheidet, wie es None behandelt.
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["denkmalschutz"] is None


# --- verfuegbarAb -------------------------------------------------------------

def test_payload_verfuegbar_ab_nur_datum():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["verfuegbarAb"] == "2026-09-01"


def test_payload_verfuegbar_ab_none_bleibt_none():
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE, verfuegbarAb=None)
    p = website.build_payload(immobilie, [])
    assert p["fields"]["verfuegbarAb"] is None


# --- Bilder --------------------------------------------------------------------

def test_payload_bilder_nur_fileCategory_image_mit_url():
    attachments = [
        {"id": "a1", "name": "Wohnzimmer", "fileCategory": "IMAGE", "url": "https://x/1"},
        {"id": "a2", "name": "Grundriss", "fileCategory": "OTHER", "url": "https://x/2"},
        {"id": "a3", "name": "Ohne URL", "fileCategory": "IMAGE", "url": None},
    ]
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, attachments)
    assert p["images"] == [{"twenty_attachment_id": "a1", "url": "https://x/1", "title": "Wohnzimmer"}]


# --- Signatur --------------------------------------------------------------------

def test_sign_matcht_hmac_sha256_hex():
    body = b'{"a":1}'
    secret = "geheim"
    erwartet = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert website._sign(body, secret) == erwartet


# --- post() ------------------------------------------------------------------------

def test_post_wirft_wenn_nicht_aktiv(monkeypatch):
    monkeypatch.setattr(website, "AKTIV", False)
    monkeypatch.setenv(website.SECRET_ENV, "geheim")
    with pytest.raises(RuntimeError, match="nicht aktiviert"):
        website.post({"x": 1})


def test_post_wirft_wenn_secret_fehlt(monkeypatch):
    monkeypatch.setattr(website, "AKTIV", True)
    monkeypatch.delenv(website.SECRET_ENV, raising=False)
    with pytest.raises(RuntimeError, match=website.SECRET_ENV):
        website.post({"x": 1})


def test_post_signiert_und_sendet_dieselben_bytes(monkeypatch):
    monkeypatch.setattr(website, "AKTIV", True)
    monkeypatch.setenv(website.SECRET_ENV, "geheim")

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "post_id": 42}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return FakeResponse()

    monkeypatch.setattr(website.urllib.request, "urlopen", fake_urlopen)

    result = website.post({"objektnummer": "IPR-immo-1"})

    body = captured["body"]
    signature_header = captured["headers"][website.SIGNATURE_HEADER.lower()]
    assert signature_header == website._sign(body, "geheim")
    assert json.loads(body) == {"objektnummer": "IPR-immo-1"}
    assert result == {"ok": True, "post_id": 42}


def test_post_http_fehler_wird_zu_website_export_error(monkeypatch):
    monkeypatch.setattr(website, "AKTIV", True)
    monkeypatch.setenv(website.SECRET_ENV, "geheim")

    def fake_urlopen(req, timeout=None):
        raise website.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(website.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(website.WebsiteExportError, match="401"):
        website.post({"x": 1})
