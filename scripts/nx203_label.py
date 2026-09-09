"""NX-203 — unealta de etichetare: un server local, o pagină, tastatura.

De ce o unealtă și nu un fișier editat de mână: cardul cere verificare umană pentru TOT ce intră în
corpus, iar asta înseamnă ~2.500 de judecăți. La 30 de secunde fiecare (citit JSON, căutat produsul,
scris relevanța) sunt 20 de ore și corpusul nu se face niciodată. La 2-3 secunde sunt sub două ore,
iar decizia rămâne integral a omului. Diferența nu e comoditate, e dacă gate-ul există sau nu.

Trei lucruri pe care unealta le impune, fiindcă altfel corpusul iese subtil greșit:

  • **`0` nu e `interzis`.** Un produs nerelevant e zgomot; un produs INTERZIS încalcă o
    constrângere dură a cererii („fără parfum", și are parfum). Metricile le tratează diferit —
    primul scade nDCG, al doilea e o încălcare care se numără separat — deci confuzia lor face
    imposibilă exact măsurătoarea pentru care există corpusul.
  • **Familia se poate SPARGE.** Extragerea grupează după vocabularul catalogului, iar acolo unde
    catalogul e sărac (părul n-are `concerns`, ingredientele rare nu-s în `attributes`) fuzionează
    cereri care nu sunt aceeași cerere. Codul nu poate ști; omul vede în două secunde. Fiecare
    spargere se notează cu motivul, iar lista lor E inventarul de fațete care lipsesc.
  • **Nimic nu se pierde.** Fiecare apăsare se scrie pe disc imediat. Închizi tabul, revii mâine,
    continui de unde ai rămas.

Ordinea candidaților vine din `pools.json` și NU e ordinea motorului (vezi nx203_build_pools).

    python scripts/nx203_build_pools.py --business <uuid> --write   # întâi pool-urile
    python scripts/nx203_label.py                                   # apoi etichetarea
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import threading
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "tests" / "golden" / "nx203"
POOLS = DATA_DIR / "pools.json"
FAMILIES = DATA_DIR / "families_draft.json"
JUDGMENTS = DATA_DIR / "judgments.json"

_LOCK = threading.Lock()

#: Pagina stă în `scripts/assets/nx203_label.html`, nu într-un string aici: HTML-ul în
#: interiorul unui literal Python trece prin linterul de Python, care îi numără liniile ca pe
#: cod, iar rupturile cerute de el fac markupul mai greu de citit fără să-l facă mai corect.
PAGE = (Path(__file__).resolve().parent / "assets" / "nx203_label.html").read_text(encoding="utf-8")


class Store:
    """Starea etichetării, pe disc după fiecare apăsare.

    Nu ține nimic doar în memorie: un proces oprit din greșeală după o oră de etichetat ar însemna
    o oră din timpul cuiva, iar a doua oară nimeni nu mai începe.
    """

    def __init__(self, pools: dict, families: dict) -> None:
        self.business_id = pools["business_id"]
        self.catalog_version = pools["catalog_version"]
        self.products = pools["products"]
        self.pools = pools["pools"]
        self.families = {f["family_id"]: f for f in families["families"]}
        # Ordinea familiilor e cea din pools.json (deja sortată pe câte formulări are familia):
        # familiile cu multe formulări sunt cele mai bine atestate, deci se etichetează întâi. Dacă
        # timpul se termină la jumătate, corpusul rezultat e tot util.
        self.order = list(self.pools.keys())
        self.state = self._load()

    def _load(self) -> dict:
        if JUDGMENTS.exists():
            return json.loads(JUDGMENTS.read_text(encoding="utf-8"))
        return {
            "business_id": self.business_id,
            "catalog_version": self.catalog_version,
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "judgments": {},  # family_id → {product_id: 0|1|2|3|"forbidden"}
            "family_notes": {},  # family_id → {action, note, at}
            "trail": [],  # istoricul apăsărilor, pentru „înapoi"
        }

    def save(self) -> None:
        JUDGMENTS.parent.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tmp = JUDGMENTS.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(JUDGMENTS)

    def _closed(self, fid: str) -> bool:
        """Familiile pe care nu se mai pun judecăți.

        `split` închide, la fel ca `skip_family`: dacă tocmai ai declarat că familia amestecă cereri
        diferite, contractul de adevăr pe care s-ar sprijini judecățile nu există, iar etichetele
        puse mai departe ar arăta ca date bune. Familia se întoarce prin re-extragere, cu nota ta ca
        indiciu despre fațeta care lipsește."""
        note = self.state["family_notes"].get(fid)
        return bool(note and note["action"] in ("skip_family", "next_family", "split"))

    def cursor(self) -> tuple[str, str] | None:
        """Următoarea pereche (familie, produs) neetichetată, în ordinea din pool."""
        for fid in self.order:
            if self._closed(fid):
                continue
            done = self.state["judgments"].get(fid, {})
            for entry in self.pools[fid]:
                if entry["product_id"] not in done:
                    return fid, entry["product_id"]
        return None

    def total(self) -> int:
        return sum(len(self.pools[fid]) for fid in self.order if not self._closed(fid))

    def labeled(self) -> int:
        return sum(len(v) for v in self.state["judgments"].values())

    def view(self) -> dict:
        cur = self.cursor()
        if cur is None:
            return {
                "done": True,
                "labeled": self.labeled(),
                "families_done": len(self.state["judgments"]),
            }
        fid, pid = cur
        pool = self.pools[fid]
        entry = next(e for e in pool if e["product_id"] == pid)
        idx = pool.index(entry)
        product = dict(self.products.get(pid, {"product_id": pid, "name": "(lipsă din catalog)"}))
        product["sources"] = entry["sources"]
        nxt = pool[idx + 1] if idx + 1 < len(pool) else None
        return {
            "done": False,
            "family": self.families.get(fid, {"family_id": fid}),
            "product": product,
            "family_index": self.order.index(fid),
            "family_total": len(self.order),
            "cand_index": idx,
            "cand_total": len(pool),
            "labeled": self.labeled(),
            "total": self.total(),
            "next_image": (self.products.get(nxt["product_id"], {}) or {}).get("image_url")
            if nxt
            else None,
        }

    def judge(self, relevance) -> None:
        cur = self.cursor()
        if cur is None:
            return
        fid, pid = cur
        self.state["judgments"].setdefault(fid, {})[pid] = relevance
        self.state["trail"].append({"kind": "judge", "family_id": fid, "product_id": pid})
        self.save()

    def family_action(self, action: str, note: str) -> None:
        cur = self.cursor()
        if cur is None:
            return
        fid, _pid = cur
        self.state["family_notes"][fid] = {
            "action": action,
            "note": note,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self.state["trail"].append({"kind": "family", "family_id": fid})
        self.save()

    def undo(self) -> None:
        """Un pas înapoi. Fără el, o apăsare greșită e permanentă și evaluatorul încetinește ca să
        nu greșească — exact ce anulează câștigul de viteză."""
        while self.state["trail"]:
            last = self.state["trail"].pop()
            if last["kind"] == "judge":
                self.state["judgments"].get(last["family_id"], {}).pop(last["product_id"], None)
                break
            if self.state["family_notes"].pop(last["family_id"], None) is not None:
                break
        self.save()


def make_handler(store: Store):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # liniște în terminal
            pass

        def _json(self, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/next"):
                with _LOCK:
                    self._json(store.view())
                return
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            with _LOCK:
                if self.path.startswith("/api/judge"):
                    store.judge(payload.get("relevance"))
                elif self.path.startswith("/api/undo"):
                    store.undo()
                elif self.path.startswith("/api/family"):
                    store.family_action(payload.get("action", ""), payload.get("note", ""))
                self._json(store.view())

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not POOLS.exists() or not FAMILIES.exists():
        print("Lipsesc pool-urile. Rulează întâi:")
        print("  python scripts/nx203_extract_families.py --business <uuid> --write")
        print("  python scripts/nx203_build_pools.py --business <uuid> --write")
        return 1

    store = Store(
        json.loads(POOLS.read_text(encoding="utf-8")),
        json.loads(FAMILIES.read_text(encoding="utf-8")),
    )
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Etichetare NX-203 · {store.total()} judecăți de pus · {store.labeled()} deja făcute")
    print(f"Deschide: {url}   (Ctrl+C oprește; progresul e salvat la fiecare apăsare)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), make_handler(store)) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print(f"\nOprit. {store.labeled()} judecăți salvate în {JUDGMENTS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
