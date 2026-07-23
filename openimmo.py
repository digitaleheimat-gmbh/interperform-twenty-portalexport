"""OpenImmo-1.2.7-Generator für den Portal-Export (TASK-011).

Erzeugt UPSERT- und DELETE-XMLs nach den am 14.07.2026 live gegen
meinestadt.de validierten Referenzvorlagen (templates/openimmo-new.xml,
templates/openimmo-delete.xml) sowie das Transfer-ZIP.

Sicherheitsregel: umfang="TEIL" ist hart verdrahtet — VOLL würde beim
Portal den kompletten Fremdbestand des Anbieters löschen.

Encoding-Entscheidung ISO-8859-1: Zeichen ohne Latin-1-Entsprechung
(€, Gedankenstriche) werden VOR der Serialisierung deterministisch
ersetzt (siehe ISO_8859_1_ERSATZ), damit Portal-Parser mit striktem
Latin-1-Handling keine numerischen Zeichenreferenzen sehen. Alle
übrigen nicht darstellbaren Zeichen fallen auf XML-Charrefs zurück
(ElementTree-Standardverhalten) — das XML bleibt dadurch immer valide.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional, Union

OPENIMMO_VERSION = "1.2.7"

# Sicherheitsregel (ADR-003): niemals "VOLL" — würde Fremdbestand löschen.
UEBERTRAGUNG_UMFANG = "TEIL"


@dataclass
class PortalConfig:
    """Portal-/Anbieter-Konfiguration; alternativ als dict übergebbar."""

    anbieternr: str
    firma: str
    openimmo_anid: Optional[str] = None  # Default: anbieternr
    techn_email: str = "ar@digitaleheimat.de"
    sendersoftware: str = "digitaleheimat Twenty Export"
    senderversion: str = "0.1"
    kennung_ursprung: str = "twenty-export"
    encoding: str = "utf-8"  # oder "iso-8859-1"


# Feldmapping Immobilien-Dict → OpenImmo-Pfad (Textelemente).
# Attribut-basierte Felder (nutzungsart, vermarktungsart, wohnungtyp,
# waehrung) werden separat in build_upsert_xml gesetzt, weil sie keine
# reinen Textknoten sind.
FELD_MAPPING = {
    "plz": "geo/plz",
    "ort": "geo/ort",
    "bundesland": "geo/bundesland",
    "kontakt_email": "kontaktperson/email_zentrale",
    "kontakt_name": "kontaktperson/name",
    "kontakt_vorname": "kontaktperson/vorname",
    "kontakt_firma": "kontaktperson/firma",
    "kaufpreis": "preise/kaufpreis",
    "wohnflaeche": "flaechen/wohnflaeche",
    "anzahl_zimmer": "flaechen/anzahl_zimmer",
    "objekttitel": "freitexte/objekttitel",
    "objektbeschreibung": "freitexte/objektbeschreibung",
}

# Deterministische Ersetzungen für ISO-8859-1 (dokumentierte Entscheidung,
# s. Modul-Docstring): € und typografische Striche existieren dort nicht.
ISO_8859_1_ERSATZ = {
    "€": "EUR",  # €
    "–": "-",    # – Gedankenstrich (en dash)
    "—": "-",    # — em dash
}


def _cfg(config: Union[PortalConfig, dict]) -> PortalConfig:
    if isinstance(config, PortalConfig):
        return config
    return PortalConfig(**config)


def _fmt(value) -> str:
    # Floats sollen wie in der Vorlage mit zwei Nachkommastellen erscheinen.
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _bool_attr(value) -> str:
    return "1" if value else "0"


def _uebertragung(root: ET.Element, cfg: PortalConfig, modus: str,
                  timestamp: Optional[datetime]) -> None:
    ts = (timestamp or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    ET.SubElement(root, "uebertragung", {
        "art": "ONLINE",
        "umfang": UEBERTRAGUNG_UMFANG,  # hart verdrahtet, s. Modulkopf
        "modus": modus,
        "version": OPENIMMO_VERSION,
        "sendersoftware": cfg.sendersoftware,
        "senderversion": cfg.senderversion,
        "techn_email": cfg.techn_email,
        "timestamp": ts,
    })


def _anbieter(root: ET.Element, cfg: PortalConfig) -> ET.Element:
    anbieter = ET.SubElement(root, "anbieter")
    ET.SubElement(anbieter, "anbieternr").text = cfg.anbieternr
    ET.SubElement(anbieter, "firma").text = cfg.firma
    anid = cfg.openimmo_anid or cfg.anbieternr
    ET.SubElement(anbieter, "openimmo_anid").text = anid
    return anbieter


def _verwaltung_techn(immobilie_el: ET.Element, cfg: PortalConfig,
                      objektnr_intern: str, objektnr_extern: str,
                      stand_vom: str, delete: bool = False) -> None:
    vt = ET.SubElement(immobilie_el, "verwaltung_techn")
    ET.SubElement(vt, "objektnr_intern").text = objektnr_intern
    ET.SubElement(vt, "objektnr_extern").text = objektnr_extern
    if delete:
        # Löschanweisung steht laut Vorlage direkt nach objektnr_extern.
        ET.SubElement(vt, "aktion", {"aktionart": "DELETE"})
    obid = f"{cfg.anbieternr}-{objektnr_extern}"
    ET.SubElement(vt, "openimmo_obid").text = obid
    ET.SubElement(vt, "kennung_ursprung").text = cfg.kennung_ursprung
    ET.SubElement(vt, "stand_vom").text = stand_vom


def _append_anhaenge(immobilie_el: ET.Element,
                     attachments: Optional[list] = None) -> None:
    """Hook für den <anhaenge>-Block — wird in TASK-013 ausgebaut.

    Bewusst noch No-Op: die Bild-Paketierung (Dateireferenzen im ZIP,
    anhangtitel, format, check-Hashes) ist erst in TASK-013 spezifiziert.
    """
    return None


def _sanitize_for_encoding(root: ET.Element, encoding: str) -> None:
    # Nur für Latin-1 nötig; utf-8 kann alle Zeichen nativ darstellen.
    if encoding.lower().replace("_", "-") not in ("iso-8859-1", "latin-1", "latin1"):
        return
    for el in root.iter():
        if el.text:
            for such, ersatz in ISO_8859_1_ERSATZ.items():
                el.text = el.text.replace(such, ersatz)
        for key, val in el.attrib.items():
            for such, ersatz in ISO_8859_1_ERSATZ.items():
                el.attrib[key] = el.attrib[key].replace(such, ersatz)
                val = el.attrib[key]


def _serialize(root: ET.Element, encoding: str) -> bytes:
    _sanitize_for_encoding(root, encoding)
    ET.indent(root, space="  ")
    # ET schreibt die Deklaration mit exakt diesem Encoding-Namen —
    # Deklaration und Byte-Encoding stimmen damit garantiert überein.
    return ET.tostring(root, encoding=encoding, xml_declaration=True)


def build_upsert_xml(immobilie: dict, order: dict,
                     config: Union[PortalConfig, dict],
                     timestamp: Optional[datetime] = None,
                     modus: str = "NEW") -> bytes:
    """Erzeugt das UPSERT-XML nach templates/openimmo-new.xml.

    objektnr_extern kommt IMMER aus order["objektnummer"] — nie aus der
    Immobilie, damit die Portal-Identität allein vom Auftrag gesteuert wird.
    """
    cfg = _cfg(config)
    objektnummer = order["objektnummer"]

    root = ET.Element("openimmo")
    _uebertragung(root, cfg, modus, timestamp)
    anbieter = _anbieter(root, cfg)
    immo = ET.SubElement(anbieter, "immobilie")

    kategorie = ET.SubElement(immo, "objektkategorie")
    ET.SubElement(kategorie, "nutzungsart", {
        "WOHNEN": _bool_attr(immobilie.get("nutzungsart_wohnen", True)),
        "GEWERBE": _bool_attr(immobilie.get("nutzungsart_gewerbe", False)),
    })
    ET.SubElement(kategorie, "vermarktungsart", {
        "KAUF": _bool_attr(immobilie.get("vermarktungsart_kauf", True)),
        "MIETE_PACHT": _bool_attr(immobilie.get("vermarktungsart_miete_pacht", False)),
    })
    objektart = ET.SubElement(kategorie, "objektart")
    ET.SubElement(objektart, "wohnung", {
        "wohnungtyp": immobilie.get("wohnungtyp", "ETAGE"),
    })

    # Textfelder über das zentrale Mapping; fehlende Werte lassen den
    # Elementpfad trotzdem entstehen, damit die Struktur zur validierten
    # Vorlage identisch bleibt.
    for feld, pfad in FELD_MAPPING.items():
        parent_name, leaf_name = pfad.split("/")
        parent = immo.find(parent_name)
        if parent is None:
            parent = ET.SubElement(immo, parent_name)
        leaf = ET.SubElement(parent, leaf_name)
        wert = immobilie.get(feld)
        leaf.text = _fmt(wert) if wert is not None else ""

    preise = immo.find("preise")
    ET.SubElement(preise, "waehrung", {
        "iso_waehrung": immobilie.get("waehrung", "EUR"),
    })

    stand_vom = immobilie.get("stand_vom") or date.today().isoformat()
    _verwaltung_techn(
        immo, cfg,
        objektnr_intern=immobilie.get("objektnr_intern", objektnummer),
        objektnr_extern=objektnummer,
        stand_vom=stand_vom,
    )

    _append_anhaenge(immo, None)  # TASK-013

    return _serialize(root, cfg.encoding)


def build_delete_xml(order: dict, config: Union[PortalConfig, dict],
                     timestamp: Optional[datetime] = None) -> bytes:
    """Erzeugt das DELETE-XML: nur objektnummer-basierte Löschanweisung.

    Bewusst ohne Objektdaten (geo/objektkategorie aus der Vorlage entfallen,
    Spec FR-004): der Löschauftrag kennt keine Immobiliendaten mehr.
    """
    cfg = _cfg(config)
    objektnummer = order["objektnummer"]

    root = ET.Element("openimmo")
    _uebertragung(root, cfg, "CHANGE", timestamp)
    anbieter = _anbieter(root, cfg)
    immo = ET.SubElement(anbieter, "immobilie")

    stand_vom = order.get("stand_vom") or date.today().isoformat()
    _verwaltung_techn(
        immo, cfg,
        objektnr_intern=order.get("objektnr_intern", objektnummer),
        objektnr_extern=objektnummer,
        stand_vom=stand_vom,
        delete=True,
    )

    return _serialize(root, cfg.encoding)


def build_zip(xml_bytes: bytes, attachments: list,
              config: Union[PortalConfig, dict], objektnummer: str,
              timestamp: Optional[datetime] = None) -> "tuple[str, bytes]":
    """Paketiert XML (+ künftige Anhänge, TASK-013) als Transfer-ZIP.

    Rückgabe: (dateiname, zip_bytes) mit Schema
    <anbieternr>_<objektnr>_<ts>.zip.
    """
    cfg = _cfg(config)
    ts = (timestamp or datetime.now()).strftime("%Y%m%d%H%M%S")
    zip_name = f"{cfg.anbieternr}_{objektnummer}_{ts}.zip"
    xml_name = f"{cfg.anbieternr}_{objektnummer}.xml"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(xml_name, xml_bytes)
        for att_name, att_bytes in attachments or []:
            zf.writestr(att_name, att_bytes)
    return zip_name, buf.getvalue()
