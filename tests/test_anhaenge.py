"""Tests für Attachment-Download + <anhaenge>-Paketierung (TASK-013).

Ohne Netz: get_attachments wird über einen FakeTwenty gestellt, der
Download über die injizierbare download-Funktion von lade_bilder.
Die Bild-Fixtures tragen echte Magic-Bytes (Spike S1: Doppelfilter
fileCategory==IMAGE UND Magic-Bytes ist Pflicht).
"""

import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openimmo import PortalConfig, build_upsert_xml, build_zip  # noqa: E402
from twenty_client import BildFehler, MAX_BILD_BYTES, lade_bilder  # noqa: E402

CONFIG = PortalConfig(
    anbieternr="51266",
    firma="Interperform Real Estate UG",
    techn_email="ar@digitaleheimat.de",
    kennung_ursprung="twenty-export-test",
)

IMMOBILIE = {
    "plz": "10115",
    "ort": "Berlin",
    "objekttitel": "Schöne Wohnung",
    "stand_vom": "2026-07-23",
}

ORDER = {"objektnummer": "DH-TEST-20260723"}

TS = datetime(2026, 7, 23, 12, 0, 0)

# Echte Magic-Bytes: JPEG = FF D8 FF, PNG = 89 50 4E 47 ...
JPEG_1 = b"\xff\xd8\xff\xe0" + b"jpeg-eins"
JPEG_2 = b"\xff\xd8\xff\xe1" + b"jpeg-zwei"
PNG_1 = b"\x89PNG\r\n\x1a\n" + b"png-eins"
PDF_1 = b"%PDF-1.7 fake"


class FakeTwenty:
    """Liefert vorbereitete Attachment-Listen statt GraphQL-Antworten."""

    def __init__(self, attachments):
        self._attachments = attachments

    def get_attachments(self, immobilie_id):
        return self._attachments


def att(name, url, category="IMAGE", created="2026-07-23T10:00:00Z"):
    return {
        "id": "id-" + name,
        "name": name,
        "extension": name.rsplit(".", 1)[-1],
        "url": url,
        "fileCategory": category,
        "createdAt": created,
    }


def make_download(mapping):
    return lambda url: mapping[url]


# --- Happy Path: 3 Bilder → ZIP + <anhaenge> --------------------------------

def _drei_bilder():
    twenty = FakeTwenty([
        att("terrasse süd&west.jpg", "u1", created="2026-07-23T10:00:00Z"),
        att("bad.jpeg", "u2", created="2026-07-23T10:01:00Z"),
        att("grundriss.png", "u3", created="2026-07-23T10:02:00Z"),
    ])
    download = make_download({"u1": JPEG_1, "u2": JPEG_2, "u3": PNG_1})
    return lade_bilder(twenty, "immo-1", download=download)


def test_zip_mit_drei_bildern_und_anhaenge_block():
    bilder, warnungen = _drei_bilder()
    assert warnungen == []
    # fortlaufende, sonderzeichen-sichere Namen; Extension aus Magic-Bytes
    assert [name for name, _ in bilder] == \
        ["bild-01.jpg", "bild-02.jpg", "bild-03.png"]
    # Reihenfolge = createdAt-Reihenfolge (Bytes eindeutig zuordenbar)
    assert bilder[0][1] == JPEG_1
    assert bilder[1][1] == JPEG_2
    assert bilder[2][1] == PNG_1

    xml_bytes = build_upsert_xml(IMMOBILIE, ORDER, CONFIG, timestamp=TS,
                                 attachments=bilder)
    zip_name, zip_bytes = build_zip(xml_bytes, bilder, CONFIG,
                                    ORDER["objektnummer"], timestamp=TS)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert set(zf.namelist()) == {
            "51266_DH-TEST-20260723.xml",
            "bild-01.jpg", "bild-02.jpg", "bild-03.png",
        }
        assert zf.read("bild-01.jpg") == JPEG_1
        assert zf.read("bild-03.png") == PNG_1

    # <anhaenge>-Pfade == ZIP-Bildnamen, in derselben Reihenfolge
    root = ET.fromstring(xml_bytes)
    anhaenge = root.findall("anbieter/immobilie/anhaenge/anhang")
    assert [a.find("daten/pfad").text for a in anhaenge] == \
        ["bild-01.jpg", "bild-02.jpg", "bild-03.png"]
    assert [a.find("anhangtitel").text for a in anhaenge] == \
        ["bild-01.jpg", "bild-02.jpg", "bild-03.png"]
    # erstes = TITELBILD, Rest = BILD; alle EXTERN
    assert [a.get("gruppe") for a in anhaenge] == \
        ["TITELBILD", "BILD", "BILD"]
    assert all(a.get("location") == "EXTERN" for a in anhaenge)


