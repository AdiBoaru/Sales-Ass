"""Job: generează + populează `faqs` (stratul gratuit FAQ, NX-74).

Stratul gratuit FAQ (codul NX-74) nu poate servi nimic cât timp `faqs` e gol pe demo.
Acest job umple tabelul în două moduri, combinabile:

  1. **Bază curatată** (default): un set fix de Q/A retail în RO (retur, livrare, plată,
     garanție, facturare, tracking, program) — adevărul de business pentru DEMO. Determinist,
     nu inventat de model la runtime (principiul „un singur adevăr editat de client, nu
     halucinat" din NX-74).
  2. **Generare LLM** (`--generate`): cere modelului întrebări frecvente SUPLIMENTARE pentru
     verticalul businessului, cu răspunsuri plauzibile de demo (ca `ai_summary` la produse).
     Best-effort — eșec / fără cheie → doar baza curatată.

Fiecare FAQ primește un embedding pe ÎNTREBARE (lookup-ul din stagiu/tool embed-uiește
mesajul clientului și caută cel mai apropiat). Upsert idempotent pe `(business_id, question,
locale)`: re-rulare = update answer+embedding, nu duplicate. Rulează ca ADMIN (scrie în
knowledge; `bot_runtime` are doar SELECT pe `faqs`) prin `admin_conn`, ca `embed_products`.

    python -m src.jobs.seed_faqs                       # baza curatată pe businessul demo
    python -m src.jobs.seed_faqs --generate            # bază + întrebări generate de LLM
    python -m src.jobs.seed_faqs --business <uuid>      # alt tenant
    python -m src.jobs.seed_faqs --locale ro --generate-n 8
"""

import argparse
import asyncio
import logging
from typing import Any

from src.agent.llm import get_llm
from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_pool

log = logging.getLogger(__name__)

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"  # nativex-demo (CLAUDE.md)

# Baza curatată RO (retail/beauty) — adevărul de business pentru DEMO. Editabil de client în
# dashboard ulterior; aici dăm un punct de plecare realist ca stratul gratuit să aibă ce servi.
BASE_FAQS_RO: list[tuple[str, str]] = [
    (
        "Care este politica de retur?",
        "Ai 14 zile calendaristice de la primirea coletului să returnezi produsele nedesfăcute, "
        "în ambalajul original. Banii se întorc în 5-7 zile lucrătoare după ce primim returul.",
    ),
    (
        "Cât costă livrarea și în cât timp ajunge?",
        "Livrarea prin curier costă 19,99 lei și este GRATUITĂ la comenzi peste 200 lei. Coletul "
        "ajunge de obicei în 1-3 zile lucrătoare în toată țara.",
    ),
    (
        "Ce metode de plată acceptați?",
        "Poți plăti cu cardul online (Visa/Mastercard), prin transfer bancar sau ramburs la "
        "curier (cash sau card la livrare).",
    ),
    (
        "Pot plăti ramburs la livrare?",
        "Da, accepți plata ramburs la curier — cash sau card în momentul livrării. Se poate adăuga "
        "un mic comision de ramburs afișat la finalizarea comenzii.",
    ),
    (
        "Produsele au garanție?",
        "Toate produsele sunt originale și beneficiază de garanție conform legii. Pentru produsele "
        "cosmetice respectăm termenul de valabilitate inscripționat pe ambalaj.",
    ),
    (
        "Primesc factură pentru comandă?",
        "Da, pentru fiecare comandă emitem factură fiscală. O primești pe email după confirmarea "
        "comenzii; dacă vrei factură pe firmă, adaugă datele companiei la finalizarea comenzii.",
    ),
    (
        "Cum urmăresc starea comenzii mele?",
        "După expediere primești pe email/WhatsApp numărul AWB cu link de tracking. Îmi poți "
        "scrie oricând numărul comenzii și verific eu statusul pentru tine.",
    ),
    (
        "Pot anula sau modifica o comandă?",
        "Da, dacă ne scrii înainte ca pachetul să fie predat curierului. După expediere poți "
        "refuza coletul la livrare sau folosi dreptul de retur în 14 zile.",
    ),
    (
        "Livrați în toată țara?",
        "Da, livrăm prin curier în toată România. Pentru localitățile fără acces direct al "
        "curierului, coletul ajunge la cel mai apropiat punct de ridicare.",
    ),
    (
        "Produsele sunt originale?",
        "Da, lucrăm doar cu produse 100% originale, achiziționate din surse autorizate. Nu "
        "comercializăm replici sau produse fără proveniență.",
    ),
    (
        "Aveți magazin fizic sau program de contact?",
        "Suntem magazin online; echipa de suport îți răspunde în zilele lucrătoare între 09:00 și "
        "18:00. În afara programului îți scriu eu și preiau un coleg dacă e nevoie.",
    ),
    (
        "Cum mă pot abona la o reducere sau noutăți?",
        "Îți pot trimite o notificare când un produs revine în stoc sau când apar oferte. Spune-mi "
        "doar ce produs te interesează și te anunț.",
    ),
]

