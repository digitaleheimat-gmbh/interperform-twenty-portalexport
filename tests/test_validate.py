"""Tests für validate.py — Feldnamen entsprechen der Twenty-CRM-Introspection."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validate as validate_mod
from validate import validate


@pytest.fixture
def immobilie():
    """Vollständige Immobilie mit realen Twenty-Feldnamen."""
    return {
        "name": "Helle 3-Zimmer-Wohnung mit Balkon in Pankow",
        "adresse": "Berliner Straße 42, 13189 Berlin",
        "vermarktungsart": "KAUF",
        "kaufpreis": {"amountMicros": 485000000000, "currencyCode": "EUR"},
        "nettokaltmiete": None,
        "wohnflaeche": 82.5,
        "zimmer": 3,
        "energieausweisArt": "VERBRAUCHSAUSWEIS",
        "energieverbrauchskennwert": 92.4,
        "energieeffizienzklasse": "C",
        "energietraeger": "GAS",
        "baujahr": 1962,
        "maklerprovisionProzent": "3,57 % inkl. MwSt.",
        "maklerprovision": None,
    }


def test_vollstaendiges_objekt(immobilie):
    assert validate(immobilie) == ([], [])


# --- Blockierende Kern-Pflichtfelder ---

@pytest.mark.parametrize(
    "feld,wert,erwartet",
    [
        ("adresse", "Berliner Straße 42, Berlin", "PLZ"),  # keine PLZ im Text
        ("adresse", None, "PLZ"),
        ("adresse", "", "PLZ"),
        ("name", None, "Titel"),
        ("name", "   ", "Titel"),
        ("vermarktungsart", None, "Vermarktungsart"),
        ("kaufpreis", None, "Preis"),
        ("kaufpreis", {"amountMicros": 0, "currencyCode": "EUR"}, "Preis"),  # Preis 0 = fehlt
        ("wohnflaeche", None, "Wohnfläche"),
        ("wohnflaeche", 0, "Wohnfläche"),
        ("zimmer", 0, "Zimmeranzahl"),  # Zimmer 0 = fehlt
        ("zimmer", None, "Zimmeranzahl"),
    ],
)
def test_fehlendes_kernfeld_blockiert(immobilie, feld, wert, erwartet):
    immobilie[feld] = wert
    blockers, warnings = validate(immobilie)
    assert len(blockers) == 1
    assert erwartet in blockers[0]
    assert warnings == []


def test_miete_braucht_nettokaltmiete(immobilie):
    immobilie["vermarktungsart"] = "MIETE"
    immobilie["kaufpreis"] = None  # Kaufpreis zählt bei Miete nicht
    blockers, _ = validate(immobilie)
    assert any("Preis" in b for b in blockers)

    immobilie["nettokaltmiete"] = {"amountMicros": 1450000000, "currencyCode": "EUR"}
    assert validate(immobilie) == ([], [])


# --- Warnende GEG-/Provisions-Felder ---

@pytest.mark.parametrize(
    "feld,erwartet",
    [
        ("energieausweisArt", "Energieausweis-Art"),
        ("energieverbrauchskennwert", "Endenergie-Kennwert"),
        ("energieeffizienzklasse", "Effizienzklasse"),
        ("energietraeger", "Energieträger"),
        ("baujahr", "Baujahr"),
    ],
)
def test_fehlende_geg_angabe_warnt(immobilie, feld, erwartet):
    immobilie[feld] = None
    blockers, warnings = validate(immobilie)
    assert blockers == []
    assert len(warnings) == 1
    assert erwartet in warnings[0]
    assert "gesetzlich vorgeschrieben" in warnings[0]


def test_fehlende_provision_warnt(immobilie):
    immobilie["maklerprovisionProzent"] = ""
    immobilie["maklerprovision"] = None
    blockers, warnings = validate(immobilie)
    assert blockers == []
    assert warnings == ["Provisionsangabe fehlt"]


def test_provision_als_float_reicht(immobilie):
    immobilie["maklerprovisionProzent"] = None
    immobilie["maklerprovision"] = 3.57
    assert validate(immobilie) == ([], [])


def test_leeres_objekt_alle_blocker():
    blockers, warnings = validate({})
    assert len(blockers) == 6
    assert len(warnings) == 6


# --- Umschalter warnend → blockierend ---

def test_geg_blockierend_umschalter(immobilie, monkeypatch):
    immobilie["energietraeger"] = None
    monkeypatch.setattr(validate_mod, "GEG_BLOCKIEREND", True)
    blockers, warnings = validate(immobilie)
    assert warnings == []
    assert any("Energieträger" in b for b in blockers)


def test_eingabe_bleibt_unveraendert(immobilie):
    kopie = copy.deepcopy(immobilie)
    validate(immobilie)
    assert immobilie == kopie