# --- Doppelfilter: PDF mit fileCategory IMAGE --------------------------------

def test_pdf_mit_image_kategorie_wird_bildfehler():
    twenty = FakeTwenty([att("expose.pdf", "u1", category="IMAGE")])
    download = make_download({"u1": PDF_1})
    with pytest.raises(BildFehler) as exc:
        lade_bilder(twenty, "immo-1", download=download)
    assert "Bildformat nicht unterstützt (nur JPEG/PNG)" in str(exc.value)
    assert "expose.pdf" in str(exc.value)


# --- Größenlimit meinestadt ---------------------------------------------------

def test_bild_ueber_10_mb_wird_bildfehler():
    zu_gross = b"\xff\xd8\xff" + b"\x00" * MAX_BILD_BYTES
    twenty = FakeTwenty([att("riesig.jpg", "u1")])
    download = make_download({"u1": zu_gross})
    with pytest.raises(BildFehler) as exc:
        lade_bilder(twenty, "immo-1", download=download)
    assert "10 MB" in str(exc.value)
    assert "riesig.jpg" in str(exc.value)


# --- Nicht-Bilder werden übersprungen ----------------------------------------

def test_video_attachment_wird_uebersprungen():
    twenty = FakeTwenty([
        att("rundgang.mp4", "u1", category="VIDEO",
            created="2026-07-23T09:00:00Z"),
        att("front.jpg", "u2", created="2026-07-23T10:00:00Z"),
    ])
    # Download darf für das Video gar nicht erst aufgerufen werden
    download = make_download({"u2": JPEG_1})
    bilder, warnungen = lade_bilder(twenty, "immo-1", download=download)
    assert [name for name, _ in bilder] == ["bild-01.jpg"]
    assert warnungen == []


# --- Keine Bilder → Warnung, kein Fehler --------------------------------------

def test_keine_bilder_gibt_warnung():
    twenty = FakeTwenty([])
    bilder, warnungen = lade_bilder(twenty, "immo-1",
                                    download=make_download({}))
    assert bilder == []
    assert warnungen == \
        ["Objekt hat keine Bilder — Inserat erscheint ohne Fotos"]


def test_nur_nicht_bilder_gibt_auch_warnung():
    twenty = FakeTwenty([att("doc.pdf", "u1", category="TEXT_DOCUMENT")])
    bilder, warnungen = lade_bilder(twenty, "immo-1",
                                    download=make_download({}))
    assert bilder == []
    assert len(warnungen) == 1


# --- Hook bleibt No-Op ohne Anhänge -------------------------------------------

def test_xml_ohne_attachments_hat_kein_anhaenge_element():
    root = ET.fromstring(build_upsert_xml(IMMOBILIE, ORDER, CONFIG,
                                          timestamp=TS))
    assert root.find("anbieter/immobilie/anhaenge") is None
    root = ET.fromstring(build_upsert_xml(IMMOBILIE, ORDER, CONFIG,
                                          timestamp=TS, attachments=[]))
    assert root.find("anbieter/immobilie/anhaenge") is None
