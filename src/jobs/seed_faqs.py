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
from src.cache.canonical import canonicalize
from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_pool

log = logging.getLogger(__name__)

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"  # nativex-demo (CLAUDE.md)

# Baza curatată RO (cosmetice/beauty) — set construit din cercetare de piață (magazine RO: Notino,
# Douglas, Sephora, ProBeauty, MakeupShop + best-practices internaționale) și aliniat legal RO:
# OUG 34/2014 (retur 14 zile + excepția cosmeticelor desigilate, Art. 16 lit. e), OUG 140/2021
# (garanție 2 ani), RO e-Factura, GDPR, ANPC/SOL. Adevărul de business pentru DEMO, editabil de
# dashboard. (OC) = conține un default realist de piață (cost/prag/transport) DE CONFIRMAT.
BASE_FAQS_RO: list[tuple[str, str]] = [
    # --- livrare ---
    (
        "Cât costă livrarea?",  # (OC)
        "Livrarea prin curier rapid costă 19,99 lei pe teritoriul României, iar comanda "
        "ajunge în 1-3 zile lucrătoare de la confirmare. Costul exact și curierul se văd "
        "la finalizarea comenzii.",
    ),
    (
        "De la ce sumă e livrarea gratuită?",  # (OC)
        "Livrarea este gratuită la comenzile de peste 200 lei. Sub acest prag se aplică "
        "taxa standard de transport, afișată la finalizarea comenzii.",
    ),
    (
        "În câte zile primesc comanda?",
        "Comenzile se procesează în 24-48 de ore, iar livrarea prin curier durează 1-3 zile "
        "lucrătoare. Comenzile plasate în weekend se expediază începând de luni.",
    ),
    # --- comenzi ---
    (
        "Cum urmăresc coletul și unde văd AWB-ul?",
        "După expediere primești numărul AWB și linkul de urmărire pe e-mail și pe WhatsApp. "
        "Poți verifica statusul oricând pe site-ul curierului sau întrebându-mă aici.",
    ),
    (
        "Cum știu că s-a confirmat comanda?",
        "Primești imediat un e-mail de confirmare cu detaliile comenzii. Dacă nu îl găsești, "
        "verifică și folderul spam; îți pot confirma și eu starea dacă îmi spui numărul comenzii.",
    ),
    (
        "Pot anula sau modifica o comandă?",
        "Poți anula sau modifica comanda doar înainte să fie procesată și expediată; după "
        "expediere, opțiunea rămâne returul. Scrie-ne cât mai repede ca să putem interveni.",
    ),
    (
        "Primesc factură pentru comandă?",
        "Da, emitem factură fiscală pentru fiecare comandă și o primești pe e-mail. Din 2025 "
        "facturile se transmit și în sistemul național RO e-Factura. Pentru factură pe firmă, "
        "completează datele companiei la plasarea comenzii.",
    ),
    # --- plată ---
    (
        "Ce metode de plată acceptați?",  # (OC)
        "Poți plăti cu cardul online (Visa/Mastercard, securizat 3D Secure) sau ramburs la "
        "livrare (cash sau card la curier). Datele cardului sunt procesate criptat de "
        "procesatorul de plăți și nu se stochează la noi.",
    ),
    (
        "Plata cu cardul e sigură?",
        "Da. Plățile cu cardul sunt procesate securizat, prin 3D Secure, de un procesator "
        "autorizat. Magazinul nu vede și nu stochează datele cardului tău.",
    ),
    (
        "Pot plăti ramburs la livrare?",  # (OC)
        "Da, accepți plata ramburs la curier — cash sau card în momentul livrării. La unele "
        "comenzi se poate adăuga un comision de ramburs, afișat la finalizarea comenzii.",
    ),
    # --- retur ---
    (
        "Cum returnez un produs?",
        "Ai 14 zile calendaristice de la primirea coletului să te retragi din comandă, fără "
        "să justifici motivul. Trimite-ne întâi cererea de retragere (e-mail sau formular), "
        "apoi ai încă 14 zile să returnezi produsul (conform OUG 34/2014).",
    ),
    (
        "Pot returna un produs cosmetic desigilat sau deschis?",
        "Nu. Din motive de igienă și protecția sănătății, produsele cosmetice desigilate "
        "(deschise, folosite sau cu sigiliul rupt) sunt exceptate de la dreptul de retur, "
        "conform Art. 16 lit. e) din OUG 34/2014. Cele rămase sigilate pot fi returnate în 14 zile."
        " calendaristice.",
    ),
    (
        "În cât timp primesc banii înapoi la retur?",
        "Îți rambursăm integral suma plătită, inclusiv costul livrării standard, în cel mult "
        "14 zile de la primirea coletului returnat (sau a dovezii de expediere). Rambursarea "
        "se face pe aceeași metodă de plată folosită la comandă.",
    ),
    (
        "Cine plătește transportul de retur?",  # (OC)
        "Costul transportului de retur este suportat de tine, conform legii. Excepție: dacă "
        "produsul a fost livrat greșit, deteriorat sau defect, returul e pe cheltuiala noastră.",
    ),
    (
        "Ce fac dacă am primit produsul greșit sau deteriorat?",
        "Ne pare rău pentru neplăcere. Scrie-ne cu numărul comenzii și, dacă poți, o poză a "
        "produsului și a coletului. Înlocuim produsul sau îți returnăm banii, fără costuri de "
        "transport pentru tine.",
    ),
    # --- produse (specific cosmetice) ---
    (
        "Sunt produsele originale?",
        "Da, vindem exclusiv produse 100% originale, achiziționate de la distribuitorii "
        "oficiali ai brandurilor. Fiecare produs ajunge sigilat, cu lot și termen de "
        "valabilitate, și este conform reglementărilor UE.",
    ),
    (
        "Unde găsesc lista de ingrediente? Am alergie la ceva.",
        "Lista completă de ingrediente (INCI) este afișată pe pagina fiecărui produs. Dacă "
        "ești alergic(ă) la un ingredient, verifică lista înainte de comandă. Pentru piele "
        "sensibilă îți recomand un patch test, iar pentru sănătate consultă un medic.",
    ),
    (
        "Ce înseamnă PAO și cât ține produsul după deschidere?",
        "PAO (Period After Opening) e simbolul borcanului deschis de pe ambalaj, cu un număr "
        "urmat de „M” (ex. 6M = 6 luni de la deschidere). Până la deschidere e valabil până "
        "la termenul inscripționat. Păstrează produsele ferite de soare și căldură.",
    ),
    (
        "Cum aleg crema potrivită pentru tenul meu?",
        "Spune-mi tipul de ten (gras, uscat, mixt, sensibil) și ce te preocupă (acnee, riduri, "
        "pete, roșeață) și îți recomand produse potrivite din catalog, cu link direct. "
        "Rutina uzuală: curățare, ser, cremă și SPF dimineața.",
    ),
    (
        "Am avut o reacție sau iritație de la un produs, ce fac?",
        "Oprește imediat folosirea produsului și, dacă simptomele persistă, consultă un medic. "
        "Eu nu pot oferi sfat medical, dar te pot pune în legătură cu un coleg din echipă "
        "pentru opțiunile de retur sau înlocuire.",
    ),
    # --- cont ---
    (
        "Trebuie să îmi fac cont ca să comand?",
        "Nu este obligatoriu să ai cont pentru a comanda. Un cont îți aduce însă beneficii: "
        "istoricul comenzilor, urmărirea statusului și comenzi mai rapide data viitoare.",
    ),
    (
        "Mi-am uitat parola, cum o resetez?",
        "Folosește opțiunea „Am uitat parola” de pe pagina de autentificare și vei primi un "
        "e-mail cu link pentru resetare. Dacă nu îl găsești, verifică și folderul spam.",
    ),
    # --- legal ---
    (
        "Ce garanție au produsele?",
        "Toate produsele beneficiază de garanția legală de conformitate de 2 ani de la "
        "livrare, conform OUG 140/2021. La un defect ai dreptul, după caz, la reparare, "
        "înlocuire, reducere de preț sau banii înapoi. E diferită de termenul de valabilitate.",
    ),
    (
        "Ce faceți cu datele mele personale (GDPR)?",
        "Datele tale (nume, adresă, telefon, e-mail) sunt prelucrate pentru a procesa, factura "
        "și livra comanda, conform GDPR. Nu le folosim pentru marketing decât cu acordul tău. "
        "Ai drept de acces, rectificare și ștergere.",
    ),
    # --- contact ---
    (
        "Cum vă contactez? Pot vorbi cu un om?",
        "Mă poți întreba aici orice, iar dacă ai nevoie de un coleg din echipă te pot pune în "
        "legătură cu un operator uman. Pentru o reclamație, scrie-ne întâi nouă; dacă nu găsim "
        "o soluție, te poți adresa ANPC (anpc.ro) ori platformei SOL a Comisiei Europene.",
    ),
]

