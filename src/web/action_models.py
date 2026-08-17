"""NX-236 — acțiunile web ca CONTRACT ÎNCHIS: registry finit, argumente mărginite, envelope opac.

Până acum un „chip" era o etichetă: frontendul o afișa, iar la apăsare o retrimitea ca MESAJ, iar
backendul reconstruia intenția ghicind din text (`render.py:178` arunca `Chip.payload`, deci nici
n-avea altceva la îndemână). Consecința nu e estetică: eticheta devine input, iar cine poate scrie
eticheta poate scrie intenția. Un buton „Vezi recenzii" editat în DevTools ajungea o propoziție
arbitrară în pipeline, iar semantica butonului trăia în două locuri (copy-ul serverului și parserul
de mesaje) care nu se puteau valida reciproc.

Aici semantica e a serverului, o singură dată. Un control are DOUĂ fețe, separate prin tip:

  • `ActionView` (NX-228) — ce vede omul: `label`, `appearance`, `icon`, `activation`. Zero sens.
  • `ActionEnvelope` — ce înțelege mașina: kind allowlisted + argumente CANONICE + legături
    (tenant, sesiune, conversație, turul-sursă, revizie) + expirare. Sigilat (`action_crypto`),
    deci frontendul nu-l poate citi, compune sau completa.

**Trei reguli pe care le impune tipul, nu disciplina.**

1. **Vocabular ÎNCHIS.** `KIND_REGISTRY` e finit și fiecare intrare declară dacă e mutantă, dacă e
   `one_shot`, ce argumente acceptă și dacă poate fi EMISĂ în Stage 1. Un kind necunoscut nu e
   „ignorat best-effort", e respins înainte de dispatch. Nu există `execute_tool`, `run_prompt`,
   rută brută sau cod: numele tool-urilor și numele acțiunilor sunt registre DISJUNCTE
   (`assert_registry_disjoint`, verificat la import ȘI în teste) — un token nu poate numi
   niciodată un tool. De asta două kind-uri au alt nume decât în card:
   `compare_products` → `compare_selection`
   și `cart_add` → `cart_add_line`, fiindcă primele DOUĂ nume erau deja tool-uri ale modelului.
   Numele nu ajunge niciodată la client (e sub sigiliu), deci costul e zero, iar invariantul
   devine mecanic în loc de promis într-un comentariu.

2. **Argumente ca REFERINȚE, nu ca fapte.** Un argument e un id opac, un ordinal mărginit sau o
   valoare dintr-un enum. Niciodată preț, nume, URL, prompt liber sau text de client. Ce nu se
   normalizează curat nu intră: `ActionArgs.parse` întoarce `None`, iar apelantul respinge.

3. **Ce n-are handler sigur NU se emite.** `refine_search` rămâne în registry ca nume STABIL
   (metrica trebuie să poată număra o încercare) cu `available=False`: nu îl emite nimeni, iar
   consumarea lui e refuzată onest. Comerțul a trecut prin ambele porți — NX-237 i-a dat handler
   (`CartService` + receipt idempotent), NX-240 i-a dat condiția de emitere (fapte VERIFICATE de
   stoc și preț, prin `TurnFacts.commerce_product_refs`). Un buton care pretinde că a mutat ceva
   fără dovadă e mai rău decât un buton absent — de asta emiterea depinde de fapte, nu de flag.

Modulul e PUR: fără DB, fără LLM, fără ceas, fără random. Sigilarea e în `action_crypto`,
autorizarea în `action_service`, execuția în `src/agent/action_kernel.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

# ── Versiune + audiență ─────────────────────────────────────────────────────────────────────
# Versiunea e în TOKEN, nu într-un câmp opțional: un verificator care nu o cunoaște refuză, nu
# „interpretează ce înțelege". Audiența leagă tokenul de suprafața pentru care a fost emis —
# același material criptografic folosit altundeva (ex. un canal viitor) nu produce token valid.
ACTION_ENVELOPE_VERSION = "a1"
ACTION_AUDIENCE = "web-widget-v2"

# ── Caps (P4 — bugetul e impus în cod) ──────────────────────────────────────────────────────
MAX_REF_LEN = 64  # un id opac de produs (uuid canonic sau external_id de platformă)
MAX_REFS = 3  # comparație: 2-3 coloane (aceeași limită ca `MAX_COMPARISON_COLUMNS`)
MAX_QUESTION_ID_LEN = 64
MAX_OPTION_REF = 7  # opțiunile unei clarificări; peste asta nu mai e o alegere, e o listă
MAX_QUANTITY = 10  # aliniat cu plafonul de linii al coșului (NX-237 îl va impune la scriere)
MAX_ACTIONS_PER_TURN = 16  # câte acțiuni poate purta un singur ViewModel terminal
MAX_ENVELOPE_BYTES = 1024  # claims canonice; peste asta e un token construit, nu unul emis

# Enumul de rafinare. ÎNCHIS deliberat: un „filtru" liber ar fi un prompt cu alt nume.
REFINE_FILTERS: frozenset[str] = frozenset({"cheaper", "in_stock", "top_rated"})

ActionPolicy = Literal["one_shot", "repeatable"]
ActionKind = Literal[
    "select_product",
    "request_details",
    "request_reviews",
    "compare_selection",
    "show_more",
    "answer_clarification",
    "refine_search",
    "cart_add_line",
    "cart_set_quantity",
    "cart_remove",
    "cart_clear",
    "checkout",
]

# Codurile de eroare — vocabular ÎNCHIS, low-cardinality (intră în metrici și în `ErrorView.code`;
# P10/P12: niciodată tokenul, argumentele sau id-urile în etichete).
ACTION_ERROR_CODES: frozenset[str] = frozenset(
    {
        "action_disabled",  # feature stins (kill-switch) — ruta refuză onest
        "action_invalid",  # crypto/format/kind/args: respingere GENERICĂ, fără detalii
        "action_expired",  # TTL depășit (ancorat în terminalul sursă), retry cu acțiune nouă
        "action_not_found",  # turul-sursă lipsește / nu e al acestei sesiuni / n-a emis acțiunea
        "action_already_consumed",  # one-shot deja folosit de ALT turn
        "action_stale",  # revizia sursei nu mai descrie ce vede clientul
        "action_unavailable",  # kind cunoscut, fără handler sigur încă (comerț → NX-237)
    }
)


#: Unde se CONSUMĂ o acțiune. Structural, nu un `if` special-case: un token de feedback prezentat
#: la `/web/v2/turns` ar porni un tur conversațional (și ar arde slotul de single-flight) pentru un
#: click de „👍". Ruta compară `spec.sink` cu propriul ei sink și refuză restul — deci o acțiune
#: nouă nu poate ajunge din greșeală pe drumul greșit, iar testul o prinde la adăugare.
SINK_TURN = "turn"
SINK_FEEDBACK = "feedback"
ACTION_SINKS: frozenset[str] = frozenset({SINK_TURN, SINK_FEEDBACK})


@dataclass(frozen=True)
class ActionSpec:
    """O intrare din registry. `available=False` = nume cunoscut, dar niciodată emis și refuzat
    la consum — diferența dintre „nu știu ce e asta" și „știu, dar nu am cum să o onorez încă"."""

    kind: str
    mutating: bool = False
    policy: ActionPolicy = "one_shot"
    available: bool = True
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    #: Ruta care are voie să o consume (vezi `SINK_*`).
    sink: str = SINK_TURN
    # Depinde de STAREA listei/sesiunii, nu doar de id-uri explicite: o acțiune care poartă
    # `product_ref` nu poate selecta produsul greșit după o reordonare, dar una care spune „încă
    # 6 din căutarea curentă" da — de aceea doar a doua se verifică pe revizie.
    stale_sensitive: bool = False


# Registry Stage 1. Ordinea e cea din card; `available` e decizia „handler sigur sau nimic".
KIND_REGISTRY: Mapping[str, ActionSpec] = {
    "select_product": ActionSpec("select_product", required=("product_ref",)),
    "request_details": ActionSpec("request_details", required=("product_ref",)),
    "request_reviews": ActionSpec("request_reviews", required=("product_ref",)),
    "compare_selection": ActionSpec("compare_selection", required=("product_refs",)),
    # `session_ref` = amprenta sesiunii de căutare (`active_search.fp`). E OBLIGATORIE fiindcă
    # paginarea e singura acțiune care nu numește ce servește: fără ea, un buton emis peste
    # căutarea de acum trei tururi ar pagina tăcut ALTĂ listă (failure matrix: „stale ⇒ eroare
    # explicită, nu primul produs").
    "show_more": ActionSpec("show_more", required=("session_ref",), stale_sensitive=True),
    "answer_clarification": ActionSpec(
        "answer_clarification", required=("question_id", "option_ref"), stale_sensitive=True
    ),
    # Rezervat: numele rămâne stabil pentru metrici, dar nu există încă o rafinare DETERMINISTĂ
    # server-side care să nu fie, de fapt, un prompt (`cheaper` trăiește azi în planner, legat de
    # textul turului). Nimic nu îl emite ⇒ nimeni nu poate purta un token cu el.
    "refine_search": ActionSpec(
        "refine_search", available=False, required=("filter",), stale_sensitive=True
    ),
    # Comerț: MUTANT. NX-237 le-a dat handler REAL — `CartService` cu receipt idempotent per
    # `action_id` (o singură creștere chiar și la double-click / race pe one-shot). `available`
    # e proprietatea STRUCTURALĂ „există handler sigur"; poarta de RUNTIME rămâne dublă:
    # autorizarea și kernelul refuză onest când `CONVERSATION_CART_ENABLED` e stins. Nimic nu le
    # EMITE încă (plan_actions nu produce planuri de comerț — CTA-urile de coș sunt NX-240).
    "cart_add_line": ActionSpec(
        "cart_add_line",
        mutating=True,
        required=("product_ref",),
        optional=("quantity",),
    ),
    "cart_set_quantity": ActionSpec(
        "cart_set_quantity",
        mutating=True,
        required=("product_ref", "quantity"),
    ),
    "cart_remove": ActionSpec("cart_remove", mutating=True, required=("product_ref",)),
    "cart_clear": ActionSpec("cart_clear", mutating=True),
    "checkout": ActionSpec("checkout", mutating=True),
    # NX-246 felia 2 — feedback. RATINGUL E ÎN KIND, deliberat: astfel el vine din tokenul
    # SIGILAT de server, iar browserul nu are cum să-l rostească (cardul: „ratingul/reasonul vin
    # exclusiv din tokenul serverului"). `reason` e opțional și dintr-o taxonomie ÎNCHISĂ,
    # versionată — al doilea pas, oferit doar după un vot negativ.
    #
    # `repeatable`, nu `one_shot`: one-shot-ul NX-236 e o proprietate a LEDGERULUI (cheia de
    # consum e rândul turului care folosește acțiunea), iar feedbackul nu creează tur. Unicitatea
    # lui trăiește în `web_feedback` (un singur rând activ per prompt) — vezi `src/web/feedback.py`.
    "feedback_up": ActionSpec("feedback_up", policy="repeatable", sink=SINK_FEEDBACK),
    "feedback_down": ActionSpec(
        "feedback_down", policy="repeatable", optional=("reason",), sink=SINK_FEEDBACK
    ),
}

#: Taxonomia de motive, VERSIONATĂ. Se schimbă doar cu versiune nouă: un `reason_code` reinterpretat
#: în tăcere ar face ca două ferestre de raport să nu mai fie comparabile, exact în momentul în care
#: cineva compară champion cu candidate.
FEEDBACK_TAXONOMY_VERSION = "feedback.v1"
FEEDBACK_REASONS: frozenset[str] = frozenset(
    {
        "not_relevant",  # nu e ce căutam        → familia „retrieval"
        "wrong_facts",  # informația e greșită   → familia „grounding"
        "missing_info",  # lipsește ceva         → familia „clarification"
        "too_long",  # prea mult text            → familia „overtalk"
        "tone",  # nu sună a om                  → familia „tone"
        "other",
    }
)

#: Kind-urile de feedback, derivate din registry (nu a doua listă întreținută manual).
FEEDBACK_KINDS: frozenset[str] = frozenset(
    k for k, s in KIND_REGISTRY.items() if s.sink == SINK_FEEDBACK
)

#: `kind → rating` persistat. Vocabular închis, ca eticheta de metrică.
RATING_BY_KIND: Mapping[str, str] = {"feedback_up": "positive", "feedback_down": "negative"}

# Mutantele pe care NX-240 le EMITE, o dată ce faptele o permit. Lista e explicită (nu „toate
# mutantele"): `cart_add_line` cere un produs dovedit vandabil, `checkout` cere un coș eligibil.
# `cart_set_quantity`/`cart_remove`/`cart_clear` au handler la consum, dar nu au încă un loc în
# ViewModel care să le dea sens (cere controale de linie în card — NX-244), deci rămân neemise.
COMMERCE_EMITTABLE_KINDS: frozenset[str] = frozenset({"cart_add_line", "checkout"})

# Kind-urile pe care Stage 1 le poate EMITE. Derivat, nu a doua listă întreținută manual.
# NX-240: comerțul intră aici, dar EMITEREA rămâne condiționată de fapte — `plan_actions` nu
# planifică un `cart_add_line` decât pentru refs pe care guardul le-a declarat vandabile, iar
# `commerce_product_refs` e gol cât timp `CONVERSATION_CART_ENABLED` e stins. Poarta e dublă:
# fără plan persistat nu există token, iar `authorize_action` refuză oricum mutantele cu serviciul
# stins (`action_unavailable`) — o rotire de flag nu poate reînvia butoane emise ieri.
EMITTABLE_KINDS: frozenset[str] = frozenset(
    k
    for k, s in KIND_REGISTRY.items()
    if s.available and (not s.mutating or k in COMMERCE_EMITTABLE_KINDS)
)


def spec_for(kind: object) -> ActionSpec | None:
    """Specul unui kind, sau None. Nicio ramură din cod nu are voie să lucreze pe un `kind` care
    n-a trecut pe aici — de asta funcția e singura poartă de intrare în registry."""
    return KIND_REGISTRY.get(kind) if isinstance(kind, str) else None


def assert_registry_disjoint(tool_names: Sequence[str]) -> None:
    """Registrul de acțiuni și cel de tool-uri sunt DISJUNCTE (verificat în teste + la import).

    Nu e paranoia: dacă un `kind` s-ar putea numi ca un tool, dispatch-ul „prin nume" ar deveni o
    tentație, iar de acolo un token ar putea executa un tool. Separarea e structurală — kernelul
    nu primește niciodată un nume, ci un `ActionSpec` deja rezolvat."""
    overlap = sorted(set(KIND_REGISTRY) & {str(t) for t in tool_names})
    if overlap:
        raise ValueError(
            f"kind de acțiune cu nume de tool: {overlap} (registrele trebuie disjuncte)"
        )


# ── Argumente canonice ──────────────────────────────────────────────────────────────────────
def _ref(value: object) -> str | None:
    """Un id opac mărginit. Fără spații, fără ghilimele, fără URL — `web.context.normalize_id`
    are aceeași regulă pentru contextul de pagină, din același motiv: un id cu spații nu e id."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_REF_LEN:
        return None
    if any(ch.isspace() for ch in text):
        return None
    if not all(ch.isalnum() or ch in "_.:-" for ch in text):
        return None
    return text


