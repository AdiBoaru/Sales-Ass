"""Memoria agentului unic, persistată în `conversations.state["assistant"]` (NX-396). PUR.

Agentul numește produsele DOAR prin handle-uri (`P1`, `P2`…), stabile pe toată conversația: un
UUID copiat greșit scotea carduri (NX-324), iar un nume scris în locul handle-ului rata fișa și
coșul (NX-395, R1). Starea ține doar perechile handle → id (P8), cele mai recente `MAX_HANDLES`;
numele, prețul și stocul se re-citesc din catalog la fiecare tur, deci sunt mereu proaspete.
Numărătoarea nu se reia niciodată: un handle scos din memorie nu ajunge la alt produs.

Notele sunt ce a SPUS clientul (buget, pentru cine, ce evită), scrise de agent în `answer.notes`
și arătate lui la turul următor. Fără reducer și fără proveniență: modelul le interpretează.

Sugestiile oferite sub ultimul răspuns (NX-407) se țin tot aici: mesajul persistat nu le are, iar
agentul oferea aceeași sugestie trei ture la rând.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Câte handle-uri ține starea (cele mai recente). ~46 de octeți pe pereche: ~1,4 KB la plafon,
#: plus notele (≤ 500), sub bugetul de 6 KB al stării, lângă ecran și coș.
MAX_HANDLES = 30
NOTES_MAX = 500
#: NX-407: sugestiile ultimului răspuns, câte și cât de lungi (≤ ~600 de octeți în stare).
OFFERED_MAX = 5
OFFERED_CHARS = 120
HANDLE_PATTERN = r"^P[1-9][0-9]*$"
_HANDLE_RE = re.compile(HANDLE_PATTERN)


class Refused(Exception):
    """O cerere a agentului pe care codul n-o execută; mesajul îi spune ce să schimbe."""


@dataclass
class Memory:
    #: handle → product_id, în ordinea folosirii (cel mai recent ultimul).
    handles: dict[str, str] = field(default_factory=dict)
    next: int = 1
    notes: str = ""
    #: NX-407: sugestiile oferite sub răspunsul anterior, ca agentul să nu le ofere din nou.
    offered: list[str] = field(default_factory=list)

    @classmethod
    def from_state(cls, raw: Any) -> Memory:
        """Defensiv pe orice formă: o stare veche sau stricată dă o memorie goală, nu o excepție."""
        raw = raw if isinstance(raw, dict) else {}
        handles = {
            str(h): str(pid)
            for h, pid in (raw.get("h") or {}).items()
            if _HANDLE_RE.match(str(h)) and pid
        }
        numbers = [int(h[1:]) for h in handles]
        try:
            nxt = int(raw.get("n") or 1)
        except (TypeError, ValueError):
            nxt = 1
        notes = raw.get("notes") if isinstance(raw.get("notes"), str) else ""
        raw_offered = raw.get("s") if isinstance(raw.get("s"), list) else []
        offered = [x for x in raw_offered if isinstance(x, str) and x.strip()]
        return cls(
            handles=handles,
            next=max([nxt, *(n + 1 for n in numbers)]),
            notes=notes,
            offered=offered[:OFFERED_MAX],
        )

    def to_state(self) -> dict[str, Any]:
        """Forma persistată: doar cele mai recente `MAX_HANDLES`. Plafonul se aplică AICI, la
        sfârșitul turului, nu în timpul lui: un handle văzut de agent în tur (un card, un rând de
        căutare) nu poate dispărea înainte ca răspunsul să-l folosească."""
        kept = dict(list(self.handles.items())[-MAX_HANDLES:])
        out: dict[str, Any] = {"h": kept, "n": self.next, "notes": self.notes[:NOTES_MAX]}
        offered = [s.strip()[:OFFERED_CHARS] for s in self.offered if s.strip()][:OFFERED_MAX]
        if offered:
            out["s"] = offered
        return out

    def handle_of(self, product_id: str, *, touch: bool = True) -> str:
        """Handle-ul produsului (nou dacă nu-l are). `touch` îl mută la coada celor recente;
        fără, un handle existent își păstrează locul (istoricul nu reordonează memoria)."""
        pid = str(product_id)
        h = next((k for k, v in self.handles.items() if v == pid), None)
        if h is None:
            h = f"P{self.next}"
            self.next += 1
        elif touch:
            del self.handles[h]
        else:
            return h
        self.handles[h] = pid
        return h

    def id_of(self, handle: str) -> str | None:
        return self.handles.get(handle)

    def check(self, handle: Any) -> str:
        """Un handle valid și cunoscut, altfel `Refused` cu lista celor valide."""
        h = str(handle or "")
        if not _HANDLE_RE.match(h):
            raise Refused(f"{h!r} is not a handle; use a handle like P1 from PRODUCTS YOU KNOW")
        if h not in self.handles:
            valid = ", ".join(list(self.handles)[-20:]) or "none yet: search first"
            raise Refused(f"unknown handle {h}; valid handles: {valid}")
        return h

    def recent(self, n: int = MAX_HANDLES) -> list[str]:
        return list(self.handles)[-n:]


# --- numele distincte ----------------------------------------------------------------------------

_WORD_RE = re.compile(r"[\w'+.%-]+", re.UNICODE)


def distinct_names(full_names: dict[str, str]) -> dict[str, str]:
    """NX-395, R4: numele scurt al fiecărui produs, deosebit de al celorlalte.

    Nuanțele și gramajele sunt produse separate cu același `display_name`, deci agentul vedea trei
    carduri identice. Pe un grup cu același nume scurt se adaugă secvența de cuvinte proprii care
    pornește de la primul cuvânt cu cifre (nuanța, gramajul): «… Cushion (23 Natural Beige)».
    Numele întregi identice primesc un număr de ordine: nimic din date nu le deosebește."""
    from src.catalog.render_text import display_name  # noqa: PLC0415

    short = {k: display_name(v) or v for k, v in full_names.items()}
    groups: dict[str, list[str]] = {}
    for k, s in short.items():
        groups.setdefault(s.casefold(), []).append(k)
    out = dict(short)
    for members in groups.values():
        if len(members) < 2:
            continue
        words = {k: _WORD_RE.findall(full_names[k] or "") for k in members}
        common = set.intersection(*({w.casefold() for w in ws} for ws in words.values()))
        for i, k in enumerate(members, 1):
            tag = _distinct_run(words[k], common)
            out[k] = f"{short[k]} ({tag or i})"
    return out


def _distinct_run(words: list[str], common: set[str]) -> str:
    """Cuvintele care deosebesc un nume de restul grupului, ca SECVENȚĂ din nume: de la primul
    cuvânt propriu cu cifre (altfel primul propriu), plus cele proprii imediat următoare, ≤ 3."""
    own = [j for j, w in enumerate(words) if w.casefold() not in common]
    if not own:
        return ""
    start = next((j for j in own if any(c.isdigit() for c in words[j])), own[0])
    run = [words[start]]
    for j in range(start + 1, min(start + 3, len(words))):
        if words[j].casefold() in common:
            break
        run.append(words[j])
    return " ".join(run)
