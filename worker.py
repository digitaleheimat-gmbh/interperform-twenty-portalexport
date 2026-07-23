"""Portal-Export-Worker: Poll-Loop mit FIFO und Dedup (plan.md §6, TASK-009).

Zyklus: offene Aufträge laden → Dedup (FR-014, jüngster je Gruppe gewinnt,
ältere werden UEBERHOLT) → Verarbeitung strikt in createdAt-Reihenfolge.
Der eigentliche Export (XML/Upload/Statusrückschreibung) folgt in TASK-014.
"""

import argparse
import logging
import os
import time

import portals  # noqa: F401 — wird ab TASK-014 im process_order-Kern gebraucht
from twenty_client import TwentyClient, TwentyClientError

log = logging.getLogger("worker")

DEFAULT_POLL_INTERVAL = 60


def _dedup_key(order):
    # immobilieId ist stabiler als objektnummer; letztere ist der Fallback,
    # damit auch DELETE-Aufträge ohne Relation dedupliziert werden.
    return (order.get("immobilieId") or order.get("objektnummer"), order.get("portal"))


def plan_zyklus(orders):
    """Pure Dedup-/Sortierlogik: liefert (to_process, to_supersede).

    Je Gruppe (Immobilie×Portal) überlebt nur der jüngste Auftrag —
    ein neuerer Auftrag macht ältere fachlich gegenstandslos (FR-014).
    Die Verarbeitungsreihenfolge bleibt strikt FIFO nach createdAt,
    damit ein DELETE einen älteren UPSERT nie überholen kann.
    """
    sorted_orders = sorted(orders, key=lambda o: (o.get("createdAt") or "", o.get("id") or ""))
    latest_per_group = {}
    for order in sorted_orders:
        latest_per_group[_dedup_key(order)] = order["id"]

    to_process = []
    to_supersede = []
    for order in sorted_orders:
        if latest_per_group[_dedup_key(order)] == order["id"]:
            to_process.append(order)
        else:
            to_supersede.append(order)
    return to_process, to_supersede


def process_order(order, twenty, portals, dry_run):
    """Verarbeitet einen Auftrag; liefert einen Ergebnis-String fürs Log.

    DELETE-Regel (kritisch): ein DELETE lädt NIE die Immobilie und
    durchläuft KEINE Validierung — es zählt allein order["objektnummer"],
    denn die Immobilie kann im CRM bereits gelöscht/verändert sein.
    """
    aktion = order.get("aktion")
    if aktion == "DELETE":
        # TODO(TASK-014): Lösch-XML nur aus objektnummer bauen, Upload,
        # Status UEBERMITTELT zurückschreiben.
        return "uebersprungen (DELETE-Verarbeitung folgt in TASK-014)"

    # TODO(TASK-014): UPSERT-Kern — twenty.get_immobilie(order["immobilieId"]),
    # validate(), XML erzeugen, portals.upload(), Status zurückschreiben.
    return "uebersprungen (UPSERT-Verarbeitung folgt in TASK-014)"


def run_cycle(twenty, dry_run):
    orders = twenty.fetch_open_orders()
    log.info("Zyklus: %d offene Aufträge geladen", len(orders))
    to_process, to_supersede = plan_zyklus(orders)

    for order in to_supersede:
        if dry_run:
            log.info(
                "[dry-run] Auftrag %s (%s/%s, objektnummer=%s) würde auf UEBERHOLT gesetzt",
                order["id"], order.get("portal"), order.get("aktion"), order.get("objektnummer"),
            )
            continue
        try:
            twenty.update_order(order["id"], status="UEBERHOLT")
            log.info("Auftrag %s auf UEBERHOLT gesetzt (jüngerer Auftrag vorhanden)", order["id"])
        except TwentyClientError as exc:
            # Fehler beim Supersede darf den Zyklus nicht stoppen — der
            # Auftrag bleibt AUSSTEHEND und wird im nächsten Lauf erneut geplant.
            log.error("UEBERHOLT-Update für %s fehlgeschlagen: %s", order["id"], exc)

    for order in to_process:
        start = time.monotonic()
        try:
            ergebnis = process_order(order, twenty, portals, dry_run)
        except Exception as exc:  # noqa: BLE001 — ein Auftrag darf den Loop nie crashen
            ergebnis = "fehler: %s" % exc
            log.exception("Auftrag %s fehlgeschlagen", order["id"])
        dauer = time.monotonic() - start
        log.info(
            "order_id=%s portal=%s aktion=%s ergebnis=%r dauer=%.2fs",
            order["id"], order.get("portal"), order.get("aktion"), ergebnis, dauer,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Portal-Export-Worker für Twenty CRM")
    parser.add_argument("--once", action="store_true", help="nur einen Zyklus ausführen")
    parser.add_argument("--dry-run", action="store_true",
                        help="keine Schreibvorgänge in Twenty, nur Log")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("WORKER_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL))
    twenty = TwentyClient()

    while True:
        try:
            run_cycle(twenty, dry_run=args.dry_run)
        except TwentyClientError as exc:
            # Transiente API-/Netzfehler: loggen und nächsten Poll abwarten.
            log.error("Zyklus fehlgeschlagen: %s", exc)
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