def _int_in(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


@dataclass(frozen=True)
class ActionArgs:
    """Argumentele unei acțiuni: REFERINȚE și enum-uri, niciodată fapte sau text liber.

    Forma canonică (`to_canonical`) e cea care intră în `action_id` și în sigiliu: aceleași
    argumente ⇒ aceiași bytes ⇒ același id, indiferent de ordinea cheilor sau de cine le
    construiește. Asta face dovada de emitere re-derivabilă (vezi `action_service`)."""

    product_ref: str | None = None
    product_refs: tuple[str, ...] = ()
    question_id: str | None = None
    option_ref: int | None = None
    session_ref: str | None = None
    filter: str | None = None
    quantity: int | None = None
    #: NX-246: motivul unui vot negativ, din `FEEDBACK_REASONS`. Absent ⇒ cheia nu apare în forma
    #: canonică, deci `action_id`-urile emise ÎNAINTE de card rămân byte-identice.
    reason: str | None = None

    def to_canonical(self) -> dict[str, Any]:
        """Doar cheile SETATE, sortate la serializare. Un `None` scris explicit ar schimba
        bytes-ul fără să schimbe sensul — adică ar rupe re-derivarea id-ului."""
        out: dict[str, Any] = {}
        if self.product_ref:
            out["product_ref"] = self.product_ref
        if self.product_refs:
            out["product_refs"] = list(self.product_refs)
        if self.question_id:
            out["question_id"] = self.question_id
        if self.option_ref is not None:
            out["option_ref"] = self.option_ref
        if self.session_ref:
            out["session_ref"] = self.session_ref
        if self.filter:
            out["filter"] = self.filter
        if self.quantity is not None:
            out["quantity"] = self.quantity
        if self.reason:
            out["reason"] = self.reason
        return out

    @property
    def present(self) -> frozenset[str]:
        return frozenset(self.to_canonical())

    @classmethod
    def parse(cls, raw: object, spec: ActionSpec) -> ActionArgs | None:
        """`raw` (dict din envelope sau din planul persistat) → argumente validate, sau None.

        STRICT în ambele sensuri: o cheie în plus (chiar validă pentru alt kind) e respingere, la
        fel ca una lipsă. Un argument permis dar malformat NU se ignoră tăcut — ar transforma
        „compară A și B" într-o comparație cu un singur produs."""
        if not isinstance(raw, Mapping):
            return None
        allowed = set(spec.required) | set(spec.optional)
        if set(raw) - allowed:
            return None
        product_ref = _ref(raw.get("product_ref")) if "product_ref" in raw else None
        if "product_ref" in raw and product_ref is None:
            return None
        refs: tuple[str, ...] = ()
        if "product_refs" in raw:
            values = raw.get("product_refs")
            if not isinstance(values, (list, tuple)) or not 2 <= len(values) <= MAX_REFS:
                return None
            parsed = [_ref(v) for v in values]
            if any(p is None for p in parsed) or len(set(parsed)) != len(parsed):
                return None
            refs = tuple(p for p in parsed if p is not None)
        question_id = None
        if "question_id" in raw:
            question_id = _ref(raw.get("question_id"))
            if question_id is None or len(question_id) > MAX_QUESTION_ID_LEN:
                return None
        option_ref = None
        if "option_ref" in raw:
            option_ref = _int_in(raw.get("option_ref"), 0, MAX_OPTION_REF)
            if option_ref is None:
                return None
        session_ref = None
        if "session_ref" in raw:
            session_ref = _ref(raw.get("session_ref"))
            if session_ref is None:
                return None
        filter_value = None
        if "filter" in raw:
            filter_value = raw.get("filter")
            if filter_value not in REFINE_FILTERS:
                return None
        quantity = None
        if "quantity" in raw:
            quantity = _int_in(raw.get("quantity"), 1, MAX_QUANTITY)
            if quantity is None:
                return None
        reason = None
        if "reason" in raw:
            reason = raw.get("reason")
            # Vocabular ÎNCHIS, ca `filter`: un motiv necunoscut e respingere, nu „other" tăcut.
            # Altfel taxonomia ar crește din date de client, iar raportul ar număra categorii pe
            # care nimeni nu le-a definit.
            if reason not in FEEDBACK_REASONS:
                return None
        args = cls(
            product_ref=product_ref,
            product_refs=refs,
            question_id=question_id,
            option_ref=option_ref,
            session_ref=session_ref,
            filter=filter_value,
            quantity=quantity,
            reason=reason,
        )
        if not set(spec.required) <= args.present:
            return None
        return args


# ── Planul de acțiuni (ce se PERSISTĂ în terminalul turului-sursă) ──────────────────────────
@dataclass(frozen=True)
class ActionPlan:
    """O acțiune PROPUSĂ de tur, fără criptografie: `{kind, args}` și atât.

    Planul e ce se persistă în `web_turns.response_json["actions"]`, în tranzacția terminală —
    adică DOVADA de emitere. Tokenul nu se persistă: e derivat DETERMINIST din plan + rândul
    ledgerului la fiecare proiecție, deci un GET repetat produce aceiași bytes, iar un token care
    nu se re-derivă din planul persistat nu a fost emis niciodată."""

    kind: str
    args: ActionArgs = field(default_factory=ActionArgs)

    def to_jsonb(self) -> dict[str, Any]:
        return {"kind": self.kind, "args": self.args.to_canonical()}

    @classmethod
    def from_jsonb(cls, raw: object) -> ActionPlan | None:
        if not isinstance(raw, Mapping):
            return None
        spec = spec_for(raw.get("kind"))
        if spec is None:
            return None
        args = ActionArgs.parse(raw.get("args") or {}, spec)
        return cls(spec.kind, args) if args is not None else None


def parse_plans(raw: object) -> tuple[ActionPlan, ...]:
    """Lista de planuri dintr-un payload persistat, DEFENSIV: o intrare stricată se ignoră, nu
    rupe proiecția (P6 — mai bine un buton lipsă decât un ViewModel care nu se randează)."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[ActionPlan] = []
    seen: set[str] = set()
    for item in raw[:MAX_ACTIONS_PER_TURN]:
        plan = ActionPlan.from_jsonb(item)
        if plan is None:
            continue
        marker = canonical_json(plan.to_jsonb())
        if marker in seen:
            continue  # planuri identice ⇒ același `action_id` ⇒ două butoane cu același sens
        seen.add(marker)
        out.append(plan)
    return tuple(out)


# ── Planificarea (ce acțiuni are SENS să emită un turn) ─────────────────────────────────────
# Câte carduri primesc butoane și câte opțiuni de clarificare devin acțiuni. Aceleași capuri ca
# în contractul v2 (`MAX_PRODUCT_ITEMS`, `MAX_ACTIONS_PER_ROW`) — un widget nu e un panou de bord.
MAX_PLANNED_PRODUCTS = 6
MAX_PLANNED_OPTIONS = 4


@dataclass(frozen=True)
class TurnFacts:
    """Ce știe TURUL și nu se poate deduce din ViewModel-ul randat.

    Două fapte, amândouă necesare ca o acțiune să fie verificabilă mai târziu: întrebarea în
    așteptare (ca răspunsul la ea să se lege de ÎNTREBAREA exactă, nu de numele slotului) și
    amprenta sesiunii de căutare (ca paginarea să nu poată sări pe altă listă)."""

    pending_field: str | None = None
    pending_attempts: int = 1
    active_search_ref: str | None = None
    # NX-240: refs pe care GUARDUL le-a declarat vandabile (stoc + preț VERIFICATE, produs
    # neblocat, serviciu de coș pornit). Nu e „ce s-a afișat": un card poate fi vizibil fără să
    # merite un buton de cumpărare, iar diferența e exact ce ține o promisiune onestă.
    commerce_product_refs: tuple[str, ...] = ()
    #: Coșul canonic e eligibil de checkout (total cunoscut, nicio linie blocată).
    cart_checkout_ready: bool = False
    #: NX-246: turul are voie să ceară feedback. Un FAPT, nu o citire de settings — `plan_actions`
    #: rămâne pură (P10), iar cohortul/flagul se decid la margine, unde se știe contextul. Fals ⇒
    #: niciun plan de feedback ⇒ niciun token ⇒ endpointul n-are ce autoriza.
    feedback_prompt: bool = False


def clarification_question_id(field: object, attempts: int) -> str | None:
    """Id-ul STABIL al unei întrebări de clarificare: `q:<slot>:<a-câta-oară>`.

    Derivat, nu stocat: se recalculează identic la emitere (din `reply.pending_question`) și la
    consum (din starea curentă). Dacă între timp s-a pus ALTĂ întrebare — alt slot sau altă
    încercare — id-urile nu se mai potrivesc, iar acțiunea e `stale`. Asta e tot mecanismul prin
    care „răspunsul la întrebarea de acum două tururi" nu umple slotul de acum."""
    slot = _ref(field)
    if slot is None or attempts < 1:
        return None
    candidate = f"q:{slot}:{min(attempts, 99)}"
    return candidate if len(candidate) <= MAX_QUESTION_ID_LEN else None


def plan_actions(view: Mapping[str, Any], facts: TurnFacts) -> tuple[ActionPlan, ...]:
    """ViewModel-ul PERSISTAT + faptele turului → planul de acțiuni. PUR și determinist.

    Se planifică din VIEW, nu din `Reply`, și asta e deliberat: view-ul e ce se persistă și ce
    citește proiecția, deci indicii (`option_ref`) și id-urile de produs se referă la EXACT lista
    pe care o vede clientul. Planificate din `Reply` ar fi putut aluneca — `render_web` filtrează
    chips prea lungi, iar al treilea buton ar fi ajuns să însemne a patra opțiune."""
    plans: list[ActionPlan] = []
    products = [p for p in (view.get("products") or []) if isinstance(p, Mapping)]
    refs: list[str] = []
    for card in products[:MAX_PLANNED_PRODUCTS]:
        ref = _ref(card.get("product_id"))
        if ref is None:
            continue  # card fără id de catalog (nu se poate acționa asupra lui, deci fără buton)
        refs.append(ref)
        plans.append(ActionPlan("request_details", ActionArgs(product_ref=ref)))
        plans.append(ActionPlan("request_reviews", ActionArgs(product_ref=ref)))
    if len(refs) >= 2:
        plans.append(ActionPlan("compare_selection", ActionArgs(product_refs=tuple(refs[:2]))))
    # NX-240 — CTA-urile de comerț. Intersecția, nu reuniunea: un buton „adaugă în coș" apare
    # numai pentru un produs care e ȘI afișat (deci clientul îl vede) ȘI dovedit vandabil (deci
    # promisiunea se susține). Un ref vandabil care nu s-a afișat n-are buton de unde să pornească.
    sellable = set(facts.commerce_product_refs)
    for ref in refs:
        if ref in sellable:
            plans.append(ActionPlan("cart_add_line", ActionArgs(product_ref=ref)))
    if facts.cart_checkout_ready:
        plans.append(ActionPlan("checkout", ActionArgs()))
    if facts.active_search_ref:
        plans.append(ActionPlan("show_more", ActionArgs(session_ref=facts.active_search_ref)))
    question_id = clarification_question_id(facts.pending_field, facts.pending_attempts)
    if question_id:
        options = [s for s in (view.get("suggestions") or []) if isinstance(s, str) and s.strip()]
        for index in range(min(len(options), MAX_PLANNED_OPTIONS)):
            plans.append(
                ActionPlan(
                    "answer_clarification",
                    ActionArgs(question_id=question_id, option_ref=index),
                )
            )
    # NX-246 — feedback, ULTIMUL și în exact două exemplare. Ratingul e în KIND, deci vine din
    # tokenul sigilat: browserul nu poate spune „positive".
    #
    # De ce NU emitem și un buton per motiv: cele 6 motive ar consuma 6 din cele 16 acțiuni ale
    # unui ViewModel și ar împinge afară exact acțiunile conversaționale (un card cu 6 produse
    # planifică deja 13). `reason` rămâne modelat, validat și persistabil — pasul doi (chips de
    # motiv după un vot negativ) e o decizie de UX care aparține NX-244, nu o jumătate de flux
    # strecurată aici. Cardul cere „două/mai multe" tokenuri; astea sunt cele două.
    #
    # Ultimul în listă, deliberat: cap-ul taie de la coadă, iar dacă un tur are atât de multe
    # acțiuni conversaționale încât nu mai încape feedbackul, atunci feedbackul e cel care cedează.
    if facts.feedback_prompt:
        plans.append(ActionPlan("feedback_up", ActionArgs()))
        plans.append(ActionPlan("feedback_down", ActionArgs()))
    return tuple(plans[:MAX_ACTIONS_PER_TURN])


# ── Envelope (server-only, sigilat) ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ActionEnvelope:
    """Ce se sigilează. Nimic comercial, niciun PII, niciun text: doar referințe și legături.

    `tenant_ref`/`session_ref`/`conversation_ref` sunt PSEUDONIME derivate cu cheia (vezi
    `action_crypto.pseudonym`) — se COMPARĂ, nu se caută, deci nu au nevoie să fie inversabile.
    `source_turn_id` rămâne id-ul real: e singurul câmp pe care backendul chiar îl CAUTĂ (rândul
    de ledger care dovedește emiterea), iar un pseudonim ar face lookup-ul imposibil. Diferența e
    deliberată și e singura: totul e oricum sub sigiliu."""

    version: str
    action_id: str
    kind: str
    args: ActionArgs
    policy: ActionPolicy
    audience: str
    tenant_ref: str
    session_ref: str
    conversation_ref: str
    source_turn_id: str
    source_revision: int
    issued_at: int
    expires_at: int

    def to_claims(self) -> dict[str, Any]:
        """Claims-urile canonice (chei scurte: tokenul se plimbă prin browser la fiecare click)."""
        return {
            "v": self.version,
            "aid": self.action_id,
            "k": self.kind,
            "a": self.args.to_canonical(),
            "p": self.policy,
            "aud": self.audience,
            "t": self.tenant_ref,
            "s": self.session_ref,
            "c": self.conversation_ref,
            "src": self.source_turn_id,
            "rev": self.source_revision,
            "iat": self.issued_at,
            "exp": self.expires_at,
        }

    @classmethod
    def from_claims(cls, raw: object) -> ActionEnvelope | None:
        """Claims → envelope validat, sau None. Nu ridică: un sigiliu deschis cu succes dar cu
        conținut necunoscut e tot o respingere GENERICĂ, nu o excepție cu detalii de oferit."""
        if not isinstance(raw, Mapping):
            return None
        if raw.get("v") != ACTION_ENVELOPE_VERSION:
            return None
        spec = spec_for(raw.get("k"))
        if spec is None:
            return None
        args = ActionArgs.parse(raw.get("a") or {}, spec)
        if args is None:
            return None
        policy = raw.get("p")
        if policy not in ("one_shot", "repeatable"):
            return None
        strings = {}
        for key in ("aid", "aud", "t", "s", "c", "src"):
            value = raw.get(key)
            if not isinstance(value, str) or not value or len(value) > 128:
                return None
            strings[key] = value
        ints = {}
        for key in ("rev", "iat", "exp"):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            ints[key] = value
        return cls(
            version=ACTION_ENVELOPE_VERSION,
            action_id=strings["aid"],
            kind=spec.kind,
            args=args,
            policy=policy,  # type: ignore[arg-type]
            audience=strings["aud"],
            tenant_ref=strings["t"],
            session_ref=strings["s"],
            conversation_ref=strings["c"],
            source_turn_id=strings["src"],
            source_revision=ints["rev"],
            issued_at=ints["iat"],
            expires_at=ints["exp"],
        )


@dataclass(frozen=True)
class ActionCommand:
    """Rezultatul TYPED după crypto + autorizare + verificarea stării: ce are voie kernelul să
    execute. Nu conține tokenul, nu conține claims brute și nu se construiește din request —
    doar `action_service` îl poate produce."""

    action_id: str
    kind: str
    args: ActionArgs
    policy: ActionPolicy
    source_turn_id: str
    source_revision: int
    conversation_id: str
    # Textul opțiunii ALESE, recuperat din ViewModel-ul persistat al turului-sursă (server-owned).
    # Nu vine din browser: browserul a trimis un ordinal opac, serverul și-a citit propria listă.
    option_text: str | None = None

    def to_jsonb(self) -> dict[str, Any]:
        """Forma DURABILĂ (payload-ul mesajului inbound). Recovery-ul NX-233 reconstruiește turul
        exclusiv din DB, deci o comandă care ar trăi doar în requestul HTTP ar dispărea la restart
        — iar executorul ar rula același turn fără să știe ce a apăsat clientul."""
        out: dict[str, Any] = {
            "v": 1,
            "action_id": self.action_id,
            "kind": self.kind,
            "args": self.args.to_canonical(),
            "policy": self.policy,
            "source_turn_id": self.source_turn_id,
            "source_revision": self.source_revision,
        }
        if self.option_text:
            out["option_text"] = self.option_text[:200]
        return out

    @classmethod
    def from_jsonb(cls, raw: object, *, conversation_id: str = "") -> ActionCommand | None:
        """Citirea inversă, DEFENSIVĂ (ca `web.context.from_payload`): DB-ul e sursă de
        durabilitate, nu de încredere — un rând editat manual retrece prin aceleași validări."""
        if not isinstance(raw, Mapping):
            return None
        spec = spec_for(raw.get("kind"))
        if spec is None:
            return None
        args = ActionArgs.parse(raw.get("args") or {}, spec)
        if args is None:
            return None
        action_id = raw.get("action_id")
        source = raw.get("source_turn_id")
        if not isinstance(action_id, str) or not isinstance(source, str):
            return None
        policy = raw.get("policy")
        option_text = raw.get("option_text")
        revision = raw.get("source_revision")
        return cls(
            action_id=action_id[:128],
            kind=spec.kind,
            args=args,
            policy=policy if policy in ("one_shot", "repeatable") else spec.policy,
            source_turn_id=source[:64],
            source_revision=revision
            if isinstance(revision, int) and not isinstance(revision, bool)
            else 0,
            conversation_id=conversation_id,
            option_text=str(option_text)[:200] if isinstance(option_text, str) else None,
        )


# ── Rezultatul kernelului ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Handled:
    """Turul a fost servit DETERMINIST (zero inferență). `reason` intră în metrici."""

    kind: str
    reason: str = "served"


@dataclass(frozen=True)
class Continue:
    """Acțiunea a produs INPUT STRUCTURAT (rută + propuneri de stare); creierul continuă turul."""

    kind: str
    reason: str = "structured_input"


@dataclass(frozen=True)
class Rejected:
    """Refuz TYPED, cu un cod din vocabularul închis. Niciodată detalii despre de ce anume."""

    code: str
    kind: str | None = None
    reason: str = ""


ActionOutcome = Handled | Continue | Rejected


# ── Copy server-owned pentru etichete (D3: fallback pe pilot, fără KeyError) ────────────────
# Etichetele sunt ale SERVERULUI, ca tot copy-ul v2 (NX-228). Frontendul le afișează; dacă le
# schimbă, se schimbă doar ce scrie pe buton — semantica stă în token.
ACTION_LABELS: Mapping[str, Mapping[str, str]] = {
    "ro": {
        "select_product": "Alege produsul",
        "request_details": "Spune-mi mai multe",
        "request_reviews": "Vezi recenziile",
        "compare_selection": "Compară-le",
        "show_more": "Arată-mi mai multe",
        "cart_add_line": "Adaugă în coș",
        "checkout": "Finalizează comanda",
        "feedback_up": "Mi-a fost de folos",
        "feedback_down": "Nu m-a ajutat",
    },
    "en": {
        "select_product": "Choose this one",
        "request_details": "Tell me more",
        "request_reviews": "See reviews",
        "compare_selection": "Compare them",
        "show_more": "Show me more",
        "cart_add_line": "Add to cart",
        "checkout": "Checkout",
        "feedback_up": "This helped",
        "feedback_down": "This did not help",
    },
    "hu": {
        "select_product": "Ezt választom",
        "request_details": "Mesélj róla többet",
        "request_reviews": "Vélemények",
        "compare_selection": "Hasonlítsd össze",
        "show_more": "Mutass többet",
        "cart_add_line": "Kosárba",
        "checkout": "Megrendelés",
        "feedback_up": "Ez segített",
        "feedback_down": "Ez nem segített",
    },
}


def action_label(kind: str, language: str | None) -> str | None:
    """Eticheta unui kind în limba turului, sau None dacă nu are una (⇒ nu se afișează)."""
    table = ACTION_LABELS.get((language or "ro")[:2]) or ACTION_LABELS["ro"]
    return table.get(kind)


# ── Accesul din pipeline (tolerant, ca `state_v2.active_needs`) ─────────────────────────────
def action_command(ctx: object) -> ActionCommand | None:
    """Comanda de acțiune a turului curent, sau None.

    Tolerant la `ctx.action=None`/obiect străin DELIBERAT: `TurnContext.action` e tipizat `Any` (ca
    să nu importăm `src.web` în models), iar consumatorii sunt pe hot path. Absența comenzii
    înseamnă „turn de text obișnuit", nu o excepție."""
    command = getattr(ctx, "action", None)
    return command if isinstance(command, ActionCommand) else None


def action_kind(ctx: object) -> str | None:
    command = action_command(ctx)
    return command.kind if command is not None else None


# ── Utilitare canonice + bucket-uri de observabilitate ──────────────────────────────────────
def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serializare CANONICĂ: chei sortate, fără spații. Aceeași convenție ca fingerprint-ul de
    request (NX-232) — un id derivat din bytes trebuie să depindă de VALORI, nu de ordinea în
    care s-a întâmplat să fie construit dicționarul."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ttl_bucket(seconds: int) -> str:
    """Etichetă low-cardinality pentru `web_action_issued{ttl_bucket}` (P10/P12)."""
    if seconds <= 300:
        return "<=5m"
    if seconds <= 1800:
        return "<=30m"
    if seconds <= 7200:
        return "<=2h"
    return ">2h"


def age_bucket(seconds: float) -> str:
    """Vechimea unui token la consum (proxy măsurabil pentru vârsta cheii în serviciu)."""
    if seconds < 0:
        return "future"  # ceas în urmă / skew: vizibil, nu ascuns într-un bucket normal
    if seconds <= 60:
        return "<=1m"
    if seconds <= 600:
        return "<=10m"
    if seconds <= 3600:
        return "<=1h"
    return ">1h"


__all__ = [
    "ACTION_AUDIENCE",
    "ACTION_ENVELOPE_VERSION",
    "ACTION_ERROR_CODES",
    "ACTION_LABELS",
    "ACTION_SINKS",
    "EMITTABLE_KINDS",
    "FEEDBACK_KINDS",
    "FEEDBACK_REASONS",
    "FEEDBACK_TAXONOMY_VERSION",
    "KIND_REGISTRY",
    "RATING_BY_KIND",
    "SINK_FEEDBACK",
    "SINK_TURN",
    "MAX_ACTIONS_PER_TURN",
    "MAX_ENVELOPE_BYTES",
    "MAX_OPTION_REF",
    "MAX_PLANNED_OPTIONS",
    "MAX_PLANNED_PRODUCTS",
    "MAX_QUANTITY",
    "MAX_REFS",
    "MAX_REF_LEN",
    "REFINE_FILTERS",
    "ActionArgs",
    "ActionCommand",
    "ActionEnvelope",
    "ActionKind",
    "ActionOutcome",
    "ActionPlan",
    "ActionPolicy",
    "ActionSpec",
    "Continue",
    "Handled",
    "Rejected",
    "TurnFacts",
    "action_command",
    "action_kind",
    "action_label",
    "age_bucket",
    "assert_registry_disjoint",
    "canonical_json",
    "clarification_question_id",
    "parse_plans",
    "plan_actions",
    "spec_for",
    "ttl_bucket",
]
