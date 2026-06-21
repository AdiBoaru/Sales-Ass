"""NX-90 — spargerea reply-ului lung în MAX 2 mesaje (funcție pură, fără I/O).

CLAUDE.md stagiul 9: „răspuns spart în 2 mesaje scurte dacă > 200 caractere". Un perete de
text pe telefon se citește greu; 2 bule scurte = experiență „om care scrie". Tăiem la ULTIMA
graniță naturală ≤ limit (paragraf → linie → sfârșit de propoziție → spațiu), fără să rupem
cuvinte. Restul = al doilea fragment (chiar dacă > limit: regula e 2 mesaje, NU N).
"""

from __future__ import annotations

# NX-126: glife de început de element de listă — la o tăiere care le-ar orfana în primul fragment,
# le mutăm în fragmentul 2 (bullet-ul trebuie să rămână lipit de item-ul lui).
_LIST_GLYPHS = ("•", "‣", "◦", "-", "*", "–")


def _bullet_safe(head: str, tail: str) -> list[str]:
    """Dacă tăierea a lăsat o glifă de listă ORFANĂ la finalul head-ului (bullet fără text),
    o mută la începutul fragmentului 2 ca să nu se piardă (NX-126)."""
    h = head.rstrip()
    for glyph in _LIST_GLYPHS:
        # glifă la finalul head-ului, precedată de spațiu/linie/început → e un bullet orfan.
        if h.endswith(glyph) and (len(h) == len(glyph) or h[-len(glyph) - 1] in " \n\t"):
            return [h[: -len(glyph)].rstrip(), f"{glyph} {tail}".strip()]
    return [head, tail]


def split_reply(text: str, *, limit: int = 200, min_head: int | None = None) -> list[str]:
    """Întoarce 1 sau 2 fragmente. `len(text) <= limit` → `[text]`. Altfel taie la cea mai
    târzie graniță naturală ≤ limit: `\\n\\n` → `\\n` → `. ! ?` → spațiu; fallback = tăiere dură
    la `limit`. Granița de spațiu/linie e consumată; punctuația de final de propoziție rămâne în
    primul fragment. Niciun cuvânt rupt (decât la fallback dur).

    NX-126: `min_head` (default `limit // 4`) evită un prim fragment minuscul („Da.") — o graniță
    sub prag e sărită pentru una mai târzie; dacă niciuna nu trece pragul, tăiere dură pentru un
    head rezonabil. Glifa de listă de la începutul fragmentului 2 e păstrată (`_bullet_safe`)."""
    if len(text) <= limit:
        return [text]
    if min_head is None:
        min_head = max(1, limit // 4)

    head = text[:limit]

    # 1) graniță de paragraf / linie — consumată (bullet-ul de după rămâne în fragmentul 2 la strip)
    for sep in ("\n\n", "\n"):
        idx = head.rfind(sep)
        if idx >= min_head:
            return [text[:idx].rstrip(), text[idx + len(sep) :].strip()]

    # 2) sfârșit de propoziție — punctuația rămâne în primul fragment
    end = max(head.rfind("."), head.rfind("!"), head.rfind("?"))
    if end >= min_head:
        return _bullet_safe(text[: end + 1].strip(), text[end + 1 :].strip())

    # 3) ultimul spațiu — consumat
    idx = head.rfind(" ")
    if idx >= min_head:
        return _bullet_safe(text[:idx].rstrip(), text[idx + 1 :].strip())

    # 4) fallback dur: nicio graniță bună ≥ min_head → tăiem la limit (nimic pierdut)
    return [text[:limit], text[limit:]]
