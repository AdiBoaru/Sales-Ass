"""Guard CI — conexiunea DB aparține OPERAȚIEI, nu turului (NX-161 → NX-231).

Trei reguli mecanice peste `src/`, verificate prin AST (nu regex pe linii — mențiunile din
comentarii/docstring-uri nu sunt noduri `Attribute`/`Call`, deci nu produc fals-pozitive):

  **R1 `deps.conn`** — accesul la conexiunea vie lungă din stagii/tool-uri. Contractul e
  `async with deps.db("operație") as conn`. Include și `PipelineDeps(conn=...)` în `src/`
  (testele au voie: puntea de compat le mapează la un provider static).

  **R2 conexiune peste await EXTERN** — un `async with` care ține o conexiune (`tenant_conn`,
  `admin_conn`, `pool.acquire`, `db(...)`, `db_tx(...)`) și conține în corp un await către ceva
  care NU e baza de date: LLM/embed/moderation, HTTP de provider, sleep/backoff, așteptare pe o
  coadă. ĂSTA e bug-ul pe care îl repară NX-231 — restul sunt simptome. Fără regula asta,
  o migrare corectă azi redevine greșită la primul PR care mută un `await` cu două linii mai sus.

  **R3 punct de intrare care primește `conn`** — un STAGIU (`*_stage`) sau un TOOL (`@register`)
  primește `(ctx, deps)`, niciodată o conexiune deschisă de altcineva: altfel proprietatea
  conexiunii urcă înapoi la apelant și tot edificiul se întoarce de unde a plecat. Helperele
  private chemate ÎNĂUNTRUL unui checkout au voie să ia `conn` — ăla e chiar tiparul corect
  („mai multe query-uri atomice = o metodă de service, o tranzacție").

Excepțiile trăiesc în `scripts/conn_allowlist.json`, cu MOTIV. O excepție fără motiv e o regulă
dezactivată în tăcere.

**Limita cunoscută:** R2 e sintactică, nu interprocedurală — vede awaiturile scrise în corpul
`async with`-ului, nu pe cele ascunse într-o funcție chemată de acolo. E un compromis deliberat:
o analiză a grafului de apeluri ar cere o hartă a modulelor și ar produce fals-pozitive pe fiecare
helper de repository. Prinde regresiile care chiar apar (cineva mută un `await` cu două linii mai
sus); pentru restul, testele din `tests/test_conn_per_op.py` verifică COMPORTAMENTUL, cu un
provider fals care pică dacă un apel extern se face cu un checkout deschis.

Rulare:
    python scripts/check_no_raw_conn.py            # hard-fail (CI)
    python scripts/check_no_raw_conn.py --report   # inventar complet, exit 0
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Liniile-sursă raportate pot conține diacritice → forțează UTF-8 (consola Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 — pe Linux/CI stdout e deja UTF-8
    pass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ALLOWLIST = Path(__file__).resolve().parent / "conn_allowlist.json"

# Apeluri care DESCHID o conexiune (context manager). Numele final al callable-ului.
_CONN_OPENERS = {"tenant_conn", "admin_conn", "acquire", "db", "db_tx", "tenant_db"}

# Await-uri EXTERNE bazei de date. Lista e deliberat CURATĂ și mică: fiecare intrare e o clasă de
# latență care nu depinde de Postgres, deci nu are ce căuta între checkout și release.
_EXTERNAL_AWAITS = {
    # model + embeddings + moderation (adaptorul comun, src/agent/llm.py)
    "embed",
    "classify_json",
    "run_tool_loop",
    "complete",
    "chat",
    "moderate",
    "moderation",
    "describe_image",
    "transcribe",
    "generate_summary",
    "extract_profile",
    # HTTP de provider (canale, webhook-uri de operator, media). Verbele generice se verifică
    # ȘI pe receptor (`_HTTP_RECEIVERS`) — `cache.get(conn, ...)` nu e un apel de rețea.
    "send_text",
    "send_rich",
    "send_products",
    "send_carousel_card",
    "send_template",
    "edit_message_media",
    "mark_typing",
    "notify_operator",
    "fetch_media",
    "download",
    # coadă / backoff / așteptare
    "sleep",
    "wait_for",
    "acquire_turn_lock",
    "enqueue_inbound",
}

# Verbe HTTP generice: `post`/`get`/`put`/`request` sunt și nume de metode locale (cache, registry),
# deci contează DOAR pe un receptor care arată a client HTTP.
_HTTP_VERBS = {"post", "get", "put", "patch", "delete", "request", "stream"}
_HTTP_RECEIVERS = {"http", "httpx", "client", "_http", "_client", "session", "_session"}


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    lineno: int
    detail: str

    @property
    def key(self) -> str:
        return f"{self.rule}:{self.path}:{self.detail}"


def _call_name(node: ast.AST) -> str | None:
    """Numele final al unui callable: `x.y.zz(...)` → `zz`, `f(...)` → `f`."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", None)


