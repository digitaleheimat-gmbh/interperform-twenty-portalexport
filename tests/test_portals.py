"""Registry-Tests für portals.py — nur lokale Logik, kein Netzwerkzugriff."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import portals

PFLICHTKEYS = {"host", "port", "user", "password_env", "encoding", "anbieternr", "aktiv"}


def test_registry_struktur_vollstaendig():
    assert portals.PORTALS, "Registry darf nicht leer sein"
    for key, cfg in portals.PORTALS.items():
        fehlend = PFLICHTKEYS - set(cfg)
        assert not fehlend, f"Portal {key}: fehlende Keys {fehlend}"
        assert isinstance(cfg["port"], int)
        assert isinstance(cfg["aktiv"], bool)
        # password_env muss ein Variablenname sein, kein Passwort-Wert
        assert cfg["password_env"].endswith("_FTP_PASSWORD")


def test_meinestadt_aktiv():
    cfg = portals.PORTALS["meinestadt"]
    assert cfg["aktiv"] is True
    assert cfg["host"] == "ftp04.meinestadt.de"
    assert cfg["user"] == "51266"
    assert cfg["anbieternr"] == "51266"
    assert cfg["encoding"] == "utf-8"
    assert cfg["password_env"] == "MEINESTADT_FTP_PASSWORD"


def test_gloim_inaktiv():
    assert portals.PORTALS["gloim"]["aktiv"] is False


def test_upload_inaktives_portal_wirft_fehler():
    with pytest.raises(RuntimeError, match="nicht aktiviert"):
        portals.upload("gloim", "test.txt", b"x")


def test_upload_unbekanntes_portal_wirft_fehler():
    with pytest.raises(KeyError):
        portals.upload("gibtsnicht", "test.txt", b"x")


def test_fehlende_env_var_wirft_klaren_fehler(monkeypatch):
    monkeypatch.delenv("MEINESTADT_FTP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError) as exc:
        portals.upload("meinestadt", "test.txt", b"x")
    assert "MEINESTADT_FTP_PASSWORD" in str(exc.value)


def test_kein_klartext_passwort_im_quelltext():
    passwort = os.environ.get("MEINESTADT_FTP_PASSWORD")
    if not passwort:
        pytest.skip("MEINESTADT_FTP_PASSWORD lokal nicht gesetzt")
    with open(portals.__file__, encoding="utf-8") as f:
        quelltext = f.read()
    assert passwort not in quelltext