# Schema strict pt generarea LLM (suplimentar bazei): listă de {question, answer} în RO.
_GEN_SCHEMA: dict[str, Any] = {
    "name": "faq_set",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["faqs"],
        "properties": {
            "faqs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question", "answer"],
                    "properties": {
                        "question": {"type": "string"},
                        "answer": {"type": "string"},
                    },
                },
            }
        },
    },
}

_GEN_SYSTEM = (
    "Ești specialist de customer support pentru un magazin online din România. Generezi "
    "întrebări frecvente (FAQ) REALISTE de la clienți, cu răspunsuri scurte, clare și "
    "prietenoase, în limba română. Răspunzi DOAR cu JSON conform schemei."
)


async def generate_faqs(llm, vertical: str, n: int) -> list[tuple[str, str]]:
    """Cere modelului `n` FAQ-uri SUPLIMENTARE pentru vertical (best-effort). [] la eșec."""
    user = (
        f"Magazin online cu profil: {vertical}. Generează {n} întrebări frecvente DIFERITE de "
        "tema retur/livrare/plată/garanție/facturare/tracking (acelea există deja). "
        "Concentrează-te pe întrebări specifice produselor și consultanței în acest domeniu. "
        "Răspunsuri de 1-2 propoziții, fără prețuri sau cifre inventate."
    )
    try:
        j = await llm.complete_schema(_GEN_SYSTEM, user, _GEN_SCHEMA)
    except Exception as e:  # noqa: BLE001 — generare best-effort
        log.warning("seed_faqs: generare LLM eșuată (%s) → doar baza curatată", type(e).__name__)
        return []
    out: list[tuple[str, str]] = []
    for item in j.get("faqs", []):
        q = (item.get("question") or "").strip()
        a = (item.get("answer") or "").strip()
        if q and a:
            out.append((q, a))
    return out


def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


async def _upsert(conn, business_id: str, locale: str, q: str, a: str, vec: str | None) -> str:
    """Upsert manual pe `(business_id, question, locale)` (fără unique în schema → select-then).
    Întoarce 'created' | 'updated'."""
    existing = await conn.fetchval(
        "select id from faqs where business_id = $1 and question = $2 and locale = $3",
        business_id,
        q,
        locale,
    )
    if existing is not None:
        await conn.execute(
            """update faqs set answer = $2, embedding = $3::vector, is_active = true,
                              updated_at = now()
               where id = $1""",
            existing,
            a,
            vec,
        )
        return "updated"
    await conn.execute(
        """insert into faqs (business_id, question, answer, locale, embedding)
           values ($1, $2, $3, $4, $5::vector)""",
        business_id,
        q,
        a,
        locale,
        vec,
    )
    return "created"


async def seed_faqs(
    conn,
    llm,
    business_id: str,
    *,
    locale: str = "ro",
    generate: bool = False,
    generate_n: int = 6,
    vertical: str = "beauty",
) -> dict[str, int]:
    """Popularea `faqs`: bază curatată (+ generare LLM opțională), embed pe întrebare, upsert
    idempotent. Întoarce {'created', 'updated', 'embedded'}."""
    faqs = list(BASE_FAQS_RO)
    if generate and llm is not None:
        faqs += await generate_faqs(llm, vertical, generate_n)

    # Dedupe pe întrebare (baza + generate pot coincide), păstrând prima variantă.
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for q, a in faqs:
        key = q.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append((q.strip(), a.strip()))

    # Embed pe ÎNTREBARE (lookup-ul embed-uiește mesajul clientului). Fără LLM → embedding NULL
    # (rândul există dar nu e servit de lookup până la un re-run cu cheie; vezi `embedding is
    # not null` în query).
    vectors: list[str | None] = [None] * len(unique)
    embedded = 0
    if llm is not None:
        raw = await llm.embed([q for q, _ in unique])
        vectors = [_vec(v) for v in raw]
        embedded = len(vectors)

    stats = {"created": 0, "updated": 0, "embedded": embedded}
    async with conn.transaction():
        for (q, a), vec in zip(unique, vectors, strict=True):
            stats[await _upsert(conn, business_id, locale, q, a, vec)] += 1
    return stats


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default=DEMO_BIZ, help="business_id țintă (default: demo)")
    ap.add_argument("--locale", default="ro")
    ap.add_argument("--generate", action="store_true", help="adaugă FAQ generate de LLM")
    ap.add_argument("--generate-n", type=int, default=6)
    ap.add_argument("--vertical", default="beauty")
    args = ap.parse_args()

    llm = get_llm()
    if llm is None:
        log.warning(
            "OPENAI_API_KEY lipsește — inserez FAQ-urile FĂRĂ embedding (nu vor fi servite de "
            "lookup până la un re-run cu cheie). Generarea LLM e dezactivată."
        )
    elif not get_settings().faq_enabled:
        log.warning("FAQ_ENABLED=false — populez oricum tabelul (kill-switch e doar pe runtime).")

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            stats = await seed_faqs(
                conn,
                llm,
                args.business,
                locale=args.locale,
                generate=args.generate,
                generate_n=args.generate_n,
                vertical=args.vertical,
            )
        log.info(
            "FAQ seed gata pe %s: %d create, %d actualizate, %d embed-uite",
            args.business,
            stats["created"],
            stats["updated"],
            stats["embedded"],
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
