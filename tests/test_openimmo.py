"""Tests für openimmo.py (TASK-011).

Referenz ist die am 14.07.2026 live gegen meinestadt.de validierte
Vorlage templates/openimmo-new.xml — der Struktur-Test vergleicht
Elementpfad-Mengen zwischen Vorlage und erzeugtem XML.
"""

import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openimmo import (  # noqa: E402
    PortalConfig,
    build_delete_xml,
    build_upsert_xml,
    build_zip,
)

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

CONFIG = PortalConfig(
    anbieternr="51266",
    firma="Interperform Real Estate UG",
    techn_email="ar@digitaleheimat.de",
    kennung_ursprung="twenty-export-test",
)

IMMOBILIE = {
    "nutzungsart_wohnen": True,
    "nutzungsart_gewerbe": False,
    "vermarktungsart_kauf": True,
    "vermarktungsart_miete_pacht": False,
    "wohnungtyp": "ETAGE",
    "plz": "10115",
    "ort": "Berlin",
    "bundesland": "Berlin",
    "kontakt_email": "paul@interperform.de",
    "kontakt_name": "Hörmann",
    "kontakt_vorname": "Paul",
    "kontakt_firma": "Interperform Real Estate UG",
    "kaufpreis": 250000.00,
    "waehrung": "EUR",
    "wohnflaeche": 52.00,
    "anzahl_zimmer": 2,
    "objekttitel": "Schöne Wohnung",
    "objektbeschreibung": "Große Wohnung süß & günstig",
    "objektnr_intern": "DH-TEST-20260714",
    "stand_vom": "2026-07-14",
}

ORDER = {"objektnummer": "DH-TEST-20260714"}

TS = datetime(2026, 7, 14, 15, 30, 0)


def element_paths(root):
    """Alle Element-Pfade als Menge — Werte/Attributwerte bleiben außen vor."""
    paths = set()

    def walk(el, prefix):
        path = f"{prefix}/{el.tag}"
        paths.add(path)
        for child in el:
            walk(child, path)

    walk(root, "")
    return paths


# --- Strukturgleichheit UPSERT ------------------------------------------

def test_upsert_struktur_gleich_vorlage():
    vorlage = ET.parse(TEMPLATES / "openimmo-new.xml").getroot()
    erzeugt = ET.fromstring(build_upsert_xml(IMMOBILIE, ORDER, CONFIG, timestamp=TS))
    assert element_paths(erzeugt) == element_paths(vorlage)


def test_upsert_attributnamen_gleich_vorlage():
    vorlage = ET.parse(TEMPLATES / "openimmo-new.xml").getroot()
    erzeugt = ET.fromstring(build_upsert_xml(IMMOBILIE, ORDER, CONFIG, timestamp=TS))

    def attr_map(root):
        result = {}
        def walk(el, prefix):
            path = f"{prefix}/{el.tag}"
            result.setdefault(path, set()).update(el.attrib.keys())
            for child in el:
                walk(child, path)
        walk(root, "")
        return result

    assert attr_map(erzeugt) == attr_map(vorlage)


def test_upsert_werte_aus_testdaten():
    root = ET.fromstring(build_upsert_xml(IMMOBILIE, ORDER, CONFIG, timestamp=TS))
    immo = root.find("anbieter/immobilie")
    assert root.find("anbieter/anbieternr").text == "51266"
    assert immo.find("geo/plz").text == "10115"
    assert immo.find("preise/kaufpreis").text == "250000.00"
    assert immo.find("flaechen/wohnflaeche").text == "52.00"
    assert immo.find("kontaktperson/name").text == "Hörmann"
    assert immo.find("objektkategorie/nutzungsart").get("WOHNEN") == "1"
    assert immo.find("objektkategorie/vermarktungsart").get("KAUF") == "1"
    assert immo.find("preise/waehrung").get("iso_waehrung") == "EUR"
    ueb = root.find("uebertragung")
    assert ueb.get("modus") == "NEW"
    assert ueb.get("version") == "1.2.7"
    assert ueb.get("timestamp") == "2026-07-14T15:30:00"


def test_upsert_objektnr_extern_immer_aus_auftrag():
    # Immobilie mit abweichender Nummer: der Auftrag muss gewinnen.
    immobilie = dict(IMMOBILIE, objektnr_extern="FALSCH-999")
    order = {"objektnummer": "AUS-AUFTRAG-42"}
    root = ET.fromstring(build_upsert_xml(immobilie, order, CONFIG, timestamp=TS))
    vt = root.find("anbieter/immobilie/verwaltung_techn")
    assert vt.find("objektnr_extern").text == "AUS-AUFTRAG-42"
    assert root.find("anbieter/immobilie/verwaltung_techn/openimmo_obid").text == \
        "51266-AUS-AUFTRAG-42"


def test_upsert_kein_anhaenge_block():
    # TASK-013 baut das aus — bis dahin darf kein <anhaenge> erscheinen.
    root = ET.fromstring(build_upsert_xml(IMMOBILIE, ORDER, CONFIG, timestamp=TS))
    assert root.find("anbieter/immobilie/anhaenge") is None


# --- DELETE ---------------------------------------------------------------

