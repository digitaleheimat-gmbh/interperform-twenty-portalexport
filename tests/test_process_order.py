"""Tests für process_order (TASK-014): UPSERT/DELETE-Kern, Retry/Backoff,
Adapter-Funktion. Kein Netzzugriff — TwentyClient und portals.upload werden
durch einfache Fakes ersetzt (process_order nimmt beide als Parameter)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twenty_client import BildFehler, TwentyClientError  # noqa: E402
from worker import (  # noqa: E402
    immobilie_zu_openimmo_dict,
    process_order,
)


PORTAL_CONFIG = {
    "anbieternr": "51266",
    "firma": "Interperform Real Estate UG",
    "encoding": "utf-8",
}


class FakePortalsModule:
    """Fake für das portals-Modul: PORTALS-Registry + injizierbarer upload()."""

    def __init__(self, upload_fn=None):
        self.PORTALS = {"meinestadt": PORTAL_CONFIG}
        self.calls = []
        self._upload_fn = upload_fn

    def upload(self, portal_key, filename, data):
        self.calls.append((portal_key, filename, data))
        if self._upload_fn:
            self._upload_fn(portal_key, filename, data)


class FakeTwenty:
    """Fake TwentyClient: get_immobilie/get_attachments/update_order ohne Netz."""

    def __init__(self, immobilie=None, attachments=None):
        self._immobilie = immobilie
        self._attachments = attachments or []
        self.get_immobilie_calls = []
        self.update_order_calls = []

    def get_immobilie(self, immobilie_id):
        self.get_immobilie_calls.append(immobilie_id)
        return self._immobilie

    def get_attachments(self, immobilie_id):
        return self._attachments

    def update_order(self, order_id, **fields):
        self.update_order_calls.append((order_id, fields))
        return {"id": order_id, "status": fields.get("status")}


VOLLSTAENDIGE_IMMOBILIE = {
    "id": "immo-1",
    "name": "MFH Luckenwalde",
    "adresse": "Rudolf-Breitscheid-Str. 1, 14943 Luckenwalde",
    "vermarktungsart": "KAUF",
    "kaufpreis": {"amountMicros": 250_000_000_000, "currencyCode": "EUR"},
    "nettokaltmiete": None,
    "wohnflaeche": 320.0,
    "zimmer": 12,
    "energieausweisArt": "BEDARFSAUSWEIS",
    "energieverbrauchskennwert": 120.0,
    "energieeffizienzklasse": None,  # → GEG-Warnung
    "energietraeger": "GAS",
    "baujahr": 1990,
    "maklerprovisionProzent": "3,57",
    "maklerprovision": None,
}


def _upsert_order(**overrides):
    order = {
        "id": "order-1",
        "name": "MEINESTADT: MFH Luckenwalde",
        "portal": "MEINESTADT",
        "aktion": "UPSERT",
        "status": "AUSSTEHEND",
        "objektnummer": "IPR-immo-1",
        "immobilieId": "immo-1",
        "versuchszaehler": 0,
        "warnhinweis": None,
        "createdAt": "2026-07-24T09:00:00.000Z",
        "updatedAt": "2026-07-24T09:00:00.000Z",
    }
    order.update(overrides)
    return order


def _delete_order(**overrides):
    order = {
        "id": "order-del",
        "name": "MEINESTADT: MFH Luckenwalde",
        "portal": "MEINESTADT",
        "aktion": "DELETE",
        "status": "AUSSTEHEND",
        "objektnummer": "IPR-immo-1",
        "immobilieId": "immo-1",
        "versuchszaehler": 0,
        "warnhinweis": None,
        "createdAt": "2026-07-24T09:00:00.000Z",
        "updatedAt": "2026-07-24T09:00:00.000Z",
    }
    order.update(overrides)
    return order


# --- Erfolgsfall UPSERT -----------------------------------------------------

def test_upsert_erfolg_setzt_uebermittelt_und_haengt_warnung_an():
    order = _upsert_order(warnhinweis="RA-1: Bitte Sichtbarkeit im Portal-Backend prüfen.")
    twenty = FakeTwenty(immobilie=VOLLSTAENDIGE_IMMOBILIE)
    fake_portals = FakePortalsModule()

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)

    assert "uebermittelt" in ergebnis
    assert len(fake_portals.calls) == 1
    assert twenty.update_order_calls, "update_order muss aufgerufen worden sein"
    order_id, fields = twenty.update_order_calls[-1]
    assert order_id == "order-1"
    assert fields["status"] == "UEBERMITTELT"
    assert fields["letzterExport"]  # Zeitstempel gesetzt
    assert fields["fehlermeldung"] is None
    # RA-1-Text bleibt erhalten, GEG-Warnung wird angehängt
    assert fields["warnhinweis"].startswith("RA-1: Bitte Sichtbarkeit im Portal-Backend prüfen.")
    assert "Effizienzklasse" in fields["warnhinweis"]


def test_upsert_erfolg_ohne_bestehenden_warnhinweis():
    order = _upsert_order(warnhinweis=None)
    twenty = FakeTwenty(immobilie=VOLLSTAENDIGE_IMMOBILIE)
    fake_portals = FakePortalsModule()

    process_order(order, twenty, fake_portals, dry_run=False)

    _, fields = twenty.update_order_calls[-1]
    assert fields["warnhinweis"]  # GEG-Warnung ohne RA-1-Präfix
    assert not fields["warnhinweis"].startswith("RA-1")


# --- Erfolgsfall DELETE -----------------------------------------------------

def test_delete_erfolg_setzt_entfernt_ohne_immobilien_zugriff():
    order = _delete_order()
    twenty = FakeTwenty(immobilie=None)  # würde crashen, wenn get_immobilie aufgerufen würde
    fake_portals = FakePortalsModule()

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)

    assert "entfernt" in ergebnis
    assert twenty.get_immobilie_calls == [], "DELETE darf get_immobilie NIE aufrufen"
    _, fields = twenty.update_order_calls[-1]
    assert fields["status"] == "ENTFERNT"
    assert fields["letzterExport"]


def test_delete_bei_nicht_mehr_existierender_immobilie_laeuft_durch():
    # Immobilie in Twenty bereits gelöscht — DELETE darf trotzdem funktionieren,
    # weil es ausschließlich order["objektnummer"] verwendet.
    order = _delete_order()

    class ExplodierendeTwenty(FakeTwenty):
        def get_immobilie(self, immobilie_id):
            raise AssertionError("DELETE darf die Immobilie nie laden")

    twenty = ExplodierendeTwenty()
    fake_portals = FakePortalsModule()

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)
    assert "entfernt" in ergebnis


# --- Validierungsfehler ------------------------------------------------------

def test_validierungsfehler_setzt_fehler_ohne_retry():
    unvollstaendig = dict(VOLLSTAENDIGE_IMMOBILIE, wohnflaeche=None, zimmer=None)
    order = _upsert_order()
    twenty = FakeTwenty(immobilie=unvollstaendig)
    fake_portals = FakePortalsModule()

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)

    assert "Validierung" in ergebnis
    assert fake_portals.calls == [], "bei Validierungsfehler darf kein Upload erfolgen"
    _, fields = twenty.update_order_calls[-1]
    assert fields["status"] == "FEHLER"
    assert "Wohnfläche" in fields["fehlermeldung"] or "Zimmer" in fields["fehlermeldung"]
    assert "versuchszaehler" not in fields, "Validierungsfehler darf versuchszaehler nicht anfassen"


# --- BildFehler ---------------------------------------------------------------

def test_bildfehler_setzt_fehler_ohne_retry(monkeypatch):
    order = _upsert_order()
    twenty = FakeTwenty(immobilie=VOLLSTAENDIGE_IMMOBILIE,
                        attachments=[{"id": "a1", "name": "bild.pdf",
                                     "fileCategory": "IMAGE", "url": "https://x/bild"}])
    fake_portals = FakePortalsModule()

    def kaputter_download(url, timeout=60):
        return b"%PDF-1.4"  # kein JPEG/PNG-Magic-Byte → BildFehler

    monkeypatch.setattr("worker.lade_bilder",
                       lambda tw, iid: __import__("twenty_client").lade_bilder(
                           tw, iid, download=kaputter_download))

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)

    assert "Bildfehler" in ergebnis
    assert fake_portals.calls == []
    _, fields = twenty.update_order_calls[-1]
    assert fields["status"] == "FEHLER"
    assert "Bildformat" in fields["fehlermeldung"]
    assert "versuchszaehler" not in fields


# --- Transienter Upload-Fehler / Retry-Backoff ---------------------------------

def _boese_upload(*args, **kwargs):
    raise TwentyClientError("Netzwerkfehler beim Upload (simuliert)")


def test_transienter_upload_fehler_inkrementiert_zaehler_bleibt_ausstehend():
    order = _upsert_order(versuchszaehler=0)
    twenty = FakeTwenty(immobilie=VOLLSTAENDIGE_IMMOBILIE)
    fake_portals = FakePortalsModule(upload_fn=_boese_upload)

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)

    assert "transienter Fehler" in ergebnis
    _, fields = twenty.update_order_calls[-1]
    assert fields.get("versuchszaehler") == 1
    assert "status" not in fields, "Auftrag muss AUSSTEHEND bleiben (kein Status-Feld)"
    assert "Transienter Fehler" in fields["fehlermeldung"]


def test_backoff_skip_bei_frischem_fehlversuch():
    # versuchszaehler=1 und updatedAt gerade eben → Backoff-Stufe (60s) noch aktiv
    frisch = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    order = _upsert_order(versuchszaehler=1, updatedAt=frisch)
    twenty = FakeTwenty(immobilie=VOLLSTAENDIGE_IMMOBILIE)
    fake_portals = FakePortalsModule(upload_fn=_boese_upload)

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)

    assert "Backoff" in ergebnis
    assert fake_portals.calls == []
    assert twenty.get_immobilie_calls == [], "im Backoff darf gar nichts verarbeitet werden"
    assert twenty.update_order_calls == []


def test_backoff_abgelaufen_verarbeitet_erneut():
    # versuchszaehler=1, letzter Fehlversuch 61s her → Backoff-Stufe (60s) vorbei
    alt = (datetime.now(timezone.utc) - timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ")
    order = _upsert_order(versuchszaehler=1, updatedAt=alt)
    twenty = FakeTwenty(immobilie=VOLLSTAENDIGE_IMMOBILIE)
    fake_portals = FakePortalsModule(upload_fn=_boese_upload)

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)

    assert "transienter Fehler" in ergebnis
    _, fields = twenty.update_order_calls[-1]
    assert fields.get("versuchszaehler") == 2


def test_dritter_fehlversuch_setzt_fehler_endgueltig():
    laengst_abgelaufen = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    order = _upsert_order(versuchszaehler=2, updatedAt=laengst_abgelaufen)  # 2 Fehlschläge, Backoff (15 min) längst um
    twenty = FakeTwenty(immobilie=VOLLSTAENDIGE_IMMOBILIE)
    fake_portals = FakePortalsModule(upload_fn=_boese_upload)

    ergebnis = process_order(order, twenty, fake_portals, dry_run=False)

    assert "Retries ausgeschoepft" in ergebnis
    _, fields = twenty.update_order_calls[-1]
    assert fields["status"] == "FEHLER"
    assert fields["versuchszaehler"] == 3


# --- Dry-Run ------------------------------------------------------------------

def test_dry_run_upsert_baut_xml_ohne_upload_und_ohne_statusschreiben():
    order = _upsert_order()
    twenty = FakeTwenty(immobilie=VOLLSTAENDIGE_IMMOBILIE)
    fake_portals = FakePortalsModule()

    ergebnis = process_order(order, twenty, fake_portals, dry_run=True)

    assert "dry-run" in ergebnis
    assert fake_portals.calls == []
    assert twenty.update_order_calls == []
    assert twenty.get_immobilie_calls == ["immo-1"]  # READ-only ist erlaubt


def test_dry_run_delete_baut_xml_ohne_upload():
    order = _delete_order()
    twenty = FakeTwenty()
    fake_portals = FakePortalsModule()

    ergebnis = process_order(order, twenty, fake_portals, dry_run=True)

    assert "dry-run" in ergebnis
    assert fake_portals.calls == []
    assert twenty.update_order_calls == []


# --- Adapter-Funktion immobilie_zu_openimmo_dict --------------------------------

def test_adapter_plz_und_ort_aus_adresse():
    d = immobilie_zu_openimmo_dict(VOLLSTAENDIGE_IMMOBILIE)
    assert d["plz"] == "14943"
    assert d["ort"] == "Luckenwalde"


def test_adapter_ohne_plz_bleibt_leer_statt_raten():
    immobilie = dict(VOLLSTAENDIGE_IMMOBILIE, adresse="Unbekannte Straße ohne PLZ")
    d = immobilie_zu_openimmo_dict(immobilie)
    assert d["plz"] == ""
    assert d["ort"] == ""


def test_adapter_kaufpreis_aus_amountmicros():
    d = immobilie_zu_openimmo_dict(VOLLSTAENDIGE_IMMOBILIE)
    assert d["kaufpreis"] == 250_000.0
    assert d["waehrung"] == "EUR"
    assert d["vermarktungsart_kauf"] is True
    assert d["vermarktungsart_miete_pacht"] is False


def test_adapter_miete_nutzt_nettokaltmiete():
    miete_immobilie = dict(
        VOLLSTAENDIGE_IMMOBILIE,
        vermarktungsart="MIETE",
        kaufpreis=None,
        nettokaltmiete={"amountMicros": 950_000_000, "currencyCode": "EUR"},
    )
    d = immobilie_zu_openimmo_dict(miete_immobilie)
    assert d["kaufpreis"] == 950.0
    assert d["vermarktungsart_kauf"] is False
    assert d["vermarktungsart_miete_pacht"] is True


def test_adapter_kontakt_defaults():
    d = immobilie_zu_openimmo_dict(VOLLSTAENDIGE_IMMOBILIE)
    assert d["kontakt_email"] == "paul@interperform.de"
    assert d["kontakt_name"] == "Hörmann"
    assert d["kontakt_vorname"] == "Paul"
