"""GraphQL-Client für Twenty CRM (plan.md §4, TASK-009).

Bewusst nur Stdlib (urllib) — der Worker läuft ohne Drittabhängigkeiten
auf dem VPS. Der API-Token kommt ausschließlich aus der Umgebung und
taucht in keiner Fehlermeldung auf.
"""

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("twenty_client")

DEFAULT_BASE_URL = "https://twenty-vlsc.srv1747052.hstgr.cloud"

# Feldliste laut validate.py-Docstring (Introspection Twenty v2.11.2) —
# alles, was Validierung (FR-005/FR-006) und XML-Erzeugung brauchen.
IMMOBILIE_FIELDS = """
    id
    name
    objektnummer
    adresse
    vermarktungsart
    kaufpreis { amountMicros currencyCode }
    nettokaltmiete { amountMicros currencyCode }
    wohnflaeche
    zimmer
    energieausweisArt
    energieverbrauchskennwert
    energieeffizienzklasse
    energietraeger
    baujahr
    maklerprovisionProzent
    maklerprovision
"""

ORDER_FIELDS = """
    id
    name
    portal
    aktion
    status
    objektnummer
    letzterExport
    fehlermeldung
    warnhinweis
    versuchszaehler
    immobilieId
    createdAt
"""


class TwentyClientError(Exception):
    """GraphQL- oder Transportfehler — enthält nie den Token."""


class TwentyClient:
    def __init__(self, base_url=None, token=None, timeout=30):
        self.base_url = (base_url or os.environ.get("TWENTY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._token = token or os.environ.get("TWENTY_API_TOKEN")
        if not self._token:
            raise TwentyClientError("TWENTY_API_TOKEN ist nicht gesetzt")
        self.timeout = timeout

    def _execute(self, query_name, query, variables=None):
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/graphql",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._token,
            },
            method="POST",
        )
        log.debug("GraphQL-Call: %s", query_name)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Body mitlesen, aber nur die GraphQL-Fehlermeldung weitergeben —
            # Header (und damit der Token) bleiben außen vor.
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                detail = ""
            raise TwentyClientError(
                "HTTP %s bei Query %r: %s" % (exc.code, query_name, detail)
            ) from None
        except urllib.error.URLError as exc:
            raise TwentyClientError(
                "Netzwerkfehler bei Query %r: %s" % (query_name, exc.reason)
            ) from None

        if body.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in body["errors"])
            raise TwentyClientError("GraphQL-Fehler bei Query %r: %s" % (query_name, msgs))
        return body["data"]

    def fetch_open_orders(self):
        """Alle AUSSTEHEND-Aufträge, FIFO nach createdAt, über alle Seiten."""
        query = """
        query OpenOrders($after: String) {
          portalExports(
            first: 200
            filter: { status: { eq: "AUSSTEHEND" } }
            orderBy: [{ createdAt: AscNullsLast }]
            after: $after
          ) {
            edges { node { %s } }
            pageInfo { hasNextPage endCursor }
          }
        }
        """ % ORDER_FIELDS
        orders = []
        after = None
        while True:
            data = self._execute("fetch_open_orders", query, {"after": after})
            conn = data["portalExports"]
            orders.extend(edge["node"] for edge in conn["edges"])
            if not conn["pageInfo"]["hasNextPage"]:
                return orders
            after = conn["pageInfo"]["endCursor"]

    def get_immobilie(self, immobilie_id):
        query = """
        query GetImmobilie($id: UUID!) {
          immobilie(filter: { id: { eq: $id } }) { %s }
        }
        """ % IMMOBILIE_FIELDS
        data = self._execute("get_immobilie", query, {"id": immobilie_id})
        return data["immobilie"]

    def update_order(self, order_id, **fields):
        query = """
        mutation UpdateOrder($id: UUID!, $data: PortalExportUpdateInput!) {
          updatePortalExport(id: $id, data: $data) { id status }
        }
        """
        data = self._execute("update_order", query, {"id": order_id, "data": fields})
        return data["updatePortalExport"]

    def get_attachments(self, immobilie_id):
        # TODO(TASK-013): Attachments der Immobilie laden (Bilder/Dokumente
        # für den OpenImmo-Anhang-Block). Signatur steht bereits fest.
        raise NotImplementedError("get_attachments kommt in TASK-013")
