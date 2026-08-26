"""Tests für website.py (Payload-Bau, Adress-Parsing, Signatur, Transport-Fehler).

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
    "vermarktungsart": "KAUF",
    "vermarktungsstatus": "AKTIV",
    "kaufpreis": {"amountMicros": 250_000_000_000, "currencyCode": "EUR"},
    "nettokaltmiete": None,
    "wohnflaeche": 320.0,
    "zimmer": 12,
    "schlafzimmer": 6,
    "badezimmer": 3,
    "objektzustand": "SANIERT",
    "heizungsart": "Gas-Zentralheizung",
    "energietraeger": "GAS",
    "energieausweis": True,
    "energieausweisArt": "BEDARFSAUSWEIS",
    "objektart": ["MFH", "GRUNDSTUECK"],
    "ausstattungsstandard": "GEHOBEN",
    "etage": None,
    "stellplaetze": 3,
    "verfuegbarAb": "2026-09-01T00:00:00.000Z",
    "baujahr": 1920,
    "maklerprovisionProzent": "3,57% inkl. MwSt.",
    "maklerprovision": None,
    "beschreibung": "Historisches MFH",
    "lagebeschreibung": "Zentrale Lage",
    "ausstattungsbeschreibung": "Parkett, Einbauküche",
    "besonderheiten": "Denkmalschutz-Fassade",
    "kellerraumVorhanden": True,
    "aufzugVorhanden": False,
    "denkmalschutz": None,
    "barrierefrei": False,
    "gaesteWcVorhanden": False,
    "einbaukuecheVorhanden": None,
    "terrasseVorhanden": True,
    "gartenVorhanden": None,
}


# --- Adress-Parsing -----------------------------------------------------------

def test_adresse_mit_hausnummer():
    d = website._parse_adresse("Rudolf-Breitscheid-Straße 1, 14943 Luckenwalde, Brandenburg")
    assert d == {
        "street": "Rudolf-Breitscheid-Straße",
        "house_number": "1",
        "zip_code": "14943",
        "city": "Luckenwalde",
    }


def test_adresse_ohne_hausnummer():
    d = website._parse_adresse("Rudolf-Breitscheid-Straße, 14943 Luckenwalde")
    assert d["street"] == "Rudolf-Breitscheid-Straße"
    assert d["house_number"] == ""
    assert d["city"] == "Luckenwalde"


def test_adresse_ohne_plz_bleibt_leer():
    d = website._parse_adresse("Irgendwo ohne Postleitzahl")
    assert d == {"street": "", "house_number": "", "zip_code": "", "city": ""}


def test_adresse_none():
    d = website._parse_adresse(None)
    assert d == {"street": "", "house_number": "", "zip_code": "", "city": ""}


# --- build_payload --------------------------------------------------------------

def test_payload_grundstruktur():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["objektnummer"] == "IPR-immo-1"
    assert p["title"] == "MFH Luckenwalde"
    assert p["vermarktungsstatus"] == "AKTIV"
    assert p["fields"]["zip_code"] == "14943"
    assert p["fields"]["city"] == "Luckenwalde"


def test_payload_preis_als_int_ohne_nachkommastellen():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["price"] == 250_000
    assert isinstance(p["fields"]["price"], int)
    assert p["fields"]["base_rent"] is None


def test_payload_preis_mit_nachkommastellen_bleibt_float():
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE,
                      kaufpreis={"amountMicros": 250_500_000, "currencyCode": "EUR"})
    p = website.build_payload(immobilie, [])
    assert p["fields"]["price"] == 250.5


def test_payload_free_from_nur_datum():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["free_from"] == "2026-09-01"


def test_payload_free_from_none_bleibt_none():
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE, verfuegbarAb=None)
    p = website.build_payload(immobilie, [])
    assert p["fields"]["free_from"] is None


def test_payload_apartment_type_erster_wert_der_liste():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["apartment_type"] == "MFH"


def test_payload_apartment_type_leere_liste_wird_none():
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE, objektart=[])
    p = website.build_payload(immobilie, [])
    assert p["fields"]["apartment_type"] is None


def test_payload_furnishings_nur_true_flags():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert set(p["fields"]["furnishings"]) == {"cellar", "balcony"}


def test_payload_furnishings_leer_wenn_alles_false():
    alle_false = dict(VOLLSTAENDIGE_IMMOBILIE, kellerraumVorhanden=False, terrasseVorhanden=False)
    p = website.build_payload(alle_false, [])
    assert p["fields"]["furnishings"] == []


def test_payload_energieausweis_status_nicht_vorhanden():
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE, energieausweis=False, energieausweisArt=None)
    p = website.build_payload(immobilie, [])
    assert p["fields"]["energy_certificate_availability"] == "NICHT_VORHANDEN"


def test_payload_energieausweis_status_vorhanden_mit_art():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    assert p["fields"]["energy_certificate_availability"] == "BEDARFSAUSWEIS"


def test_payload_fehlendes_feld_wird_als_null_gesendet_nicht_weggelassen():
    # Twenty-Quelle existiert (heizungsart), ist aber leer → Key bleibt im
    # Payload (mit null), damit ein geleertes Feld auf der Website auch
    # geleert wird (kein "alter Wert bleibt einfach stehen").
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE, heizungsart=None)
    p = website.build_payload(immobilie, [])
    assert "heating_type" in p["fields"]
    assert p["fields"]["heating_type"] is None


def test_payload_felder_ohne_twenty_quelle_fehlen_ganz():
    p = website.build_payload(VOLLSTAENDIGE_IMMOBILIE, [])
    for feld in ("region", "bathroom", "flooring_type", "parking_space_types",
                 "price_on_inquiry", "rent_subsidy", "highlight", "number_of_floors"):
        assert feld not in p["fields"]


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