def test_delete_nur_loeschanweisung_keine_objektdaten():
    root = ET.fromstring(build_delete_xml(ORDER, CONFIG, timestamp=TS))
    immo = root.find("anbieter/immobilie")
    # Löschanweisung vorhanden, objektnummer aus dem Auftrag
    aktion = immo.find("verwaltung_techn/aktion")
    assert aktion is not None
    assert aktion.get("aktionart") == "DELETE"
    assert immo.find("verwaltung_techn/objektnr_extern").text == "DH-TEST-20260714"
    # keinerlei Objektdaten
    for verboten in ("objektkategorie", "geo", "preise", "flaechen",
                     "freitexte", "kontaktperson", "anhaenge"):
        assert immo.find(verboten) is None, f"{verboten} darf im DELETE nicht stehen"
    assert root.find("uebertragung").get("modus") == "CHANGE"


def test_delete_immobilie_nur_verwaltung_techn():
    root = ET.fromstring(build_delete_xml(ORDER, CONFIG, timestamp=TS))
    immo = root.find("anbieter/immobilie")
    assert [child.tag for child in immo] == ["verwaltung_techn"]


# --- umfang="TEIL" Sicherheitsregel ---------------------------------------

@pytest.mark.parametrize("xml_bytes", [
    build_upsert_xml(IMMOBILIE, ORDER, CONFIG, timestamp=TS),
    build_delete_xml(ORDER, CONFIG, timestamp=TS),
    build_upsert_xml(IMMOBILIE, ORDER,
                     PortalConfig(anbieternr="51266", firma="X",
                                  encoding="iso-8859-1"), timestamp=TS),
], ids=["upsert-utf8", "delete-utf8", "upsert-latin1"])
def test_umfang_teil_in_jedem_xml(xml_bytes):
    root = ET.fromstring(xml_bytes)
    assert root.find("uebertragung").get("umfang") == "TEIL"


# --- Encoding / Umlaut-Roundtrip -------------------------------------------

UMLAUT_IMMOBILIE = dict(
    IMMOBILIE,
    objekttitel="Größe zählt – ätzend schön für 1 €",
    objektbeschreibung="äöüß und – Gedankenstrich, Preis in €",
)


def test_umlaut_roundtrip_utf8():
    xml_bytes = build_upsert_xml(UMLAUT_IMMOBILIE, ORDER, CONFIG, timestamp=TS)
    assert xml_bytes.decode("utf-8").startswith("<?xml")
    assert b"encoding='utf-8'" in xml_bytes or b'encoding="utf-8"' in xml_bytes
    root = ET.fromstring(xml_bytes)
    ft = root.find("anbieter/immobilie/freitexte")
    # utf-8 kann alles: alle Zeichen inkl. € und – bleiben erhalten
    assert ft.find("objekttitel").text == "Größe zählt – ätzend schön für 1 €"
    assert ft.find("objektbeschreibung").text == \
        "äöüß und – Gedankenstrich, Preis in €"


def test_umlaut_roundtrip_iso_8859_1():
    cfg = PortalConfig(anbieternr="51266", firma="Interperform Real Estate UG",
                       encoding="iso-8859-1")
    xml_bytes = build_upsert_xml(UMLAUT_IMMOBILIE, ORDER, cfg, timestamp=TS)
    # Deklaration und Byte-Encoding stimmen überein
    text = xml_bytes.decode("iso-8859-1")
    assert "iso-8859-1" in text.splitlines()[0]
    root = ET.fromstring(xml_bytes)
    ft = root.find("anbieter/immobilie/freitexte")
    # ä/ö/ü/ß existieren in Latin-1 und überleben unverändert;
    # € und – existieren dort NICHT → dokumentierte Ersetzung EUR bzw. "-"
    assert ft.find("objekttitel").text == "Größe zählt - ätzend schön für 1 EUR"
    assert ft.find("objektbeschreibung").text == \
        "äöüß und - Gedankenstrich, Preis in EUR"
    # kein € mehr in den Bytes (weder direkt noch als Charref)
    assert b"\x80" not in xml_bytes
    assert b"&#8364;" not in xml_bytes


def test_delete_iso_8859_1_parsebar():
    cfg = PortalConfig(anbieternr="51266", firma="Müller & Söhne GmbH",
                       encoding="iso-8859-1")
    xml_bytes = build_delete_xml(ORDER, cfg, timestamp=TS)
    root = ET.fromstring(xml_bytes)
    assert root.find("anbieter/firma").text == "Müller & Söhne GmbH"


# --- ZIP --------------------------------------------------------------------

def test_zip_dateiname_und_inhalt():
    xml_bytes = build_upsert_xml(IMMOBILIE, ORDER, CONFIG, timestamp=TS)
    zip_name, zip_bytes = build_zip(xml_bytes, [], CONFIG,
                                    ORDER["objektnummer"], timestamp=TS)
    assert zip_name == "51266_DH-TEST-20260714_20260714153000.zip"
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert zf.namelist() == ["51266_DH-TEST-20260714.xml"]
        assert zf.read("51266_DH-TEST-20260714.xml") == xml_bytes


def test_zip_mit_attachments():
    # Signatur trägt Anhänge schon (TASK-013 erweitert die Paketierung)
    xml_bytes = build_delete_xml(ORDER, CONFIG, timestamp=TS)
    zip_name, zip_bytes = build_zip(
        xml_bytes, [("bild1.jpg", b"\xff\xd8fake")], CONFIG,
        ORDER["objektnummer"], timestamp=TS)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert set(zf.namelist()) == {"51266_DH-TEST-20260714.xml", "bild1.jpg"}
        assert zf.read("bild1.jpg") == b"\xff\xd8fake"
