"""NX-90 — spargerea reply-ului lung în MAX 2 mesaje (funcție pură, fără I/O).

CLAUDE.md stagiul 9: „răspuns spart în 2 mesaje scurte dacă > 200 caractere". Un perete de
text pe telefon se citește greu; 2 bule scurte = experiență „om care scrie". Tăiem la ULTIMA
graniță naturală ≤ limit (paragraf → linie → sfârșit de propoziție → spațiu), fără să rupem
cuvinte. Restul = al doilea fragment (chiar dacă > limit: regula e 2 mesaje, NU N).
"""

from __future__ import annotations


def split_reply(text: str, *, limit: int = 200) -> list[str]:
    """Întoarce 1 sau 2 fragmente. `len(text) <= limit` → `[text]`. Altfel taie la cea mai
    târzie graniță naturală ≤ limit: `\\n\\n` → `\\n` → `. ! ?` → spațiu; fallback = tăiere dură
    la `limit` (un singur „cuvânt"/URL lung). Granița de spațiu/linie e consumată; punctuația de
    final de propoziție rămâne în primul fragment. Niciun cuvânt rupt (decât la fallback dur)."""
    if len(text) <= limit:
        return [text]

    head = text[:limit]

    # 1) graniță de paragraf / linie — consumată (nu apare în niciun fragment)
    for sep in ("\n\n", "\n"):
        idx = head.rfind(sep)
        if idx > 0:
            return [text[:idx].rstrip(), text[idx + len(sep) :].strip()]

    # 2) sfârșit de propoziție — punctuația rămâne în primul fragment
    end = max(head.rfind("."), head.rfind("!"), head.rfind("?"))
    if end > 0:
        return [text[: end + 1].strip(), text[end + 1 :].strip()]

    # 3) ultimul spațiu — consumat
    idx = head.rfind(" ")
    if idx > 0:
        return [text[:idx].rstrip(), text[idx + 1 :].strip()]

    # 4) fallback dur: niciun separator în primii `limit` chars → tăiem la limit (nimic pierdut)
    return [text[:limit], text[limit:]]