def _receiver_name(node: ast.Call) -> str | None:
    """`http.post(...)` → `http`; `self._client.get(...)` → `_client`; `f(...)` → None."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    value = func.value
    if isinstance(value, ast.Name):
        return value.id
    return getattr(value, "attr", None)


def _is_external_await(node: ast.Await) -> str | None:
    """Numele apelului dacă awaitul e EXTERN bazei de date, altfel None."""
    call = node.value
    name = _call_name(call)
    if name is None:
        return None
    if name in _EXTERNAL_AWAITS:
        return name
    if name in _HTTP_VERBS and isinstance(call, ast.Call):
        receiver = _receiver_name(call)
        if receiver and receiver.lower() in _HTTP_RECEIVERS:
            return f"{receiver}.{name}"
    return None


def _opens_connection(item: ast.withitem) -> str | None:
    """`async with X() as conn` deschide o conexiune? Întoarce numele deschizătorului sau None."""
    name = _call_name(item.context_expr)
    if name in _CONN_OPENERS:
        return name
    # `async with deps.db(...)` / `async with self._db(...)` prind mai sus prin `attr`; aici
    # rămâne cazul în care variabila legată se numește `conn` (un provider redenumit).
    var = item.optional_vars
    if isinstance(var, ast.Name) and var.id == "conn" and isinstance(item.context_expr, ast.Call):
        return name or "conn-cm"
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.findings: list[Finding] = []

    # --- R1 ------------------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802 — API ast
        if node.attr == "conn" and isinstance(node.value, ast.Name) and node.value.id == "deps":
            self.findings.append(Finding("R1", self.rel, node.lineno, "deps.conn"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — API ast
        if _call_name(node) == "PipelineDeps" and any(kw.arg == "conn" for kw in node.keywords):
            self.findings.append(Finding("R1", self.rel, node.lineno, "PipelineDeps(conn=...)"))
        self.generic_visit(node)

    # --- R2 ------------------------------------------------------------
    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802 — API ast
        opener = next((o for i in node.items if (o := _opens_connection(i))), None)
        if opener is not None:
            for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if not isinstance(inner, ast.Await):
                    continue
                name = _is_external_await(inner)
                if name is not None:
                    self.findings.append(
                        Finding(
                            "R2",
                            self.rel,
                            inner.lineno,
                            f"await {name}() în interiorul lui `{opener}`",
                        )
                    )
        self.generic_visit(node)


def _is_entry_point(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """Stagiu (`*_stage`) sau tool `@register`-at — contractul amândurora e `(ctx, deps)`."""
    if node.name.endswith("_stage"):
        return True
    return any(_call_name(d) == "register" for d in node.decorator_list)


def _check_r3(tree: ast.AST, rel: str) -> list[Finding]:
    """R3: un punct de intrare (stagiu/tool) nu primește o conexiune deschisă de altcineva."""
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_entry_point(node):
            continue
        args = node.args
        names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        if "conn" in names:
            out.append(Finding("R3", rel, node.lineno, f"{node.name}(conn=...)"))
    return out


def _load_allowlist() -> dict[str, str]:
    if not ALLOWLIST.exists():
        return {}
    data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for entry in data.get("allow", []):
        reason = (entry.get("reason") or "").strip()
        if not reason:
            raise SystemExit(f"conn_allowlist: intrarea {entry!r} nu are `reason` — refuzat.")
        out[f"{entry['rule']}:{entry['path']}:{entry['detail']}"] = reason
    return out


def scan() -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # fișier ne-parsabil → sărit (nu blochează guard-ul)
            continue
        visitor = _Visitor(rel)
        visitor.visit(tree)
        findings.extend(visitor.findings)
        findings.extend(_check_r3(tree, rel))
    return findings


_RULE_TITLES = {
    "R1": "`deps.conn` / `PipelineDeps(conn=...)` în src/",
    "R2": "conexiune ținută peste un await EXTERN (LLM/HTTP/backoff/coadă)",
    "R3": "punct de intrare (stagiu/tool) care primește `conn`",
}


def _print_report(findings: list[Finding], allow: dict[str, str]) -> None:
    print(f"INVENTAR conn — {len(findings)} apariții în src/\n")
    for rule in ("R1", "R2", "R3"):
        rows = [f for f in findings if f.rule == rule]
        print(f"[{rule}] {_RULE_TITLES[rule]}: {len(rows)}")
        for f in rows:
            mark = "allowlist" if f.key in allow else "FINDING"
            print(f"    {mark:9} {f.path}:{f.lineno}  {f.detail}")
            if f.key in allow:
                print(f"              motiv: {allow[f.key]}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="inventar complet, exit 0")
    ap.add_argument("--out", type=Path, default=None, help="scrie inventarul într-un fișier")
    args = ap.parse_args()

    allow = _load_allowlist()
    findings = scan()
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                [
                    {"rule": f.rule, "path": f.path, "line": f.lineno, "detail": f.detail}
                    for f in findings
                ],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"inventar scris în {args.out}")
    if args.report:
        _print_report(findings, allow)
        return 0

    violations = [f for f in findings if f.key not in allow]
    if not violations:
        print(
            f"check_no_raw_conn: OK — 0 violări în src/ "
            f"({len(findings)} apariții, {len(allow)} allowlistate)."
        )
        return 0
    print(f"check_no_raw_conn: {len(violations)} violări (NX-231):\n")
    for f in violations:
        print(f"  [{f.rule}] {f.path}:{f.lineno}: {f.detail}")
    print(
        '\nContractul: `async with deps.db("operație") as conn` — checkout scurt, zero await\n'
        "extern înăuntru. Dacă e o excepție legitimă, adaug-o în scripts/conn_allowlist.json\n"
        "CU MOTIV."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
