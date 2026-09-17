"""NX-247 — scenariile sintetice, tenanții de test și dependențele externe FALSE ale harnessului.

Aici trăiesc cele trei lucruri care fac gate-ul posibil fără cost și fără nedeterminism:

1. **Un embedder determinist REAL, nu un stub de zerouri.** `embed_text` proiectează fiecare token
   pe o direcție unitară derivată din sha256 și adună. Rezultatul e un spațiu vectorial în care
   „ser cu vitamina C" e efectiv mai aproape de un produs care conține acele cuvinte decât de un
   șampon — deci `search_products_semantic` (pgvector, JOIN real, HNSW real) rankează pe semnal, nu
   pe hazard. Un stub care întoarce `[0.0] * 1536` ar face ca ORICE produs să fie la fel de
   aproape: testul ar trece, iar retrievalul n-ar fi fost exersat deloc.

2. **Două cataloage sintetice, în două tenanți al căror UUID diferă DOAR în ultimul nibble.** Un
   bug de izolare care compară prefixe, trunchiază, sau se sprijină pe „ID-uri evident diferite"
   trece neobservat pe date de test comode. Aici nu are unde să se ascundă.

3. **Un model fals care nu inventează fapte.** `run_tool_loop` cheamă `execute` REAL (deci
   `search_products` lovește Postgres real, cu filtrele lui reale) și compune răspunsul EXCLUSIV
   din ce s-a întors. Consecința e importantă: validatorul (stagiul 8) și grounding guardul
   (NX-240) rulează pe fapte adevărate și pot să respingă. Un fake care ar întoarce text fix ar
   trece pe lângă exact stratul pe care gate-ul pretinde că îl verifică.

Fără rețea: singurul I/O de aici e Postgres. Contoarele (`ModelCounters`) sunt dovada pozitivă —
`calls_total == 0` pe căile care nu trebuie să ajungă la model (rate limit, body respins, acțiune
tamperată) e mai tare decât absența unei erori.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from src.catalog.folding import fold as _fold

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = ROOT / "qa-suite" / "stage1" / "web-v2"
SCENARIOS_PATH = MANIFEST_DIR / "scenarios.json"
THRESHOLDS_PATH = MANIFEST_DIR / "gate-thresholds.json"

EMBED_DIM = 1536  # `product_embeddings.embedding` e `vector(1536)` — dimensiunea nu e negociabilă


# ── Embedder determinist ────────────────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9]+")


#: Aceeași normalizare pe care o face catalogul, IMPORTATĂ din unicul ei producător — ca spațiul
#: semantic sintetic să nu fie mai indulgent decât SQL-ul. Copia locală de dinainte folosea NFKD și
#: era deci mai indulgentă exact pe numele internaționale, adică fix unde harnessul ar fi trebuit să
#: exerseze potrivirea.
fold = _fold


def tokens(text: str) -> list[str]:
    return _WORD_RE.findall(fold(text))


@lru_cache(maxsize=4096)
def concept_vector(token: str) -> tuple[float, ...]:
    """Direcția unitară a unui token: 1536 de componente din fluxul sha256 al tokenului.

    Determinist între procese și între platforme (sha256 + `int.from_bytes` big-endian), deci
    embeddingul seedat în DB și cel calculat la runtime sunt bit-identice. Componentele sunt
    centrate în [-1, 1) ca vectorii diferiți să fie ~ortogonali, nu toți în același octant.
    """
    raw = bytearray()
    counter = 0
    while len(raw) < EMBED_DIM * 2:
        raw += hashlib.sha256(f"{token}:{counter}".encode()).digest()
        counter += 1
    vals = [int.from_bytes(raw[i * 2 : i * 2 + 2], "big") / 32768.0 - 1.0 for i in range(EMBED_DIM)]
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return tuple(v / norm for v in vals)


def embed_text(text: str) -> list[float]:
    """Suma normalizată a direcțiilor tokenilor. Text gol → vector zero (valid pentru pgvector)."""
    acc = [0.0] * EMBED_DIM
    seen = tokens(text)
    if not seen:
        return acc
    for tok in seen:
        vec = concept_vector(tok)
        for i in range(EMBED_DIM):
            acc[i] += vec[i]
    norm = math.sqrt(sum(v * v for v in acc)) or 1.0
    return [v / norm for v in acc]


# ── Catalog sintetic ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SyntheticProduct:
    """Un produs inventat, cu tot ce cere read-path-ul ca să fie SERVIBIL.

    Prețurile sunt numere întregi deliberat: validatorul (`_prices_ok`) compară cu toleranță 0,5,
    iar un preț cu zecimale ar face testul să depindă de rotunjirea de formatare în loc de fapte.
    `synced_at` e setat (NX-240): fără moment de VERIFICARE, guardul declară produsul nevandabil
    și n-ar exista niciun CTA de coș de testat.
    """

    external_id: str
    name: str
    summary: str
    price: int
    category_slug: str
    concerns: tuple[str, ...] = ()
    key_ingredients: tuple[str, ...] = ()
    sale_price: int | None = None
    stock_total: int = 12
    availability: str = "in_stock"
    rating: float = 4.6
    review_count: int = 40

    @property
    def slug(self) -> str:
        return self.external_id

    @property
    def document(self) -> str:
        """Textul pe care se calculează embeddingul: exact ce vede jobul real (`name + ai_summary`
        + fațete). Dacă asta ar diferi de ce cere queryul, retrievalul ar fi noroc, nu semnal."""
        return " ".join(
            [self.name, self.summary, *self.concerns, *self.key_ingredients, self.category_slug]
        )

    @property
    def effective_price(self) -> int:
        return self.sale_price if self.sale_price is not None else self.price


#: Catalogul tenantului `alpha`. Vocabular ales ca scenariile canonice să aibă câștigători CLARI:
#: „vitamina C" + „ten uscat" selectează trei produse și le ordonează stabil.
ALPHA_PRODUCTS: tuple[SyntheticProduct, ...] = (
    SyntheticProduct(
        external_id="e2e-alpha-ser-vitamina-c-hidratant",
        name="Ser cu vitamina C hidratant Lumea Blanda",
        summary="Ser cu vitamina C pentru ten uscat, cu hidratare de zi.",
        price=89,
        category_slug="ser",
        concerns=("ten_uscat", "luminozitate"),
        key_ingredients=("vitamina c", "acid hialuronic"),
    ),
    SyntheticProduct(
        external_id="e2e-alpha-ser-vitamina-c-intens",
        name="Ser cu vitamina C intens Lumea Blanda",
        summary="Ser cu vitamina C concentrat pentru ten uscat si lipsit de strălucire.",
        price=129,
        sale_price=99,
        category_slug="ser",
        concerns=("ten_uscat", "luminozitate"),
        key_ingredients=("vitamina c",),
    ),
    SyntheticProduct(
        external_id="e2e-alpha-ser-vitamina-c-usor",
        name="Ser cu vitamina C usor Lumea Blanda",
        summary="Ser cu vitamina C cu textura ușoară pentru ten uscat sensibil.",
        price=69,
        category_slug="ser",
        concerns=("ten_uscat", "sensibil"),
        key_ingredients=("vitamina c",),
    ),
    SyntheticProduct(
        external_id="e2e-alpha-crema-hidratanta",
        name="Crema hidratanta de noapte Lumea Blanda",
        summary="Cremă de noapte pentru ten uscat, în rutina de seară.",
        price=79,
        category_slug="crema",
        concerns=("ten_uscat",),
        key_ingredients=("ceramide",),
    ),
    SyntheticProduct(
        external_id="e2e-alpha-sampon-par-gras",
        name="Sampon pentru par gras Lumea Blanda",
        summary="Șampon pentru păr gras, spălare blândă.",
        price=39,
        category_slug="sampon",
        concerns=("par_gras",),
    ),
    SyntheticProduct(
        external_id="e2e-alpha-ser-epuizat",
        name="Ser calmant epuizat Lumea Blanda",
        summary="Ser calmant pentru ten sensibil, momentan indisponibil.",
        price=59,
        category_slug="ser",
        concerns=("sensibil",),
        stock_total=0,
        availability="out_of_stock",
    ),
)

#: Catalogul tenantului `beta`. Nume și limbă DIFERITE de alpha: dacă un rând al lui beta apare
#: într-un răspuns al lui alpha, se vede din prima, fără să compari ID-uri.
BETA_PRODUCTS: tuple[SyntheticProduct, ...] = (
    SyntheticProduct(
        external_id="e2e-beta-vitamin-c-serum",
        name="Northwind Vitamin C Daily Serum",
        summary="Vitamin C serum for dry skin, daily brightening.",
        price=95,
        category_slug="serum",
        concerns=("dry_skin",),
        key_ingredients=("vitamin c",),
    ),
    SyntheticProduct(
        external_id="e2e-beta-night-cream",
        name="Northwind Night Recovery Cream",
        summary="Night cream for dry skin, evening routine.",
        price=110,
        category_slug="cream",
        concerns=("dry_skin",),
    ),
)


@dataclass(frozen=True, slots=True)
class SyntheticTenant:
    """Un tenant de test complet: business + canal webchat + catalog + secretul de sesiune."""

    key: str
    business_id: str
    slug: str
    locale: str
    channel_id: str
    channel_token: str
    session_secret: str
    products: tuple[SyntheticProduct, ...]

    def by_external_id(self, external_id: str) -> SyntheticProduct:
        for p in self.products:
            if p.external_id == external_id:
                return p
        raise KeyError(external_id)

    @property
    def price_snapshot(self) -> dict[str, int]:
        """name → preț efectiv. Sursa de adevăr a invariantului `prices_match_catalog_snapshot`:
        se compară vederea livrată cu ce am SEEDAT, nu cu ce a citit tot din DB același cod."""
        return {p.name: p.effective_price for p in self.products}


def sibling_business_ids() -> tuple[str, str]:
    """Două UUID-uri care diferă DOAR în ultimul nibble.

    Nu e cochetărie: un `where business_id::text like $1 || '%'` scris greșit, o comparație pe
    primele caractere sau un cache cu cheie trunchiată trec toate pe ID-uri „evident diferite".
    Aici nu trec.
    """
    base = uuid4()
    low = UUID(int=(base.int & ~0xF) | 0xA)
    high = UUID(int=(base.int & ~0xF) | 0xB)
    return str(low), str(high)


def make_tenants() -> tuple[SyntheticTenant, SyntheticTenant]:
    alpha_id, beta_id = sibling_business_ids()
    suffix = alpha_id[:8]
    alpha = SyntheticTenant(
        key="alpha",
        business_id=alpha_id,
        slug=f"e2e-alpha-{suffix}",
        locale="ro",
        channel_id=str(uuid4()),
        channel_token=f"e2e-alpha-tok-{uuid4().hex[:12]}",
        session_secret=f"e2e-alpha-secret-{uuid4().hex}",
        products=ALPHA_PRODUCTS,
    )
    beta = SyntheticTenant(
        key="beta",
        business_id=beta_id,
        slug=f"e2e-beta-{suffix}",
        locale="en",
        channel_id=str(uuid4()),
        channel_token=f"e2e-beta-tok-{uuid4().hex[:12]}",
        session_secret=f"e2e-beta-secret-{uuid4().hex}",
        products=BETA_PRODUCTS,
    )
    return alpha, beta


# ── Seed / teardown (Postgres real, admin_conn) ─────────────────────────────────────────────


async def seed_tenant(conn, tenant: SyntheticTenant, *, embed_model: str) -> None:
    """Creează business + canal + brand + categorii + produse + embeddings pentru un tenant.

    `embed_model` TREBUIE să fie `settings.model_embed`: read-path-ul filtrează explicit pe model
    (`has_embeddings`), deci un embedding scris cu alt nume de model e invizibil — testul ar cădea
    pe calea lexicală și ar raporta verde pe altă cale decât cea de producție.
    """
    # Codecul pgvector se înregistrează pe `bot_pool` (init), NU pe poolul admin — iar seedarea
    # rulează pe admin (control plane, înainte ca tenantul să existe). Fără el, `list[float]` ajunge
    # la asyncpg ca listă și primim `DataError: expected str`. Idempotent și defensiv (vezi
    # `register_vector_codec`): dacă tipul lipsește, nu rupe nimic — dar atunci embeddingurile n-ar
    # intra, iar testul de retrieval semantic ar cădea zgomotos, cum trebuie.
    from src.db.connection import register_vector_codec

    await register_vector_codec(conn)
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale, "
        "supported_locales, settings) values ($1, $2, $3, 'ecommerce', 'active', $4, $5, $6)",
        tenant.business_id,
        tenant.slug,
        f"NX-247 {tenant.key}",
        tenant.locale,
        [tenant.locale],
        json.dumps({"store_base_url": f"https://{tenant.key}.e2e.invalid"}),
    )
    # `session_secret` stă în `channels.settings`, NU în `credentials_ref` (acela e o referință în
    # secret manager). `resolve_web_session` citește `settings->>'session_secret'` și întoarce None
    # fără el — adică bootstrap-ul ar da 403 și n-am avea nicio sesiune de testat.
    await conn.execute(
        "insert into channels (id, business_id, kind, provider_account_id, settings, status) "
        "values ($1, $2, 'webchat', $3, $4::jsonb, 'active')",
        tenant.channel_id,
        tenant.business_id,
        tenant.channel_token,
        json.dumps({"session_secret": tenant.session_secret}),
    )
    brand_id = str(uuid4())
    await conn.execute(
        "insert into brands (id, business_id, name, slug) values ($1, $2, $3, $4)",
        brand_id,
        tenant.business_id,
        "Lumea Blanda" if tenant.key == "alpha" else "Northwind",
        f"brand-{tenant.key}",
    )
    categories: dict[str, str] = {}
    for slug in sorted({p.category_slug for p in tenant.products}):
        cat_id = str(uuid4())
        categories[slug] = cat_id
        await conn.execute(
            "insert into categories (id, business_id, name, slug, path) "
            "values ($1, $2, $3, $4, $4)",
            cat_id,
            tenant.business_id,
            slug.replace("_", " ").title(),
            slug,
        )
    for product in tenant.products:
        product_id = str(uuid4())
        await conn.execute(
            "insert into products (id, business_id, brand_id, primary_category_id, external_id, "
            "name, slug, short_description, description, ai_summary, price, sale_price, "
            "availability, stock_total, rating, review_count, status, attributes, product_url, "
            "content_status, synced_at) values ($1,$2,$3,$4,$5,$6,$7,$8,$8,$9,$10,$11,$12,$13,"
            "$14,$15,'active',$16,$17,'published', now())",
            product_id,
            tenant.business_id,
            brand_id,
            categories[product.category_slug],
            product.external_id,
            product.name,
            product.slug,
            product.summary,
            product.summary,
            product.price,
            product.sale_price,
            product.availability,
            product.stock_total,
            product.rating,
            product.review_count,
            json.dumps(
                {
                    "concerns": list(product.concerns),
                    "key_ingredients": list(product.key_ingredients),
                }
            ),
            f"https://{tenant.key}.e2e.invalid/p/{product.slug}",
        )
        await conn.execute(
            "insert into product_embeddings (product_id, business_id, model, doc_type, embedding, "
            "content_hash) values ($1, $2, $3, 'product', $4, $5)",
            product_id,
            tenant.business_id,
            embed_model,
            embed_text(product.document),
            hashlib.sha256(product.document.encode()).hexdigest(),
        )


#: Prefixul de slug al tenanților sintetici. Există ca purja să fie posibilă: un `kill -9` peste
#: launcher sare peste `finally`, iar „stack efemer" ar deveni o afirmație falsă după primul crash.
SYNTHETIC_SLUG_PREFIX = "e2e-"


async def purge_synthetic_tenants(conn) -> list[str]:
    """Șterge tenanții sintetici rămași dintr-o rulare CRĂPATĂ (self-healing, ca `web_audit`).

    Prefix, nu listă: rularea care i-a creat nu mai există ca să-i spună ID-urile. Apelantul
    (`scripts/stage1_e2e_server.py`) o cheamă DOAR pe DB loopback — pe o bază partajată, o ștergere
    pe prefix ar fi o unealtă prea ascuțită pentru o problemă de igienă.
    """
    rows = await conn.fetch(
        "select id::text as id, slug from businesses where slug like $1",
        f"{SYNTHETIC_SLUG_PREFIX}%",
    )
    for row in rows:
        await drop_tenant(conn, row["id"])
    return [row["slug"] for row in rows]


async def drop_tenant(conn, business_id: str) -> None:
    """`businesses` cascadează pe tot ce e tenant-scoped. Rândurile care NU au FK către business
    (ledgerul de ture, receipts) se șterg explicit — altfel un test ar lăsa reziduu care schimbă
    numărătorile testului următor."""
    await conn.execute("delete from web_turns where business_id = $1", business_id)
    await conn.execute("delete from businesses where id = $1", business_id)


# ── Modelul fals ────────────────────────────────────────────────────────────────────────────


@dataclass
class ModelCounters:
    """Contoare de dovadă POZITIVĂ. `calls_total` e ce dovedește „zero execuție" acolo unde
    cardul cere zero: absența unei erori nu dovedește absența unui apel."""

    moderate: int = 0
    embed: int = 0
    classify: int = 0
    tool_loop: int = 0
    structured: int = 0
    complete: int = 0
    tool_calls: list[str] = field(default_factory=list)

    @property
    def calls_total(self) -> int:
        """Apelurile care ar COSTA la un provider real. `embed` intră (e tot un apel plătit)."""
        return (
            self.moderate
            + self.embed
            + self.classify
            + self.tool_loop
            + self.structured
            + (self.complete)
        )

    @property
    def generations(self) -> int:
        """Doar generările: câte răspunsuri a compus modelul. Metrica de „o singură execuție"."""
        return self.tool_loop + self.structured

    def snapshot(self) -> dict[str, Any]:
        return {
            "moderate": self.moderate,
            "embed": self.embed,
            "classify": self.classify,
            "tool_loop": self.tool_loop,
            "structured": self.structured,
            "complete": self.complete,
            "calls_total": self.calls_total,
            "generations": self.generations,
            "tool_calls": list(self.tool_calls),
        }

    def reset(self) -> None:
        self.moderate = self.embed = self.classify = self.tool_loop = 0
        self.structured = self.complete = 0
        self.tool_calls.clear()


