"""Tests für die pure Dedup-/FIFO-Logik plan_zyklus (TASK-009, FR-014)."""

from worker import plan_zyklus


def _order(id, created, portal="MEINESTADT", aktion="UPSERT",
           objektnummer="OBJ-1", immobilie_id=None):
    return {
        "id": id,
        "createdAt": created,
        "portal": portal,
        "aktion": aktion,
        "objektnummer": objektnummer,
        "immobilieId": immobilie_id,
    }


def test_leere_liste():
    assert plan_zyklus([]) == ([], [])


def test_fifo_reihenfolge():
    a = _order("a", "2026-01-01T10:00:00Z", objektnummer="X")
    b = _order("b", "2026-01-01T09:00:00Z", objektnummer="Y")
    c = _order("c", "2026-01-01T11:00:00Z", objektnummer="Z")
    to_process, to_supersede = plan_zyklus([a, b, c])
    assert [o["id"] for o in to_process] == ["b", "a", "c"]
    assert to_supersede == []


def test_dedup_juengster_gewinnt():
    alt = _order("alt", "2026-01-01T09:00:00Z")
    neu = _order("neu", "2026-01-01T10:00:00Z")
    to_process, to_supersede = plan_zyklus([neu, alt])
    assert [o["id"] for o in to_process] == ["neu"]
    assert [o["id"] for o in to_supersede] == ["alt"]


def test_upsert_dann_delete_reihenfolge_bleibt():
    # verschiedene Objekte: DELETE darf einen älteren UPSERT nie überholen
    upsert = _order("u", "2026-01-01T09:00:00Z", objektnummer="A")
    delete = _order("d", "2026-01-01T10:00:00Z", objektnummer="B", aktion="DELETE")
    to_process, _ = plan_zyklus([delete, upsert])
    assert [o["id"] for o in to_process] == ["u", "d"]


def test_delete_ueberholt_aelteren_upsert_gleiches_objekt():
    # gleiches Objekt+Portal: der jüngere DELETE macht den UPSERT gegenstandslos
    upsert = _order("u", "2026-01-01T09:00:00Z")
    delete = _order("d", "2026-01-01T10:00:00Z", aktion="DELETE")
    to_process, to_supersede = plan_zyklus([upsert, delete])
    assert [o["id"] for o in to_process] == ["d"]
    assert [o["id"] for o in to_supersede] == ["u"]


def test_verschiedene_portale_getrennte_gruppen():
    ms = _order("ms", "2026-01-01T09:00:00Z", portal="MEINESTADT")
    is24 = _order("is24", "2026-01-01T10:00:00Z", portal="IMMOSCOUT24")
    to_process, to_supersede = plan_zyklus([ms, is24])
    assert [o["id"] for o in to_process] == ["ms", "is24"]
    assert to_supersede == []


def test_dedup_per_immobilie_id_vor_objektnummer():
    # gleiche immobilieId, abweichende objektnummer → trotzdem eine Gruppe
    alt = _order("alt", "2026-01-01T09:00:00Z", objektnummer="A", immobilie_id="uuid-1")
    neu = _order("neu", "2026-01-01T10:00:00Z", objektnummer="B", immobilie_id="uuid-1")
    to_process, to_supersede = plan_zyklus([alt, neu])
    assert [o["id"] for o in to_process] == ["neu"]
    assert [o["id"] for o in to_supersede] == ["alt"]
