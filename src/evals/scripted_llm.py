"""LLM-ul SCRIPTAT al harnessului golden — o singură definiție, două consumatoare.

## De ce trăiește în `src/` și nu în teste

`tests/test_golden.py` (gate-ul CI) și `scripts/eval_regression.py` (snapshot + diff) rulează
ACELEAȘI cazuri prin ACELAȘI pipeline. Până acum fiecare își avea copia lui de LLM scriptat,
cu motivul declarat în antetul scriptului: un script din `scripts/` nu trebuie să depindă de un
modul de test. Motivul e corect; concluzia nu era — a treia opțiune e ca definiția să stea în
`src/`, unde amândouă au voie să o importe.

Copia a și divergat, exact cum divergă copiile: cea din teste a învățat calea creierului unic
(`run_tool_loop_structured` + `complete_schema`), cea din script nu. Consecința nu era un test
roșu, ci una mai rea — cu `SINGLE_BRAIN_ENABLED=true` în producție, harnessul de regresie ar fi
continuat să măsoare calea v1, adică singurul instrument care compară două rulări s-ar fi uitat
în altă parte decât rulează produsul. Un `AttributeError` pe metoda lipsă e înghițit de
`_generate_plan` (`src/agent/brain.py`) și devine fallback determinist, deci toate cazurile ar fi
picat cu același mesaj generic, iar diff-ul ar fi arătat „totul s-a schimbat".

## Ce e scriptat și ce nu

Metodele sunt exact cele pe care le cheamă pipeline-ul: `moderate` (gates), `embed` (cache +
tool-uri), `classify_json` (triaj), `complete` (retry de validator), `run_tool_loop` (v1) și
`run_tool_loop_structured` + `complete_schema` (creierul unic). Fixturile sunt ale cazului, nu
ale acestui modul: aici nu se inventează niciun text.
"""

from __future__ import annotations

from src.agent.llm import ModerationResult

#: Linia din promptul MainBrain care enumeră obligațiile turului. Citită, nu re-derivată — vezi
#: `obligations_from_prompt`.
_OBLIGATIONS_LINE = "Obligațiile turului (acoperă-le pe TOATE în plan): "

#: Cazurile care NU pot fi rulate pe calea `brain` din fixturile v1. Limită de HARNESS, nu de
#: produs: nano returna `reply: null`, iar textul îl compunea codul v1, deci nu există ce deriva
#: — iar a-l inventa noi ar însemna să testăm ce am scris tot noi. Se deblochează cu un `plan_v2`
#: explicit în fixtură.
#:
#: Lista e DECLARATĂ, nu tăcută: gate-ul o marchează `xfail(strict=True)`, deci o reparație se
#: raportează ca XPASS în loc să treacă neobservată. Așa a scăzut de la 20 la 1.
BRAIN_NO_SCRIPTED_TEXT: frozenset[str] = frozenset({"clarify-low-confidence-sales"})


def brain_gap(case_id: str) -> str | None:
    """Motivul pentru care cazul nu poate rula pe calea `brain`, sau None dacă poate."""
    if case_id in BRAIN_NO_SCRIPTED_TEXT:
        return "fixtura n-are text scriptat pentru calea brain (cere `plan_v2` explicit)"
    return None


def obligations_from_prompt(user: str) -> list[tuple[str, str]]:
    """Obligațiile turului, citite din promptul pe care brain-ul le-a scris el însuși.

    Nu le re-derivăm cu extractorul: dacă harnessul ar chema aceeași funcție ca produsul, un bug
    în extractor ar fi invizibil — testul și codul ar greși identic. Aici citim exact ce a ajuns
    în fața modelului, adică ce ar citi și un model real."""
    for line in user.splitlines():
        if line.startswith(_OBLIGATIONS_LINE):
            raw = line[len(_OBLIGATIONS_LINE) :]
            out = []
            for chunk in raw.split(";"):
                kind, _, key = chunk.strip().partition(":")
                if kind and key:
                    out.append((kind, key))
            return out
    return []