#: Vocabular ÎNCHIS de scripturi de model. Un scenariu care cere un script inexistent e o eroare,
#: nu un „default rezonabil" — altfel un scenariu scris greșit ar testa alt drum decât crede.
MODEL_SCRIPTS: frozenset[str] = frozenset(
    {
        "text_answer",
        "recommend",
        "compare",
        "clarify",
        "no_results",
        "routine",
        "price_of_context_product",
        "pipeline_error",
        "model_timeout",
    }
)


@dataclass
class ScenarioScript:
    """Ce face modelul fals într-un tur. `script` e din `MODEL_SCRIPTS`."""

    script: str = "recommend"
    #: Secunde de „gândire" — folosit DOAR de `model_timeout`, ca deadline-ul NX-241 să taie real.
    stall_s: float = 0.0


class Stage1FakeLLM:
    """Modelul + embedderul fals ale harnessului. Implementează exact metodele pe care le cheamă
    pipeline-ul: `moderate`, `embed`, `classify_json`, `complete`, `run_tool_loop` (calea de azi)
    și `run_tool_loop_structured` / `complete_schema` (calea de creier unic, NX-239).

    Nu ține stare de conversație: turul e reconstruit din DB la fiecare execuție, exact ca în
    producție. Ce ține e scriptul CURENT (armat de harness) și contoarele.
    """

    model_agent = "fake-agent"
    model_triage = "fake-triage"

    def __init__(self) -> None:
        self.counters = ModelCounters()
        self.script = ScenarioScript()

    def arm(self, script: str, *, stall_s: float = 0.0) -> None:
        if script not in MODEL_SCRIPTS:
            raise ValueError(f"script necunoscut: {script!r} (vezi MODEL_SCRIPTS)")
        self.script = ScenarioScript(script=script, stall_s=stall_s)

    # ── metode chemate de pipeline ──────────────────────────────────────────────────────────
    async def moderate(self, text, *, model=None):
        from src.agent.llm import ModerationResult

        self.counters.moderate += 1
        return ModerationResult(flagged=False, categories=[])

    async def embed(self, texts, *, model=None):
        self.counters.embed += 1
        return [embed_text(t) for t in texts]

    async def classify_json(self, system, user, *, model=None):
        self.counters.classify += 1
        return _TRIAGE[self.script.script]

    async def complete(self, system, user, *, model=None):
        """Retry-ul validatorului. Întoarce STRING GOL deliberat: un fake care ar „repara"
        răspunsul ar ascunde exact eșecurile pe care validatorul trebuie să le producă. Gol ⇒
        pipeline-ul merge pe fallbackul determinist, care e comportamentul de producție."""
        self.counters.complete += 1
        return ""

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        self.counters.tool_loop += 1
        await self._maybe_stall()
        if self.script.script == "pipeline_error":
            raise RuntimeError("stage1-e2e: eșec injectat în bucla de tool-uri")
        results = []
        for name, args in _TOOL_PLAN[self.script.script]:
            self.counters.tool_calls.append(name)
            results.append(_parse_tool_result(await execute(name, _resolve_args(args, results))))
        return _compose(self.script.script, results)

    async def run_tool_loop_structured(self, system, user, tools, execute, schema, **kwargs):
        """Calea NX-239. Aceeași disciplină: faptele vin din `execute`, nu din fake."""
        self.counters.structured += 1
        await self._maybe_stall()
        if self.script.script == "pipeline_error":
            raise RuntimeError("stage1-e2e: eșec injectat în bucla structurată")
        results = []
        for name, args in _TOOL_PLAN[self.script.script]:
            self.counters.tool_calls.append(name)
            results.append(_parse_tool_result(await execute(name, _resolve_args(args, results))))
        return _answer_plan_v2(self.script.script, results), 1

    async def complete_schema(self, system, user, schema, **kwargs):
        self.counters.complete += 1
        return _answer_plan_v2(self.script.script, [])

    async def _maybe_stall(self) -> None:
        if self.script.stall_s > 0:
            import asyncio

            await asyncio.sleep(self.script.stall_s)


