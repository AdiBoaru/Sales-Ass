"""Seed: concern_map RO pentru tenantul demo (NX-124, partea „perfecționăm demoul în română").

Demo-ul e vertical=`ecommerce` (încarcă `ecommerce.json` cu `concern_map={}`) → maparea
concern→cheie a fost MEREU goală, deci filtrul de concerns + concern-overlap-rerank (NX-113c)
n-au funcționat niciodată pe demo. Fix: un OVERRIDE per-tenant în
`businesses.settings['domain_pack']['concern_map']` (loader-ul deep-merge-uiește peste JSON-ul
default, NX-114) — termeni liberi (RO + EN) → valorile RO REALE stocate în
`products.attributes->'concerns'` (cu diacritice, EXACT cum sunt în DB, altfel `?|` nu prinde).

DOAR concern-uri care EXISTĂ în datele demo. Idempotent (rescrie concern_map întreg, păstrează
restul settings). Rulează: PYTHONPATH=. python scripts/seed_demo_domain_pack.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Termen liber (orice formă — loader-ul normalizează CHEILE: lower + fără diacritice) → cheia
# canonică EXACT cum e stocată în products.attributes->'concerns' (VALORILE păstrează diacriticele).
CONCERN_MAP: dict[str, str] = {
    # --- tipuri de ten ---
    "ten gras": "ten gras",
    "piele grasa": "ten gras",
    "gras": "ten gras",
    "oily": "ten gras",
    "ten uscat": "ten uscat",
    "piele uscata": "ten uscat",
    "uscat": "ten uscat",
    "dry": "ten uscat",
    "ten mixt": "ten mixt",
    "piele mixta": "ten mixt",
    "mixt": "ten mixt",
    "combination": "ten mixt",
    "ten normal": "ten normal",
    "normal": "ten normal",
    "ten sensibil": "ten sensibil",
    "piele sensibila": "ten sensibil",
    "sensibil": "ten sensibil",
    "sensitive": "ten sensibil",
    # --- nevoi ten ---
    "hidratare": "hidratare",
    "hidratant": "hidratare",
    "deshidratat": "hidratare",
    "uscaciune": "hidratare",
    "calmare": "calmare",
    "calmant": "calmare",
    "iritatii": "calmare",
    "roseata": "calmare",
    "luminozitate": "luminozitate",
    "stralucire": "luminozitate",
    "radianta": "luminozitate",
    "glow": "luminozitate",
    "protectie solara": "protecție solară",
    "spf": "protecție solară",
    "soare": "protecție solară",
    "protectie uv": "protecție solară",
    "exfoliere": "exfoliere",
    "exfoliant": "exfoliere",
    "peeling": "exfoliere",
    "riduri": "riduri",
    "antirid": "riduri",
    "anti-imbatranire": "riduri",
    "anti-aging": "riduri",
    "pete": "pete pigmentare",
    "pete pigmentare": "pete pigmentare",
    "pigmentare": "pete pigmentare",
    "hiperpigmentare": "pete pigmentare",
    "fermitate": "fermitate",
    "cearcane": "cearcăne",
    # --- zone ---
    "buze": "buze",
    "ingrijire buze": "buze",
    "ochi": "ochi",
    "conturul ochilor": "ochi",
    "contur ochi": "ochi",
    "sprancene": "sprâncene",
    # --- păr ---
    "par uscat": "păr uscat",
    "par deteriorat": "păr uscat",
    "par gras": "păr gras",
    "par vopsit": "păr vopsit",
    "vopsit": "păr vopsit",
    "volum": "volum păr",
    "volum par": "volum păr",
    "matreata": "anti-mătreață",
    "anti-matreata": "anti-mătreață",
    "antimatreata": "anti-mătreață",
    # --- uz / ocazie / machiaj ---
    "uz zilnic": "uz zilnic",
    "zilnic": "uz zilnic",
    "zi de zi": "uz zilnic",
    "cadou": "cadou",
    "gift": "cadou",
    "machiaj": "acoperire machiaj",
    "acoperire": "acoperire machiaj",
    "fond de ten": "acoperire machiaj",
    "acoperire machiaj": "acoperire machiaj",
    # --- NX-208: extindere high-confidence din cazurile compuse (concern → valoare RO reală) ---
    "ten reactiv": "ten sensibil",
    "reactiv": "ten sensibil",
    "reactiva": "ten sensibil",
    "ma lucesc": "ten gras",
    "luciu": "ten gras",
}

# NX-208: vocabular de EXPANDARE a interogării (query understanding). Frază colocvială (loader-ul
# normalizează CHEILE) → termeni canonici de căutare adăugați la `search_text`, ca lexical +
# semantic să prindă query-urile pe care textul brut le rata. Vocabular RO real, high-confidence,
# GENERIC (nu reverse-engineering pe ID-uri). Separat de concern_map (acela e mapare la filtru).
QUERY_EXPANSIONS: dict[str, list[str]] = {
    "mă lucesc": ["matifiant", "mat", "ten gras"],
    "să nu lucesc": ["matifiant", "mat"],
    "luciu": ["matifiant", "mat"],
    "lucios": ["matifiant", "mat"],
    "să reziste": ["rezistent", "long lasting"],
    "rezistent la căldură": ["rezistent", "long lasting"],
    "pe căldură": ["rezistent"],
    "aspect natural": ["natural", "acoperire lejeră"],
    "machiaj discret": ["natural", "lejer", "acoperire lejeră"],
    "toată rutina": ["curățare", "ser", "cremă hidratantă", "hidratare"],
    "rutina de față": ["curățare", "ser", "cremă hidratantă", "hidratare"],
    "apă micelară": ["demachiant", "apă micelară"],
    "reactiv": ["ten sensibil"],
}


async def main() -> None:
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            raw = await conn.fetchval("select settings from businesses where id=$1", DEMO_BIZ)
            settings = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
            dp = settings.get("domain_pack") or {}
            dp["concern_map"] = CONCERN_MAP
            dp["query_expansions"] = QUERY_EXPANSIONS  # NX-208
            settings["domain_pack"] = dp
            await conn.execute(
                "update businesses set settings = $2::jsonb where id = $1",
                DEMO_BIZ,
                json.dumps(settings, ensure_ascii=False),
            )
            print(
                f"OK: domain_pack override scris pe {DEMO_BIZ} — concern_map ({len(CONCERN_MAP)} "
                f"intrări) + query_expansions ({len(QUERY_EXPANSIONS)} intrări)"
            )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
