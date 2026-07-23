"""Zweistufige Validierung einer Immobilie vor dem Portal-Export (FR-005/FR-006, ADR-004).

Feldnamen per GraphQL-Introspection aus Twenty CRM v2.11.2 ermittelt (Objekt "Immobilie"):

Kern-Pflichtfelder (blockierend):
- PLZ: KEIN eigenes Feld — wird per Regex ``\\b\\d{5}\\b`` aus dem TEXT-Feld ``adresse`` gelesen
- Titel: ``name`` (String)
- Vermarktungsart: ``vermarktungsart`` (Enum: KAUF | MIETE)
- Preis: ``kaufpreis`` bei KAUF, ``nettokaltmiete`` bei MIETE (Currency-Objekt mit
  ``amountMicros``/``currencyCode``); ohne Vermarktungsart zählt eines von beiden
- Wohnfläche: ``wohnflaeche`` (Float)
- Zimmeranzahl: ``zimmer`` (Float)

GEG-§87-/Provisions-Felder (warnend):
- Energieausweis-Art: ``energieausweisArt`` (Enum: BEDARFSAUSWEIS | VERBRAUCHSAUSWEIS)
- Endenergie-Kennwert (kWh/m²·a): ``energieverbrauchskennwert`` (Float)
- Effizienzklasse: ``energieeffizienzklasse`` (Enum A_PLUS…H)
- Energieträger: ``energietraeger`` (Enum)
- Baujahr: ``baujahr`` (Float)
- Provisionsangabe: ``maklerprovisionProzent`` (String) ODER ``maklerprovision`` (Float)

Umschalter warnend → blockierend: ``GEG_BLOCKIEREND = True`` setzen (eine Zeile),
dann landen alle GEG-/Provisions-Meldungen in ``blockers`` statt ``warnings``.
"""

import re

# Ein-Zeilen-Umschalter: True → GEG-/Provisions-Mängel blockieren den Export.
GEG_BLOCKIEREND = False

_PLZ_RE = re.compile(r"\b\d{5}\b")


def _fehlt(wert) -> bool:
    """None, leere/whitespace-Strings und 0-Werte gelten als fehlend."""
    if wert is None:
        return True
    if isinstance(wert, str):
        return not wert.strip()
    if isinstance(wert, (int, float)):
        return wert == 0
    return False


def _preis_fehlt(immobilie: dict) -> bool:
    """Preis je nach Vermarktungsart: KAUF → kaufpreis, MIETE → nettokaltmiete."""
    art = immobilie.get("vermarktungsart")
    if art == "KAUF":
        kandidaten = ["kaufpreis"]
    elif art == "MIETE":
        kandidaten = ["nettokaltmiete"]
    else:
        # Vermarktungsart fehlt selbst → jeder gesetzte Preis reicht
        kandidaten = ["kaufpreis", "nettokaltmiete"]
    return all(_betrag_fehlt(immobilie.get(feld)) for feld in kandidaten)


def _betrag_fehlt(wert) -> bool:
    """Currency-Objekte von Twenty tragen den Betrag in amountMicros."""
    if isinstance(wert, dict):
        return _fehlt(wert.get("amountMicros"))
    return _fehlt(wert)


def _plz_fehlt(immobilie: dict) -> bool:
    adresse = immobilie.get("adresse")
    if not isinstance(adresse, str):
        return True
    return _PLZ_RE.search(adresse) is None


def _provision_fehlt(immobilie: dict) -> bool:
    return _fehlt(immobilie.get("maklerprovisionProzent")) and _fehlt(
        immobilie.get("maklerprovision")
    )


def validate(immobilie: dict) -> tuple[list[str], list[str]]:
    """Prüft eine Immobilie und liefert (blockers, warnings) als deutsche Meldungen."""
    blockers: list[str] = []

    if _plz_fehlt(immobilie):
        blockers.append("Pflichtfeld PLZ fehlt (keine 5-stellige Postleitzahl in der Adresse gefunden)")
    if _fehlt(immobilie.get("name")):
        blockers.append("Pflichtfeld Titel fehlt")
    if _fehlt(immobilie.get("vermarktungsart")):
        blockers.append("Pflichtfeld Vermarktungsart fehlt (Kauf oder Miete)")
    if _preis_fehlt(immobilie):
        blockers.append("Pflichtfeld Preis fehlt (Kaufpreis bzw. Nettokaltmiete)")
    if _fehlt(immobilie.get("wohnflaeche")):
        blockers.append("Pflichtfeld Wohnfläche fehlt")
    if _fehlt(immobilie.get("zimmer")):
        blockers.append("Pflichtfeld Zimmeranzahl fehlt")

    geg: list[str] = []
    if _fehlt(immobilie.get("energieausweisArt")):
        geg.append("GEG-Angabe Energieausweis-Art fehlt (gesetzlich vorgeschrieben)")
    if _fehlt(immobilie.get("energieverbrauchskennwert")):
        geg.append("GEG-Angabe Endenergie-Kennwert (kWh/m²·a) fehlt (gesetzlich vorgeschrieben)")
    if _fehlt(immobilie.get("energieeffizienzklasse")):
        geg.append("GEG-Angabe Effizienzklasse fehlt (gesetzlich vorgeschrieben)")
    if _fehlt(immobilie.get("energietraeger")):
        geg.append("GEG-Angabe Energieträger fehlt (gesetzlich vorgeschrieben)")
    if _fehlt(immobilie.get("baujahr")):
        geg.append("GEG-Angabe Baujahr fehlt (gesetzlich vorgeschrieben)")
    if _provision_fehlt(immobilie):
        geg.append("Provisionsangabe fehlt")

    if GEG_BLOCKIEREND:
        return blockers + geg, []
    return blockers, geg