#: Ieșirea de triaj per script (contractul din `stages/triage.py`).
_TRIAGE: dict[str, dict[str, Any]] = {
    "text_answer": {
        "route": "simple",
        "reply": "Livrăm în toată țara, iar comanda pleacă în aceeași zi lucrătoare.",
    },
    "recommend": {"route": "sales", "category_key": None},
    "compare": {"route": "sales", "category_key": None},
    "clarify": {"route": "clarify", "missing_field": "category", "reply": None},
    "no_results": {"route": "sales", "category_key": None},
    "routine": {"route": "sales", "category_key": None},
    "price_of_context_product": {"route": "sales", "category_key": None},
    "pipeline_error": {"route": "sales", "category_key": None},
    "model_timeout": {"route": "sales", "category_key": None},
}

#: Planul de tool-uri per script. Numele sunt din `TOOL_NAMES` (registrul real) — un nume inventat
#: ar fi respins de `ToolRun.execute`, ceea ce e exact comportamentul dorit.
_TOOL_PLAN: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "text_answer": [],
    "recommend": [("search_products", {"query": "ser cu vitamina C pentru ten uscat"})],
    # Al DOILEA pas depinde de rezultatul primului: `compare_products` cere id-uri REALE de produs,
    # iar fake-ul nu are voie să le inventeze. Fără acest lanț, scenariul „comparison" nu producea
    # niciun bloc `comparison` (măsurat) — deci invariantul lui era declarat, dar nu se executa.
    "compare": [
        ("search_products", {"query": "ser cu vitamina C pentru ten uscat"}),
        ("compare_products", lambda prev: {"product_ids": _ids_from(prev)[:2]}),
    ],
    "clarify": [],
    # `price_max`, nu `budget_max`: numele din schema REALĂ a tool-ului. Cu numele greșit, filtrul
    # e ignorat, iar căutarea semantică întoarce oricum vecinii cei mai apropiați (cosine nu are
    # prag) — scenariul „zero rezultate" ar afișa produse. Exact asta a prins prima rulare pe DB
    # real, și e motivul pentru care invariantul `no_results_notice_honest` verifică ABSENȚA
    # cardurilor, nu prezența unui text.
    "no_results": [("search_products", {"query": "ser cu ingredient inexistent", "price_max": 1})],
    "routine": [("search_products", {"query": "rutina de seara ten uscat"})],
    "price_of_context_product": [
        ("search_products", {"query": "ser cu vitamina C hidratant"}),
    ],
    "pipeline_error": [],
    "model_timeout": [("search_products", {"query": "ser cu vitamina C pentru ten uscat"})],
}