# Variante de formulare (NX-124a): ACELAȘI răspuns, dar întrebarea e formulată cum tastează clienții
# RO (terse, fără diacritice) → după `canonicalize` matchează aproape exact mesajul lor (recall mai
# bun pe straturile gratuite). Răspunsul e REUTILIZAT din întrebarea-părinte (fără duplicare).
_BY_Q = {q: a for q, a in BASE_FAQS_RO}
_VARIANTS_RO: list[tuple[str, str]] = [
    ("Acceptați ramburs?", _BY_Q["Pot plăti ramburs la livrare?"]),
    ("Cum plătesc?", _BY_Q["Ce metode de plată acceptați?"]),
    ("Îmi dați factură?", _BY_Q["Primesc factură pentru comandă?"]),
    ("Vreau să vorbesc cu un operator.", _BY_Q["Cum vă contactez? Pot vorbi cu un om?"]),
    ("Aveți livrare gratuită?", _BY_Q["De la ce sumă e livrarea gratuită?"]),
    (
        "Cât ține produsul după deschidere?",
        _BY_Q["Ce înseamnă PAO și cât ține produsul după deschidere?"],
    ),
    (
        "Pot returna un produs desfăcut?",
        _BY_Q["Pot returna un produs cosmetic desigilat sau deschis?"],
    ),
]
BASE_FAQS_RO = BASE_FAQS_RO + _VARIANTS_RO

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
        # NX-124a: embed pe `canonicalize(question)` (fără diacritice + punctuație) → paritate cu
        # lookup-ul (faq_stage/faq_lookup embed-uiesc tot canonical) → recall mult mai bun pe RO.
        raw = await llm.embed([canonicalize(q)[0] for q, _ in unique])
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