def derive_plan(fx: dict, *, user: str, business_id: str, locale: str) -> dict:
    """Planul pe care l-ar emite un model COMPETENT pentru fixtura asta — nu unul PERFECT.

    Regula care ține testul onest: `direct_answer` e EXACT textul scriptat al cazului. Nu-l
    rescriem, nu-i atașăm evidence pentru cifre pe care catalogul nu le are. Deci un caz cu preț
    inventat rămâne cu preț inventat, iar validatorul V2 trebuie să-l respingă exact cum îl
    respingea validatorul v1 — altfel migrarea pe creierul unic ar „repara" fix cazurile negative
    pe care golden-ul există ca să le prindă."""
    # Catalogul fixturii NU e retrieval: produsele intră în plan doar dacă turul chiar a căutat.
    # Altfel planul ar cita produse pe care serverul nu le-a văzut în turul ăsta —
    # `unknown_product`, și pe bună dreptate: exact asta previne validatorul.
    searched = any(name == "search_products" for name, _ in fx.get("tool_calls", []))
    products = list(fx.get("catalog", []))[:6] if searched else []
    obligations = obligations_from_prompt(user)
    kinds = {kind for kind, _ in obligations}
    # NX-297: fixtura `triage` a dispărut odată cu stagiul. Textul scriptat al unui tur trăiește
    # într-un singur loc (`final`) pe AMBELE contracte — cât timp existau două, un caz putea trece
    # pe v1 din fixtura triajului și pe brain din `final`, adică din surse diferite.
    answer = fx.get("final") or ""
    # O clarificare rămâne exprimabilă, dar DECLARAT, nu dedusă din ruta unui clasificator care nu
    # mai există: cazul care vrea să testeze planul de clarificare o scrie explicit.
    clarification = fx.get("clarification")

    def ev(product: dict, kind: str) -> str:
        return f"product:{product['id']}:{kind}"

    selected = [
        {"product_id": p["id"], "variant_id": None, "evidence_ids": [ev(p, "identity")]}
        for p in products
    ]
    recommendations = [
        {
            "product_id": p["id"],
            "variant_id": None,
            "reason": "potrivit pentru ce ai cerut",
            "evidence_ids": [ev(p, "identity")],
            "need_ids": [],
        }
        for p in products
    ]
    comparison = None
    if "compare" in kinds and len(products) >= 2:
        pair = products[:4]
        comparison = {
            "product_ids": [p["id"] for p in pair],
            "axes": ["price"],
            "cells": [
                {
                    "product_id": p["id"],
                    "axis": "price",
                    "value": p.get("price"),
                    "evidence_id": ev(p, "price"),
                }
                for p in pair
                if p.get("price") is not None
            ],
        }
    # Fără produse, o obligație de recomandare/comparație NU poate fi acoperită onest: `no_match`
    # e clasa corectă, iar `insufficient_data` ar fi minciuna pe care D7 o interzice (UNKNOWN ≠
    # MISMATCH). Cazurile care testează chiar taxonomia asta își scriu `plan_v2` explicit.
    no_results = None
    if not products and kinds & {"recommend", "compare"}:
        no_results = {"reason_class": "no_match", "criteria": [], "alternatives": []}

    return {
        "schema_version": 2,
        "business_id": business_id,
        "locale": locale,
        "intent_summary": "caz golden",
        "obligations": [{"kind": k, "key": key} for k, key in obligations][:8],
        "direct_answer": answer,
        "selected_products": selected,
        "claims": [],
        "facts": {"prices": [], "stocks": [], "urls": []},
        "recommendations": recommendations if "recommend" in kinds else [],
        "comparison": comparison,
        "constraints_applied": [],
        "unknowns": [],
        "relaxations": [],
        "clarification": clarification,
        "no_results": no_results,
        "state_update_proposals": [],
        "action_intents": [],
        "disclosures": [],
        "confirmed_actions": [],
        "style_signals": {"tone": "neutral", "verbosity": "short"},
    }