#: Formatul REAL pe care un tool îl trimite modelului (`ToolResult.llm_view`, vezi
#: `src/tools/catalog_tools.py::_brief`): o linie per produs,
#: `[<uuid>] <nume> | <brand> | <preț> lei | <rating>★ | stoc: … | <sumar>`.
#:
#: Fake-ul parsează EXACT asta, nu JSON. Prima versiune presupunea JSON și, pe calea reală, nu
#: extrăgea nimic: proza cădea pe mesajul de „n-am găsit" în timp ce vederea arăta trei carduri, iar
#: `compare_products` primea o listă goală de id-uri. Testul trecea — dar nu pe forma pe care
#: producția o emite. De aceea parserul e legat de formatul real și testat pe el.
_TOOL_LINE_RE = re.compile(r"^\[(?P<id>[^\]]+)\]\s*(?P<name>[^|]+?)\s*\|")
_TOOL_PRICE_RE = re.compile(r"\|\s*(?P<price>\d+(?:[.,]\d{1,2})?)\s*lei(?!\w)")


def _parse_tool_result(raw: Any) -> dict[str, Any]:
    """Rezultatul unui tool → `{"products": [...]}`, citind formatul de SÂRMĂ către model.

    Tolerează și un dict/JSON (folosit de testele unitare cu stub), dar calea normală e textul:
    dacă parserul s-ar rupe, `_compose` ar produce mesajul de zero-rezultate și s-ar vedea imediat
    în invarianți (`no_results_notice_honest` vs `min_three_product_cards`), nu tăcut.
    """
    if isinstance(raw, dict):
        return raw
    text = str(raw)
    products: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _TOOL_LINE_RE.match(line.strip())
        if m is None:
            continue
        price = _TOOL_PRICE_RE.search(line)
        products.append(
            {
                "id": m.group("id"),
                "name": m.group("name").strip(),
                "price": float(price.group("price").replace(",", ".")) if price else None,
            }
        )
    return {"products": products, "_text": text}


