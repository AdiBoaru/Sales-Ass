"""Poartă: un adevăr al sistemului are UN producător; al doilea nu intră tăcut.

## De ce

Cele șapte reparații din 2026-09-16 au avut aceeași cauză, nu șapte cauze: undeva exista o
verificare, dar verificarea era legată de o COPIE a adevărului, nu de adevărul însuși. Testul de
state rula pe un nume demo în loc de numele reale; sonda de producție interoga o cale scrisă de
mână în loc de rutele declarate de router; `routine_plan` avea propria interpretare a lui
`concerns` în loc de `resolve_any`; vocabularul avea propriul tokenizator; `product_type` avea
propria pliere de diacritice.

O rescriere e corectă în ziua în care o scrii. Divergează tăcut după, iar testele nu prind nimic
fiindcă verifică o copie contra altei copii.

Poarta acoperă azi DOUĂ adevăruri:

1. **Plierea de diacritice** — cheia de potrivire a întregii căi lexicale.
   Registru: `folding.DISTINCT_FOLDINGS`.
2. **Ce poate PROMITE magazinul (chips)** — un chip nu e o părere, e o promisiune apăsabilă, iar
   apăsarea lui reintră în pipeline ca mesaj nou al clientului.
   Registru: `clarify_menu.CHIP_PRODUCERS`.

În ambele cazuri detecția e pe FORMĂ (AST), nu pe o listă de nume de funcții: o listă ar fi ea
însăși încă o rescriere a adevărului, și ar rata exact copia următoare, care va avea alt nume.
Măsurat: la pliere, cititul cu ochiul găsise 4 locuri și poarta a găsit 19; la chips, 5 și 12.
Precedentul e `tests/test_dropped_objects_guard.py` (NX-289), care citește migrările în loc să țină
minte ce s-a șters.

## Ce NU face

Nu interzice al doilea producător, îl cere DECLARAT cu sursa lui. Un slug ASCII chiar e un adevăr
diferit de o cheie de potrivire, iar a le unifica fiindcă arată la fel ar fi greșeala simetrică —
vezi lecția NX-295: un mecanism care pare general poate fi greșit tocmai prin generalitate.
Registrul de chips nu face chip-urile adevărate; face vizibil cine le produce, fiindcă azi 11 din
12 sunt ancorați ACCIDENTAL (se întâmplă să cheme date reale), ceea ce nu e un invariant.

Simetric, fiecare registru e verificat și invers: o intrare care nu mai corespunde codului e la fel
de periculoasă ca una lipsă, fiindcă amândouă afirmă că sistemul e într-o stare în care nu e.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.catalog.clarify_menu import CHIP_PRODUCERS
from src.catalog.folding import DISTINCT_FOLDINGS

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

#: Fișierul care DEȚINE adevărul. Plierea de aici e definiția, nu o copie.
PRODUCER = "src/catalog/folding.py"

#: Diacriticele românești, în ambele forme Unicode (virgulă U+0219/U+021B și sedilă U+015F/U+0163).
#: Un literal care le conține și e dat lui `str.maketrans` sau lui `.replace` e, prin construcție,
#: o pliere de diacritice — indiferent cum se numește funcția din jur.
RO_DIACRITICS = set("ăâîșțşţĂÂÎȘȚŞŢ")

#: Formele Unicode de descompunere care, urmate de eliminarea semnelor combinatorii, produc o
#: pliere. `NFKC`/`NFC` compun, deci nu pliază nimic și nu intră aici.
DECOMPOSING = {"NFKD", "NFD"}


def _rel(path: Path) -> str:
    """Cale relativă cu `/`, ca poarta să dea același verdict pe Windows și pe Linux."""
    return path.relative_to(REPO).as_posix()


def _str_literal(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_translation_table(node: ast.AST) -> bool:
    """Literal dat lui `str.maketrans`: orice lungime, dacă poartă măcar un diacritic."""
    text = _str_literal(node)
    return bool(text) and any(ch in RO_DIACRITICS for ch in text)


def _is_single_diacritic(node: ast.AST) -> bool:
    """Primul argument al unui `.replace` care chiar pliază: EXACT un diacritic.

    Lungimea e discriminantul, nu conținutul. `"prețul EXACT (lei)".replace(...)` e substituție de
    șablon care se întâmplă să conțină un diacritic — a o trata ca pliere ar umple poarta de fals
    pozitive, iar o poartă zgomotoasă se dezactivează, adică nu mai apără nimic.
    """
    text = _str_literal(node)
    return text is not None and len(text) == 1 and text in RO_DIACRITICS


class _FoldFinder(ast.NodeVisitor):
    """Adună `(funcție, motiv)` pentru fiecare pliere de diacritice găsită într-un modul.

    Trei forme, toate structurale:

    1. `unicodedata.normalize("NFKD", …)` într-o funcție care folosește și `unicodedata.combining`
       — descompune și aruncă semnele, adică pliază.
    2. `str.maketrans(…)` cu un literal care conține diacritice RO.
    3. `.replace(<diacritic>, …)` — varianta scrisă de mână.
    """

    def __init__(self) -> None:
        self.found: dict[str, set[str]] = {}
        self._stack: list[str] = []
        self._decomposes: set[str] = set()
        self._strips: set[str] = set()

    # Numele calificat al funcției curente; la nivel de modul, `<module>`.
    def _here(self) -> str:
        return self._stack[-1] if self._stack else "<module>"

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = _enter  # noqa: N815 — API-ul lui ast
    visit_AsyncFunctionDef = _enter  # noqa: N815

    def _mark(self, reason: str) -> None:
        self.found.setdefault(self._here(), set()).add(reason)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — API-ul lui ast
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

        if name == "normalize" and any(_str_literal(a) in DECOMPOSING for a in node.args):
            self._decomposes.add(self._here())
        elif name == "combining":
            self._strips.add(self._here())
        elif name == "maketrans" and any(_is_translation_table(a) for a in node.args):
            self._mark("str.maketrans cu literal de diacritice")
        elif name == "replace" and node.args and _is_single_diacritic(node.args[0]):
            self._mark("`.replace` pe un diacritic")

        self.generic_visit(node)

    def finish(self) -> dict[str, set[str]]:
        """Descompunerea contează ca pliere doar împreună cu eliminarea semnelor combinatorii.

        Separat, `normalize("NFKD", …)` e normalizare Unicode legitimă (comparare de chei, lățime
        de caractere) și nu atinge potrivirea.
        """
        for fn in self._decomposes & self._strips:
            self.found.setdefault(fn, set()).add("NFKD + strip combining")
        return self.found


def _scan() -> dict[str, set[str]]:
    """Toate plierile din `src/`, cheiate `<cale>::<funcție>`."""
    out: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = _rel(path)
        if rel == PRODUCER:
            continue
        finder = _FoldFinder()
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        for fn, reasons in finder.finish().items():
            out[f"{rel}::{fn}"] = reasons
    return out


def test_plierea_de_diacritice_are_un_singur_producator() -> None:
    """Orice pliere din `src/` e ori delegare către `folding.fold`, ori declarată cu motiv.

    Când pică: NU adăuga o intrare în `DISTINCT_FOLDINGS` ca să treacă poarta. Întreabă întâi dacă
    e alt adevăr (slug, identificator, comparare Unicode) sau același adevăr scris a doua oară.
    Dacă e același, importă `fold`.
    """
    undeclared = {k: sorted(v) for k, v in _scan().items() if k not in DISTINCT_FOLDINGS}
    assert not undeclared, (
        "Pliere de diacritice nedeclarată, în afara unicului producător "
        f"({PRODUCER}):\n"
        + "\n".join(f"  {k} — {', '.join(v)}" for k, v in sorted(undeclared.items()))
        + "\n\nDacă e cheia de potrivire cu `search_tsv`: `from src.catalog.folding import fold`."
        "\nDacă e alt adevăr: declar-o în `folding.DISTINCT_FOLDINGS`, cu motivul."
    )


def test_exceptiile_declarate_corespund_unei_plieri_reale() -> None:
    """O excepție rămasă în urmă e la fel de periculoasă ca una nedeclarată.

    Amândouă afirmă că sistemul e într-o stare în care nu e. Dacă funcția a fost ștearsă sau nu mai
    pliază, intrarea trebuie să plece — altfel data viitoare cineva o citește ca pe un fapt.
    """
    stale = sorted(set(DISTINCT_FOLDINGS) - set(_scan()))
    assert not stale, (
        "Intrări în `DISTINCT_FOLDINGS` care nu mai corespund unei plieri reale în `src/`:\n"
        + "\n".join(f"  {k}" for k in stale)
    )


def test_fiecare_exceptie_are_motiv_nu_doar_o_bifa() -> None:
    """Motivul e partea care lucrează: el e citit de următorul om care vrea să adauge o copie."""
    thin = sorted(k for k, why in DISTINCT_FOLDINGS.items() if len(why.strip()) < 40)
    assert not thin, f"Excepții fără motiv real: {thin}"


# ── Adevărul 2: ce poate PROMITE magazinul (chips) ───────────────────────────────────────────


class _ChipFinder(ast.NodeVisitor):
    """Adună funcțiile care emit un chip către client.

    Două forme, amândouă structurale:

    1. atribuire pe `.suggestions` (`ctx.reply.suggestions = …`);
    2. argument `suggestions=` sau `chips=` într-un apel (construcție de `Reply`, `set_clarify`,
       `set_comparison`).

    Un chip nu e o părere, e o promisiune apăsabilă: apăsarea lui reintră în pipeline ca mesaj NOU
    al clientului. De-aia producătorii se declară, chiar și cei care azi nu pot minți.
    """

    KWARGS = {"suggestions", "chips"}

    def __init__(self) -> None:
        self.found: set[str] = set()
        self._stack: list[str] = []

    def _here(self) -> str:
        return self._stack[-1] if self._stack else "<module>"

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = _enter  # noqa: N815
    visit_AsyncFunctionDef = _enter  # noqa: N815

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "suggestions":
                self.found.add(self._here())
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # `suggestions=` pe DEFINIȚIE (un parametru cu acest nume) nu e emitere; doar pe APEL.
        if any(kw.arg in self.KWARGS for kw in node.keywords if kw.arg):
            self.found.add(self._here())
        self.generic_visit(node)


#: Funcții care POARTĂ chips fără să le producă: transportă mai departe ce a decis altcineva.
#: Declarate aici, nu în `CHIP_PRODUCERS`, fiindcă registrul ăla răspunde la „de unde vine
#: afirmația" — iar răspunsul unui releu ar fi „de mai sus", adică nimic.
_RELAYS = {
    # Primesc `suggestions`/`chips` de la apelant. Apelanții sunt cei declarați în `CHIP_PRODUCERS`;
    # a cere unui releu „sursa afirmației" ar primi răspunsul „de mai sus", adică nimic.
    "src/models.py::set_clarify",
    "src/models.py::set_comparison_reply",
    # Rehidratează chips-urile dintr-un payload de outbox deja scris — transport, nu decizie.
    "src/channels/web/render.py::reply_from_outbox",
    # Copiază `reply.suggestions` într-un semnal intern (NX-239); nu iese nimic nou spre client.
    "src/agent/control_plane.py::gate_early_exit",
}


def _scan_chips() -> set[str]:
    out: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = _rel(path)
        finder = _ChipFinder()
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        for fn in finder.found:
            key = f"{rel}::{fn}"
            if key in _RELAYS or f"{rel}::*" in _RELAYS:
                continue
            out.add(key)
    return out


def test_fiecare_producator_de_chips_e_declarat_cu_sursa_lui() -> None:
    """Un producător nou de chips nu intră tăcut.

    Azi patru din cinci sunt ancorați PRIN CONSTRUCȚIE — cheamă date reale, deci se întâmplă să nu
    poată minți. „Se întâmplă" nu e invariant. Registrul nu face chip-urile adevărate; face vizibil
    cine le produce, ca al șaselea să nu apară fără ca nimeni să se uite la sursa lui.
    """
    undeclared = sorted(_scan_chips() - set(CHIP_PRODUCERS))
    assert not undeclared, (
        "Producător de chips nedeclarat:\n"
        + "\n".join(f"  {k}" for k in undeclared)
        + "\n\nDeclară-l în `clarify_menu.CHIP_PRODUCERS`, cu SURSA din care se poate verifica "
        "că numește ceva servabil. Dacă doar transportă chips decise de altcineva, e un releu: "
        "vezi `_RELAYS` în test."
    )


def test_registrul_de_chips_nu_ramane_in_urma_codului() -> None:
    """Un producător declarat care nu mai emite chips e o afirmație falsă despre sistem."""
    stale = sorted(set(CHIP_PRODUCERS) - _scan_chips())
    assert not stale, "Intrări în `CHIP_PRODUCERS` care nu mai emit chips:\n" + "\n".join(
        f"  {k}" for k in stale
    )
