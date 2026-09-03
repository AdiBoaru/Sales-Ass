"""NX-264 — codul n-are voie să știe ce vinde tenantul.

Principiul 9 din CLAUDE.md spune că vocabularul vine din DB, nu din cod. Un principiu fără poartă
mecanică e o intenție: `concern_map` a trimis cinci săptămâni spre valori inexistente exact pentru
că nimeni nu compara declaratul cu realitatea. Aici e aceeași clasă — nimeni nu compara „codul e
generic" cu codul.

**Ce scanează:** literalii de string din `src/` și `scripts/`, exceptând docstringurile. Nu
identificatorii și nu comentariile, și e o decizie, nu o scăpare:

* scurgerea reală trăiește în valori — `{"crema": ...}`, `re.compile(r"fara parfum")`,
  `["oily", "dry"]`. Acolo intră vocabularul clientului în comportament.
* un identificator care conține un cuvânt de domeniu (`extract_value`, `dry_run`) e coincidență
  lexicală, nu cuplare la vertical. A-l semnala ar umple excepțiile cu zgomot până când nimeni n-ar
  mai citi poarta, iar o poartă ignorată e mai rea decât una absentă.
* comentariile și docstringurile EXPLICĂ domeniul. Util, și nu ajunge în comportament.

**De unde vin termenii:** `tests/domain_terms.json`, derivat de `scripts/build_domain_terms.py` din
pachetele de domeniu ȘI din catalogul real. Lista nu se scrie de mână — ar fi exact greșeala pe care
o previne.

**Trei feluri de a scuti ceva, în ordinea preferinței:**

1. **Pragma pe linie** — `# domain-leak: ok — <motiv>`. Pentru OMONIME: „hidratare eșuată" din
   `state_v2.py` vorbește despre rehidratarea stării, nu despre pielea nimănui. Motivul stă lângă
   linie, unde se citește. E preferată, fiindcă e cea mai îngustă.
2. **Cale în allowlist** — pentru fișiere al căror CONȚINUT e, prin definiție, domeniu: scripturi
   care AUTOREAZĂ catalogul demo, sonde de măsurare ale căror intrări sunt frazele clientului.
   Acceptă glob.
3. **Cric (`domain_leak_baseline.json`)** — pentru scurgeri REALE, existente, pe care cardul de față
   nu le repară. Îngheață perechea (fișier, termen): una nouă pică testul, iar o intrare care nu se
   mai potrivește TREBUIE ștearsă. Datoria poate doar să scadă. Nu ascunde nimic — o listează.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import pathlib
import re
import unicodedata

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TERMS_FILE = ROOT / "tests" / "domain_terms.json"
ALLOWLIST_FILE = ROOT / "tests" / "domain_leak_allowlist.json"
BASELINE_FILE = ROOT / "tests" / "domain_leak_baseline.json"
SCAN_ROOTS = ("src", "scripts")

PRAGMA = re.compile(r"#\s*domain-leak:\s*ok\b")

# Sub lungimea asta niciun termen nu e semnal: „am"/„pm" ca valori de `routine_time` apar în orice
# text. Cei declarați în pachete nu fac excepție — un token de două litere nu devine dovadă pentru
# că l-a scris cineva într-un JSON.
MIN_TERM_LEN = 4


def _norm(text: str) -> str:
    """lower + fără diacritice. Replicat aici, nu importat din `src/domain/normalize`, ca testul să
    nu depindă de codul pe care îl judecă."""
    nfkd = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _load_json(path: pathlib.Path, required: bool = True) -> dict:
    if not path.exists():
        if not required:
            return {}
        pytest.fail(
            f"lipsește {path.relative_to(ROOT)}. Regenerează cu "
            "`python scripts/build_domain_terms.py --business <uuid>`"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _allowlist() -> tuple[set[str], list[str]]:
    """(termeni scutiți, tipare de cale scutite). Fiecare intrare cere `reason` nevid."""
    data = _load_json(ALLOWLIST_FILE, required=False)
    terms, paths = set(), []
    for entry in data.get("terms", []):
        assert str(entry.get("reason", "")).strip(), f"intrare de allowlist fără motiv: {entry}"
        terms.add(_norm(str(entry["term"])))
    for entry in data.get("paths", []):
        assert str(entry.get("reason", "")).strip(), f"intrare de allowlist fără motiv: {entry}"
        paths.append(str(entry["path"]).replace("\\", "/"))
    return terms, paths


def _baseline() -> set[tuple[str, str]]:
    """Perechile (fișier, termen) înghețate ca datorie cunoscută."""
    data = _load_json(BASELINE_FILE, required=False)
    out = set()
    for entry in data.get("known", []):
        assert str(entry.get("reason", "")).strip(), f"intrare de baseline fără motiv: {entry}"
        out.add((str(entry["path"]).replace("\\", "/"), _norm(str(entry["term"]))))
    return out


def _domain_terms() -> set[str]:
    data = _load_json(TERMS_FILE)
    terms = set(data.get("declared", {})) | set(data.get("catalog", {}))
    exempt, _ = _allowlist()
    return {t for t in terms if len(t) >= MIN_TERM_LEN and t not in exempt}


def _string_literals(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Literalii care ajung în comportament, cu INTERVALUL lor de linii. Docstringurile ies.

    Intervalul, nu linia: două f-stringuri alăturate sunt un singur nod, raportat pe linia primului.
    Fără `end_lineno`, o pragmă pusă lângă textul din al doilea n-ar fi găsită niciodată."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    out: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.append((node.lineno, node.end_lineno or node.lineno, node.value))
    return out


def _python_files() -> list[pathlib.Path]:
    _, exempt_paths = _allowlist()
    files: list[pathlib.Path] = []
    for root in SCAN_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            if "__pycache__" in rel or "/archive/" in rel:
                continue
            if any(fnmatch.fnmatch(rel, pattern) for pattern in exempt_paths):
                continue
            files.append(path)
    return files


def scan() -> list[tuple[str, int, str, str]]:
    """(fișier, linie, termen, fragment). Funcție, nu test, ca s-o poată chema și un script."""
    terms = _domain_terms()
    patterns = {t: re.compile(rf"(?<!\w){re.escape(t)}(?!\w)") for t in terms}
    findings: list[tuple[str, int, str, str]] = []
    for path in _python_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        lines = source.splitlines()
        for lineno, end_lineno, literal in _string_literals(tree):
            # Pragma se caută pe TOT intervalul literalului plus linia dinaintea lui: într-un apel
            # scris pe mai multe linii comentariul stă natural deasupra șirului, iar a-l forța la
            # coadă ar sparge limita de 100 de coloane exact pe mesajele lungi.
            window = [lines[i] for i in range(lineno - 2, end_lineno) if 0 <= i < len(lines)]
            if any(PRAGMA.search(line) for line in window):
                continue
            normalized = _norm(literal)
            if not normalized:
                continue
            for term, pattern in patterns.items():
                if pattern.search(normalized):
                    findings.append((rel, lineno, term, literal[:80]))
    return findings


def test_excepțiile_au_motiv_pe_fiecare_intrare() -> None:
    """O excepție fără motiv scris nu e o excepție, e o gaură."""
    _allowlist()
    _baseline()


def test_artefactul_de_termeni_are_provenance() -> None:
    """Lista de termeni trebuie să spună de unde vine și cum se regenerează — altfel, peste trei
    luni, nimeni nu mai știe dacă e derivată sau scrisă de mână, adică fix problema."""
    data = _load_json(TERMS_FILE)
    prov = data.get("_provenance") or {}
    for key in ("business_id", "declared_from", "catalog_from", "regenerate"):
        assert prov.get(key), f"provenance incomplet: lipsește {key}"
    assert data.get("declared"), "niciun termen declarat — artefactul e gol"
    assert data.get("catalog"), "niciun termen din catalog — artefactul e gol"


def test_cricul_nu_are_intrari_moarte() -> None:
    """Datoria poate doar să scadă. O intrare de baseline care nu se mai potrivește înseamnă că
    scurgerea a fost reparată — și atunci trebuie ȘTEARSĂ, ca poarta să nu rămână deschisă degeaba
    pe un fișier deja curat."""
    seen = {(rel, term) for rel, _, term, _ in scan()}
    stale = sorted(_baseline() - seen)
    assert not stale, (
        "intrări de baseline care nu mai corespund niciunei scurgeri (șterge-le din "
        f"tests/domain_leak_baseline.json): {stale}"
    )


def test_codul_nu_contine_vocabular_de_domeniu() -> None:
    """Niciun literal de string NOU din `src/` sau `scripts/` nu conține vocabularul tenantului."""
    known = _baseline()
    findings = [f for f in scan() if (f[0], f[2]) not in known]
    if findings:
        lines = "\n".join(
            f"  {rel}:{lineno}  termen={term!r}  în {frag!r}"
            for rel, lineno, term, frag in findings
        )
        pytest.fail(
            f"{len(findings)} scurgeri de domeniu NOI în cod:\n{lines}\n\n"
            "Vocabularul aparține datelor (pachetul de domeniu al tenantului), nu codului.\n"
            "Dacă apariția e un omonim, pune `# domain-leak: ok — <motiv>` pe linie.\n"
            "Dacă fișierul autorează conținut sau măsoară fraze de client, adaugă calea în "
            "tests/domain_leak_allowlist.json, cu motiv."
        )