def _resolve_args(args: Any, prior: list[dict[str, Any]]) -> dict[str, Any]:
    """Argumentele unui pas: fie literale, fie derivate din rezultatele pașilor ANTERIORI.

    Seam-ul există pentru un singur motiv: `compare_products` cere id-uri de produs pe care doar
    căutarea le cunoaște. Un fake care le-ar hardcoda ar compara ID-uri care nu există în catalogul
    tenantului — adică ar testa respingerea, nu comparația."""
    return args(prior) if callable(args) else args


def _ids_from(results: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for product in _products_from(results):
        value = product.get("id") or product.get("product_id")
        if value:
            out.append(str(value))
    return out


def _products_from(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Produsele pe care tool-urile le-au întors DE FAPT. Singura sursă de nume/prețuri a fake-ului
    — de aici vine grounding-ul adevărat."""
    out: list[dict[str, Any]] = []
    for res in results:
        for key in ("products", "items", "_items"):
            value = res.get(key)
            if isinstance(value, list):
                out.extend(x for x in value if isinstance(x, dict))
    return out


def _names(results: list[dict[str, Any]], limit: int = 3) -> list[str]:
    names = [str(p.get("name") or p.get("title") or "") for p in _products_from(results)]
    return [n for n in names if n][:limit]


def _compose(script: str, results: list[dict[str, Any]]) -> str:
    """Proza finală. REGULA: zero cifre, cu o singură excepție motivată.

    Cardurile poartă faptele (preț, stoc, rating) ca text localizat server-side — asta e chiar
    decizia NX-240. O proză fără cifre e trivial groundabilă, deci ce rămâne de testat e exact
    stratul care contează: cardurile. Excepția e `price_of_context_product`, unde întrebarea ESTE
    despre preț: acolo cifra se ia din rezultatul tool-ului, ca validatorul să o poată confrunta.
    """
    names = _names(results)
    if script == "no_results" or (script in ("recommend", "compare", "routine") and not names):
        return (
            "Nu am găsit nimic care să se potrivească cu ce mi-ai cerut. "
            "Spune-mi altfel și caut din nou."
        )
    if script == "compare":
        return "Le-am pus față în față, ca să vezi diferențele: " + ", ".join(names) + "."
    if script == "routine":
        return "Pentru seară aș merge pe pașii aceștia, în ordine, cu " + ", ".join(names) + "."
    if script == "price_of_context_product":
        products = _products_from(results)
        if not products:
            return "Nu am reușit să văd produsul acesta acum. Spune-mi numele lui și îl caut."
        first = products[0]
        price = first.get("price")
        if price is None:
            return f"Am găsit {first.get('name')}, dar prețul nu îmi apare acum."
        return f"{first.get('name')} este {float(price):g} lei."
    return "Uite ce ți se potrivește: " + ", ".join(names) + "."


def _answer_plan_v2(script: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """`AnswerPlanV2` minimal pentru calea de creier unic. Aceleași fapte, altă formă."""
    return {
        "prose": _compose(script, results),
        "products": [
            {"product_id": p.get("id") or p.get("product_id")}
            for p in _products_from(results)[:3]
            if p.get("id") or p.get("product_id")
        ],
    }


# ── Manifest, invarianți, defecte injectabile ───────────────────────────────────────────────


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def thresholds() -> dict[str, Any]:
    return json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))


def invariant_owners() -> dict[str, str]:
    return {
        name: spec["owner"]
        for name, spec in manifest()["invariants"].items()
        if not name.startswith("_")
    }


def backend_invariants() -> frozenset[str]:
    return frozenset(n for n, owner in invariant_owners().items() if owner == "backend")


#: Defectele injectabile, vocabular ÎNCHIS. Un defect necunoscut e respins: un harness care
#: acceptă orice string de fault ar putea „injecta" nimic și raporta verde.
FAULTS: frozenset[str] = frozenset(
    {
        "none",
        "model_timeout",
        "pipeline_error",
        "db_transient_at_commit",
        "redis_dead",
        "price_changed_after_emit",
        "kill_worker_after_claim",
    }
)


@dataclass(frozen=True)
class InvariantInput:
    """Tot ce are nevoie un checker. Deliberat DOAR date: un checker care ar putea face I/O ar
    putea și să „repare" ce verifică."""

    view: dict[str, Any]
    tenant: SyntheticTenant
    probes: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)


# ── Forma vederii: `web-chat.v1` ────────────────────────────────────────────────────────────
# Envelope-ul de blocuri `web-view.v2` a fost ȘTERS din produs, deci invarianții de aici sunt
# rescriși pe contractul pe care serverul chiar îl servește: payload-ul persistat de executor
# (`render_web`) + plicul de tur. Ce s-a pierdut odată cu blocurile e DECLARAT, nu tăcut:
#   • „chips opace" — pe v1 un chip e MESAJUL pe care l-ar scrie clientul, nu un token sigilat;
#   • „sumar de coș server-owned" și „copy de shell la bootstrap" — nu au producător pe v1;
#   • „doar string-uri display-ready" — pe v1 prețul e un NUMĂR, deliberat: formatarea e a
#     frontendului. Era o proprietate a lui v2, nu un adevăr despre produs.


def _products(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in (view.get("products") or []) if isinstance(p, dict)]


#: Cheile pe care contractul v1 le pune pe sârmă. Aceeași allowlist ca `src/web/turn_view_v1.py` —
#: dacă vederea ar începe să emită altceva, gate-ul trebuie să o vadă.
_KNOWN_VIEW_KEYS = frozenset(
    {
        "schema_version",
        "conversation",
        "turn",
        "error",
        "content",
        "products",
        "suggestions",
        "comparison",
        "offer",
        "error_code",
    }
)

_COT_MARKERS = (
    "chain of thought",
    "let me think",
    "gandesc",
    "as gandi cu voce tare",
    "reasoning:",
    "thought:",
    "pasul meu de gandire",
)


def _check_terminal_view_renderable(inp: InvariantInput) -> None:
    """P6: niciun terminal mut. Aceeași regulă ca `turn_service.renderable()`, care e poarta de
    la commit — verificată aici pe ce a ieșit efectiv pe sârmă."""
    view = inp.view
    assert (
        view.get("content") or view.get("products") or view.get("comparison") or view.get("offer")
    ), "vedere terminală fără nimic randabil (P6)"


def _check_single_ledger_row(inp: InvariantInput) -> None:
    assert inp.probes.get("ledger_rows") == 1, f"rânduri de ledger: {inp.probes.get('ledger_rows')}"


def _check_single_execution(inp: InvariantInput) -> None:
    assert inp.counters.get("generations") == 1, (
        f"generări de model: {inp.counters.get('generations')} (așteptat 1)"
    )


def _check_no_product_block(inp: InvariantInput) -> None:
    assert not _products(inp.view), "răspuns textual cu carduri nesolicitate"


def _check_min_three_product_cards(inp: InvariantInput) -> None:
    items = _products(inp.view)
    assert len(items) >= 3, f"doar {len(items)} carduri (minim 3)"
    for item in items:
        assert isinstance(item.get("price"), (int, float)), "card fără preț"
        assert item.get("product_id"), "card fără identitate de catalog"


def _check_comparison_block_present(inp: InvariantInput) -> None:
    """Pe v1 tabelul e `comparison: {columns, rows}`, cu `values` aliniat 1:1 cu coloanele —
    frontendul nu are voie să ghicească alinierea."""
    table = inp.view.get("comparison")
    assert isinstance(table, dict), "comparație cerută, tabel absent"
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    assert len(columns) >= 2, f"comparație cu {len(columns)} coloane (minim 2)"
    assert rows, "comparație fără rânduri"
    for i, row in enumerate(rows):
        values = row.get("values") or []
        assert len(values) == len(columns), (
            f"rândul {i} are {len(values)} valori pentru {len(columns)} coloane"
        )


def _check_no_results_notice_honest(inp: InvariantInput) -> None:
    assert (inp.view.get("content") or "").strip(), "zero rezultate fără niciun mesaj (tăcere)"
    assert not _products(inp.view), "zero rezultate, dar cu produse afișate"


def _check_revoked_need_absent(inp: InvariantInput) -> None:
    revoked = inp.state.get("revoked") or []
    active = inp.state.get("active_needs") or []
    assert revoked, "scenariul de corecție nu a produs nicio revocare"
    overlap = sorted(set(revoked) & set(active))
    assert not overlap, f"nevoia revocată a supraviețuit: {overlap}"


def _check_context_resolved_server_side(inp: InvariantInput) -> None:
    assert inp.state.get("context_resolved") is True, (
        'referința „acesta" nu a fost rezolvată server-side din contextul rehidratat'
    )


def _check_one_receipt_per_action(inp: InvariantInput) -> None:
    assert inp.probes.get("receipts") == 1, f"receipts: {inp.probes.get('receipts')} (așteptat 1)"


def _check_no_false_commerce_success(inp: InvariantInput) -> None:
    assert inp.probes.get("cart_items", 0) == 0, "s-a scris în coș pe date stale"
    _check_terminal_view_renderable(inp)


def _check_deadline_fallback_persisted(inp: InvariantInput) -> None:
    status = (inp.view.get("turn") or {}).get("status")
    assert status in ("completed", "failed"), f"deadline fără terminal persistat (status={status})"
    _check_terminal_view_renderable(inp)


def _check_one_feedback_row(inp: InvariantInput) -> None:
    assert inp.probes.get("feedback_rows") == 1, (
        f"rânduri de feedback: {inp.probes.get('feedback_rows')} (așteptat 1)"
    )


def _check_prices_match_catalog_snapshot(inp: InvariantInput) -> None:
    snapshot = inp.tenant.price_snapshot
    for item in _products(inp.view):
        name = item.get("name") or ""
        expected = next((v for k, v in snapshot.items() if k == name), None)
        assert expected is not None, f"card cu nume care nu e în catalogul seedat: {name!r}"
        assert int(float(item.get("price") or 0)) == int(expected), (
            f"preț afișat {item.get('price')!r} ≠ snapshot {expected} pentru {name!r}"
        )


def _check_no_chain_of_thought(inp: InvariantInput) -> None:
    blob = fold(json.dumps(inp.view, ensure_ascii=False))
    for marker in _COT_MARKERS:
        assert marker not in blob, f"urmă de raționament în vedere: {marker!r}"


def _check_product_ids_from_own_tenant(inp: InvariantInput) -> None:
    own = {p.name for p in inp.tenant.products}
    for item in _products(inp.view):
        assert item.get("name") in own, (
            f"produs din alt catalog în vederea lui {inp.tenant.key}: {item.get('name')!r}"
        )


def _check_only_known_block_types(inp: InvariantInput) -> None:
    """Numele invariantului a rămas (e cheie în `scenarios.json`), dar întrebarea e cea a lui v1:
    vederea emite DOAR cheile de contract? O cheie scursă din payload-ul intern e exact clasa pe
    care allowlistul din `turn_view_v1.py` o închide."""
    unknown = sorted(set(inp.view) - _KNOWN_VIEW_KEYS)
    assert not unknown, f"chei necunoscute pe sârmă: {unknown}"


#: Registrul de checkere. Cheia e invariantul din manifest; testul de acoperire cere ca fiecare
#: invariant `owner=backend` să apară AICI și fiecare intrare de aici să existe în manifest.
INVARIANT_CHECKS: dict[str, Callable[[InvariantInput], None]] = {
    "terminal_view_renderable": _check_terminal_view_renderable,
    "single_ledger_row": _check_single_ledger_row,
    "single_execution": _check_single_execution,
    "no_product_block": _check_no_product_block,
    "min_three_product_cards": _check_min_three_product_cards,
    "comparison_block_present": _check_comparison_block_present,
    "no_results_notice_honest": _check_no_results_notice_honest,
    "revoked_need_absent": _check_revoked_need_absent,
    "context_resolved_server_side": _check_context_resolved_server_side,
    "one_receipt_per_action": _check_one_receipt_per_action,
    "no_false_commerce_success": _check_no_false_commerce_success,
    "deadline_fallback_persisted": _check_deadline_fallback_persisted,
    "one_feedback_row": _check_one_feedback_row,
    "prices_match_catalog_snapshot": _check_prices_match_catalog_snapshot,
    "no_chain_of_thought": _check_no_chain_of_thought,
    "product_ids_from_own_tenant": _check_product_ids_from_own_tenant,
    "only_known_block_types": _check_only_known_block_types,
}


def check_invariants(names: list[str], inp: InvariantInput) -> list[str]:
    """Rulează checkerele BACKEND din `names` și întoarce lista celor rulate. Invarianții
    `owner=frontend` se sar aici (îi dovedește PR B), dar NU tăcut: apelantul primește lista și
    poate asserta acoperirea."""
    ran: list[str] = []
    for name in names:
        check = INVARIANT_CHECKS.get(name)
        if check is None:
            continue
        check(inp)
        ran.append(name)
    return ran
