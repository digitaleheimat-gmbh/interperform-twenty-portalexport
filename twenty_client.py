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


class BildFehler(Exception):
    """Fachlicher Bildfehler (Format/Größe) — TASK-014 mappt das auf
    FEHLER ohne Retry, weil ein erneuter Versuch dieselben Daten lädt."""


# Limit laut meinestadt-FAQ: max. 10 MB pro Bild.
MAX_BILD_BYTES = 10 * 1024 * 1024

# Nur JPEG/PNG sind für den Portal-Export zugelassen (Spike S1: im Bestand
# existiert ein PDF mit fileCategory IMAGE — Magic-Bytes sind daher Pflicht,
# die Twenty-Extension ist nicht vertrauenswürdig).
_MAGIC_BYTES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89\x50\x4e\x47", "png"),
)


def _bildformat(data):
    """Dateiendung aus Magic-Bytes — None, wenn kein JPEG/PNG."""
    for magic, ext in _MAGIC_BYTES:
        if data[: len(magic)] == magic:
            return ext
    return None


def download_attachment(url, timeout=60):
    """Lädt eine signierte Attachment-URL herunter.

    Spike S1: der Token steckt bereits als Query-Parameter in der URL —
    ein zusätzlicher Authorization-Header führt zu 403, deshalb wird hier
    bewusst KEIN Auth-Header gesetzt.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise TwentyClientError(
            "HTTP %s beim Attachment-Download" % exc.code
        ) from None
    except urllib.error.URLError as exc:
        raise TwentyClientError(
            "Netzwerkfehler beim Attachment-Download: %s" % exc.reason
        ) from None


def lade_bilder(twenty, immobilie_id, download=download_attachment):
    """Lädt alle Bilder einer Immobilie für die ZIP-Paketierung (FR-004a).

    Rückgabe: (bilder, warnungen) mit bilder = [(dateiname, bytes), ...] in
    createdAt-Reihenfolge (erstes Bild = Titelbild). Dateinamen werden
    fortlaufend vergeben (bild-01.jpg, ...) — Sonderzeichen aus Twenty-Namen
    landen so nie im ZIP; die Extension kommt aus den Magic-Bytes.

    download ist injizierbar, damit Tests ohne Netz laufen.
    """
    bilder = []
    warnungen = []
    for att in twenty.get_attachments(immobilie_id):
        if att.get("fileCategory") != "IMAGE":
            log.info(
                "Attachment %r übersprungen (fileCategory=%s, kein Bild)",
                att.get("name"), att.get("fileCategory"),
            )
            continue
        name = att.get("name") or att.get("id") or "?"
        url = att.get("url")
        if not url:
            # Ohne signierte URL ist das Attachment nicht ladbar — das ist
            # ein Datenproblem, kein transienter Fehler.
            raise BildFehler("Keine Download-URL für Bild: %s" % name)
        data = download(url)
        ext = _bildformat(data)
        if ext is None:
            raise BildFehler(
                "Bildformat nicht unterstützt (nur JPEG/PNG): %s" % name
            )
        if len(data) > MAX_BILD_BYTES:
            raise BildFehler(
                "Bild größer als 10 MB (%.1f MB): %s"
                % (len(data) / (1024 * 1024), name)
            )
        bilder.append(("bild-%02d.%s" % (len(bilder) + 1, ext), data))

    if not bilder:
        warnungen.append(
            "Objekt hat keine Bilder — Inserat erscheint ohne Fotos"
        )
    return bilder, warnungen


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
        """Attachments einer Immobilie in createdAt-Reihenfolge.

        Rückgabe: [{id, name, extension, url, fileCategory, createdAt}, ...].
        Die URL ist signiert und nur 24 h gültig (Spike S1) — deshalb wird
        hier NIE gecacht, jeder Auftrag holt die URLs frisch. attachment.
        fullPath ist leer; die Download-URL steckt ausschließlich im
        FILES-Feld `file` (Array je Attachment).
        """
        query = """
        query GetAttachments($id: UUID!) {
          immobilie(filter: { id: { eq: $id } }) {
            attachments(first: 200) {
              edges { node { id name fileCategory createdAt file } }
            }
          }
        }
        """
        data = self._execute("get_attachments", query, {"id": immobilie_id})
        immobilie = data.get("immobilie")
        if immobilie is None:
            raise TwentyClientError(
                "Immobilie %s nicht gefunden (get_attachments)" % immobilie_id
            )
        result = []
        for edge in immobilie["attachments"]["edges"]:
            node = edge["node"]
            files = node.get("file") or []
            entry = files[0] if files else {}
            result.append({
                "id": node.get("id"),
                "name": node.get("name"),
                "extension": entry.get("extension"),
                "url": entry.get("url"),
                "fileCategory": node.get("fileCategory"),
                "createdAt": node.get("createdAt"),
            })
        # createdAt = Anhang-Reihenfolge in Twenty; erstes Bild = Titelbild.
        result.sort(key=lambda a: (a.get("createdAt") or "", a.get("id") or ""))
        return result