class ScriptedLLM:
    """LLM determinist scriptat din `fixtures`. Implementează exact metodele chemate de pipeline.

    `fx` poate fi un dict (caz single-tur) sau un getter fără argumente care întoarce fixtures-ul
    turului CURENT (caz multi-tur — comutat de `on_turn`)."""

    def __init__(self, fx, *, business_id: str = "biz-golden", locale: str = "ro") -> None:
        self._get = fx if callable(fx) else (lambda: fx)
        self._business_id = business_id
        self._locale = locale

    @property
    def _fx(self) -> dict:
        return self._get()

    async def moderate(self, text, *, model=None):
        m = self._fx.get("moderation", {})
        return ModerationResult(
            flagged=bool(m.get("flagged")), categories=list(m.get("categories", []))
        )

    async def embed(self, texts, *, model=None):
        # vectori determiniști (zerouri) — search/cache-ul real e oricum stubat.
        return [[0.0] * 8 for _ in texts]

    async def classify_json(self, system, user, *, model=None):
        # NX-297: nimic din pipeline nu mai clasifică. Singurul consumator de
        # `classify_json` e extracția de fundal (POST-tur), care nu rulează în golden.
        return dict(self._fx.get("classify", {}))

    async def complete(self, system, user, *, model=None):
        # textul de la validator-retry (poate fi tot invalid → fallback determinist).
        return self._fx.get("retry", "")

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None, **kw):
        calls = self._fx.get("tool_calls", [])
        for name, args in calls:
            await execute(name, args)
        # NX-312: toate apelurile scriptate = O rundă. Dacă predicatul agentului o declară
        # suficientă, bucla REALĂ n-ar mai cere proza, deci nici modelul scriptat n-o întoarce:
        # altfel golden-ul ar măsura un text pe care producția nu-l mai produce.
        stop = kw.get("stop_after_tools")
        if calls and stop is not None and stop([name for name, _ in calls]):
            return ""
        return self._fx.get("final", "")

    # --- creierul unic (NX-239): ACELEAȘI fixturi, alt contract de ieșire -----

    async def run_tool_loop_structured(self, system, user, tools, execute, schema, **kw):
        """Bucla structurată: aceleași tool calls, dar ieșirea e un `AnswerPlanV2`.

        Planul vine din `fx["plan_v2"]` dacă e scris explicit (cazuri care testează planuri
        anume — invalide, parțiale), altfel e DERIVAT din aceleași fixturi ca pe calea v1."""
        for name, args in self._fx.get("tool_calls", []):
            await execute(name, args)
        rounds = 1 if self._fx.get("tool_calls") else 0
        return self._plan(user), rounds

    async def complete_schema(self, system, user, schema, **kw):
        """Repair-ul bounded. `plan_repair` scris explicit = cazul testează repararea; altfel
        repetăm ACELAȘI plan — un model care nu știe să repare, deci turul trebuie să cadă în
        fallback determinist. A întoarce aici un plan „reparat" din senin ar face repair-ul să
        pară că funcționează în cazuri în care n-a fost niciodată exercitat."""
        repair = self._fx.get("plan_repair")
        return dict(repair) if repair else self._plan(user)

    def _plan(self, user: str) -> dict:
        fx = self._fx
        explicit = fx.get("plan_v2")
        if explicit:
            return dict(explicit)
        return derive_plan(fx, user=user, business_id=self._business_id, locale=self._locale)


__all__: list[str] = [
    "BRAIN_NO_SCRIPTED_TEXT",
    "ScriptedLLM",
    "brain_gap",
    "derive_plan",
    "obligations_from_prompt",
]


def _assert_scripted_llm_covers_client() -> None:
    """Poartă de IMPORT: metoda pe care o cheamă pipeline-ul dar scriptul n-o are.

    Exact defectul pe care îl repară fișierul ăsta: copia din `scripts/` n-avea
    `run_tool_loop_structured`, iar lipsa nu se vedea ca eroare — `_generate_plan` înghite
    `AttributeError` și cade pe fallback determinist, deci un harness orb raportează cazuri
    roșii, nu un instrument stricat. Comparăm cu clientul REAL, ca adăugarea unei metode noi
    în adaptor să oprească procesul aici, nu să producă o măsurătoare tăcut greșită."""
    from src.agent.llm import LLMClient  # noqa: PLC0415 — evită ciclul la import

    required = {
        "classify_json",
        "complete",
        "complete_schema",
        "embed",
        "moderate",
        "run_tool_loop",
        "run_tool_loop_structured",
    }
    missing = {
        name
        for name in required
        if hasattr(LLMClient, name) and not hasattr(ScriptedLLM, name)  # pragma: no cover
    }
    if missing:
        raise ValueError(
            f"ScriptedLLM nu acoperă metode ale LLMClient: {sorted(missing)} — harnessul ar "
            "măsura fallback-ul, nu calea reală"
        )


_assert_scripted_llm_covers_client()
