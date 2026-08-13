"""NX-236 — kernelul de acțiuni: dispatch TYPED, determinist, înaintea triajului.

Un token deja autorizat (`web.action_service`) ajunge aici ca `ActionCommand`: kind allowlisted,
argumente canonice, turul-sursă dovedit, consum înregistrat. Kernelul face UN singur lucru —
alege handlerul din registry și îl execută pe aceleași drumuri ca varianta text. Nu parsează
etichete, nu construiește prompturi, nu numește tool-uri (registrele sunt disjuncte, verificat la
import) și nu are ramură `else` care „încearcă ceva".

**De ce înaintea triajului.** O acțiune nu e o intenție de ghicit: e deja o decizie. A o trece
prin clasificatorul de text ar însemna să transformăm o comandă exactă într-o presupunere — și, pe
un mesaj gol (o acțiune NU are text de client), într-o presupunere fără dovezi.

**Două feluri de rezultat, ambele oneste.**

  • `Handled` — turul e servit DETERMINIST, zero inferență: detalii, recenzii, comparație. Exact
    handlerele NX-219/NX-220, apelate cu id-uri, nu cu deixis.
  • `Continue` — acțiunea produce INPUT STRUCTURAT (rută + propuneri de stare) și creierul
    continuă turul: paginarea unei sesiuni active, răspunsul la o clarificare. Nu se fabrică text
    de client; singurul text care intră e OPȚIUNEA pe care serverul însuși a scris-o și a
    persistat-o în turul-sursă, aleasă printr-un ordinal opac.

**Ce se refuză, cu reply randabil (P6, niciodată tăcere):** kind fără handler sigur, o sesiune de
căutare care nu mai există, o clarificare la care se răspunde după ce întrebarea s-a schimbat. Un
refuz e un mesaj, nu un ecran gol.

**Comerțul (NX-237):** kind-urile mutante (`cart_*`, `checkout`) au acum handler REAL — aceeași
comandă typed (`CartCommand`) și același `CartService` ca tool-urile LLM, cu receipt idempotent pe
`action_id` (double-click sau race pe one-shot ⇒ O singură creștere, același rezultat). Refuzate
onest cât timp `CONVERSATION_CART_ENABLED` e stins. Confirmarea e DETERMINISTĂ, din snapshot
(server-owned), și numește coșul ca al conversației — nu pretinde coșul global al magazinului.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import TYPE_CHECKING, Any

from src.agent.deterministic import serve_comparison, serve_details, serve_reviews
from src.agent.tool_definitions import TOOL_NAMES
from src.config import get_settings
from src.conversation.state_reducer import StateUpdateProposal
from src.models import Route, RouteDecision, TurnContext
from src.web.action_models import (
    Continue,
    Handled,
    Rejected,
    action_command,
    assert_registry_disjoint,
    clarification_question_id,
    spec_for,
)

if TYPE_CHECKING:
    from src.web.action_models import ActionCommand, ActionOutcome
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


# ── Copy server-owned pentru refuzuri (D3: fallback pe pilot, fără KeyError) ────────────────
# Refuzurile sunt PROPOZIȚII, nu coduri: clientul a apăsat un buton real și merită un motiv real.
_COPY: dict[str, dict[str, str]] = {
    "ro": {
        "unavailable": "Deocamdată nu pot face asta din chat. Îți pot arăta produsul sau "
        "detaliile lui, dacă vrei.",
        "stale_search": "Căutarea de dinainte nu mai e activă. Spune-mi din nou ce cauți și "
        "caut de la capăt.",
        "stale_question": "Între timp am schimbat subiectul, așa că răspunsul ăsta nu mai are "
        "unde să meargă. Spune-mi ce cauți și continuăm.",
        "compare_failed": "Nu pot compara acum produsele alese. Îți pot arăta detaliile "
        "fiecăruia, dacă vrei.",
        "cart_failed": "Nu am putut actualiza coșul pentru produsul ales. Îți pot arăta "
        "alternative, dacă vrei.",
        "cart_stale": "Coșul s-a schimbat între timp. Aruncă o privire la varianta curentă "
        "și mai încearcă o dată.",
        "cart_empty": "Coșul e gol deocamdată. Alege întâi un produs.",
        "checkout_failed": "Nu pot pregăti plata acum. Coșul rămâne neschimbat.",
    },
    "en": {
        "unavailable": "I can't do that from chat yet. I can show you the product or its "
        "details instead.",
        "stale_search": "That search isn't active anymore. Tell me again what you're looking "
        "for and I'll start over.",
        "stale_question": "We moved on since then, so this answer has nowhere to go. Tell me "
        "what you need and we'll continue.",
        "compare_failed": "I can't compare those products right now. I can show you the "
        "details of each one instead.",
        "cart_failed": "I couldn't update the cart for that product. I can show you "
        "alternatives if you like.",
        "cart_stale": "The cart changed in the meantime. Take a look at the current version "
        "and try again.",
        "cart_empty": "Your cart is empty for now. Pick a product first.",
        "checkout_failed": "I can't prepare the payment right now. Your cart is unchanged.",
    },
    "hu": {
        "unavailable": "Ezt még nem tudom megcsinálni a chatből. Megmutathatom a terméket vagy "
        "a részleteit.",
        "stale_search": "Az a keresés már nem aktív. Mondd el újra, mit keresel, és elölről "
        "kezdem.",
        "stale_question": "Közben témát váltottunk, így ennek a válasznak már nincs hova "
        "mennie. Mondd el, mire van szükséged, és folytatjuk.",
        "compare_failed": "Most nem tudom összehasonlítani a kiválasztott termékeket. "
        "Megmutathatom külön-külön a részleteiket.",
        "cart_failed": "Nem tudtam frissíteni a kosarat ezzel a termékkel. Mutathatok "
        "alternatívákat, ha szeretnéd.",
        "cart_stale": "A kosár időközben megváltozott. Nézd meg a jelenlegi állapotát, és "
        "próbáld újra.",
        "cart_empty": "A kosár egyelőre üres. Válassz először egy terméket.",
        "checkout_failed": "Most nem tudom előkészíteni a fizetést. A kosár változatlan.",
    },
}

# Confirmările de coș — DETERMINISTE, server-owned, din snapshot. Numărul de produse și totalul
# vin calculate de serviciu (display strings), nu compuse de vreun model.
_CART_CONFIRM_COPY: dict[str, dict[str, str]] = {
    "ro": {
        "updated": "Am actualizat coșul conversației: {n} produse.",
        "updated_total": "Am actualizat coșul conversației: {n} produse, total {total}.",
        "cleared": "Am golit coșul.",
        "checkout": "Ți-am pregătit linkul de plată: {url}",
    },
    "en": {
        "updated": "I updated this conversation's cart: {n} items.",
        "updated_total": "I updated this conversation's cart: {n} items, total {total}.",
        "cleared": "I emptied the cart.",
        "checkout": "Here is your payment link: {url}",
    },
    "hu": {
        "updated": "Frissítettem a beszélgetés kosarát: {n} termék.",
        "updated_total": "Frissítettem a beszélgetés kosarát: {n} termék, összesen {total}.",
        "cleared": "Kiürítettem a kosarat.",
        "checkout": "Itt a fizetési linked: {url}",
    },
}


def _copy(language: str | None) -> dict[str, str]:
    return _COPY.get((language or "ro")[:2]) or _COPY["ro"]


def _refuse(ctx: TurnContext, code: str, message_key: str, kind: str) -> Rejected:
    """Refuz VIZIBIL: reply randabil + verdict typed. `cacheable=False` — un refuz e legat de
    starea ACESTEI conversații, iar un cache hit l-ar servi altcuiva (lecția NX-172)."""
    ctx.set_reply(_copy(ctx.language)[message_key], cacheable=False)
    return Rejected(code, kind, message_key)


def _select(ctx: TurnContext, product_id: str) -> None:
    """O acțiune pe un produs E o selecție explicită (NX-235: `source="action"`).

    Propunerea trece prin reducer ca oricare alta — un click nu ocolește regulile de stare, doar
    aduce cea mai bună sursă posibilă: clientul a arătat cu degetul."""
    ctx.state_proposals.append(
        StateUpdateProposal(
            "set_references",
            source="action",
            turn_id=ctx.turn_id,
            payload={"selected_product": product_id, "last_action": "select"},
        )
    )


async def _handle_product_action(
    ctx: TurnContext, deps: PipelineDeps, command: ActionCommand
) -> ActionOutcome:
    """`select_product` / `request_details` / `request_reviews` — toate pe un produs NUMIT."""
    product_id = command.args.product_ref or ""
    if command.kind == "request_reviews":
        await serve_reviews(ctx, deps, product_id)
    else:
        # `select_product` = „vreau ăsta": răspunsul util e fișa produsului, aceeași pe care o
        # servește și `request_details`. Un al doilea comportament pentru același gest ar fi doar
        # o a doua cale de întreținut.
        await serve_details(ctx, deps, product_id)
    if ctx.retrieval is not None:
        # Selecția se memorează DUPĂ ce produsul a supraviețuit porții de siguranță (NX-173) și e
        # încă în catalog: `ctx.retrieval` e semnalul că a fost servit efectiv. Altfel am ține minte
        # ca „ales" tocmai produsul pe care tocmai am refuzat să-l arătăm.
        _select(ctx, product_id)
    return Handled(command.kind)


async def _handle_compare(
    ctx: TurnContext, deps: PipelineDeps, command: ActionCommand
) -> ActionOutcome:
    ids = list(command.args.product_refs)
    if await serve_comparison(ctx, deps, ids):
        ctx.state_proposals.append(
            StateUpdateProposal(
                "set_references",
                source="action",
                turn_id=ctx.turn_id,
                payload={"compared_products": ids, "last_action": "compare"},
            )
        )
        return Handled(command.kind)
    # Sub 2 produse valide (stoc/safety/coerență) — pe calea text ar cădea în bucla LLM, dar aici
    # nu există text de căutat: mai bine onest decât un model care improvizează pe mesaj gol.
    return _refuse(ctx, "action_stale", "compare_failed", command.kind)


def _handle_show_more(ctx: TurnContext, command: ActionCommand) -> ActionOutcome:
    """Paginare: input STRUCTURAT pentru drumul determinist din `agent_stage` (NX-119b).

    Nu fabricăm „mai arată-mi": setăm ruta și lăsăm `is_show_more` să recunoască acțiunea.
    Sesiunea trebuie să fie ACEEAȘI (`fp`) — altfel butonul ar pagina altă listă decât cea
    de sub care a fost apăsat."""
    session = ctx.state.active_search or {}
    if not session or str(session.get("fp") or "") != (command.args.session_ref or ""):
        return _refuse(ctx, "action_stale", "stale_search", command.kind)
    ctx.route = RouteDecision(route=Route.SALES)
    return Continue(command.kind, "pagination")


def _handle_answer_clarification(ctx: TurnContext, command: ActionCommand) -> ActionOutcome:
    """Răspuns la clarificare prin `question_id` + ordinal opac, niciodată prin eticheta primită.

    Verificarea de prospețime e o comparație de id-uri: întrebarea în așteptare ACUM trebuie să
    fie exact cea peste care s-a emis butonul. Dacă între timp am întrebat altceva (alt slot, altă
    încercare) sau nimic, alegerea NU modifică starea — cerința explicită a cardului."""
    pending = ctx.state.pending_question if isinstance(ctx.state.pending_question, dict) else None
    field = str((pending or {}).get("field") or "")
    attempts = int((pending or {}).get("attempts") or 1)
    current_id = clarification_question_id(field, attempts) if pending else None
    if current_id is None or current_id != command.args.question_id or not command.option_text:
        return _refuse(ctx, "action_stale", "stale_question", command.kind)

    answer = command.option_text
    # Aceeași umplere ca pe calea text (`clarify_resume_stage`), doar că ancorată de întrebare:
    # slotul curent primește valoarea, iar reducerul o normalizează canonic (NX-235).
    ctx.state.constraints[field] = answer
    ctx.state_proposals.append(
        StateUpdateProposal(
            "resolve_question",
            key=field,
            question_id=current_id,
            source="action",
            turn_id=ctx.turn_id,
        )
    )
    ctx.state_proposals.append(
        StateUpdateProposal(
            "set_need", key=field, value=answer, source="action", turn_id=ctx.turn_id
        )
    )
    if field not in ctx.state.asked_intents:
        ctx.state.asked_intents.append(field)
        ctx.state.asked_intents[:] = ctx.state.asked_intents[-8:]
    # Întrebarea e închisă AICI → `clarify_resume_stage` nu o mai consumă a doua oară.
    ctx.state.pending_question = None
    # Textul opțiunii devine inputul turului. NU e text fabricat de noi și nu e eticheta trimisă
    # de browser: e opțiunea pe care SERVERUL a scris-o în turul-sursă, recitită din ledger și
    # aleasă printr-un ordinal opac. Browserul n-a putut nici să o compună, nici să o schimbe.
    ctx.message.body = answer
    try:
        ctx.route = RouteDecision(route=Route(pending.get("resume_route") or Route.SALES.value))
    except ValueError:
        ctx.route = RouteDecision(route=Route.SALES)
    return Continue(command.kind, "clarification_answer")


_COMMERCE_OPS: dict[str, str] = {
    "cart_add_line": "add",
    "cart_set_quantity": "set_quantity",
    "cart_remove": "remove",
    "cart_clear": "clear",
}


def _cart_confirm(ctx: TurnContext, snapshot: Any, *, cleared: bool = False) -> None:
    """Confirmarea DETERMINISTĂ a coșului, din snapshot (server-owned). Totalul apare DOAR când e
    cunoscut (`totals.status == 'known'`) — un total incert nu se afirmă (D8)."""
    table = _CART_CONFIRM_COPY.get((ctx.language or "ro")[:2]) or _CART_CONFIRM_COPY["ro"]
    if cleared or not snapshot.lines:
        ctx.set_reply(table["cleared"], cacheable=False)
        return
    totals = snapshot.totals
    if totals.status == "known" and totals.display:
        text = table["updated_total"].format(n=len(snapshot.lines), total=totals.display)
    else:
        text = table["updated"].format(n=len(snapshot.lines))
    ctx.set_reply(text, cacheable=False)


async def _handle_commerce(
    ctx: TurnContext, deps: PipelineDeps, command: ActionCommand
) -> ActionOutcome:
    """NX-237: mutațiile de coș din click — ACEEAȘI comandă typed + ACELAȘI serviciu ca tool-urile
    LLM. Idempotency pe `action_id`: două tururi care ar apuca să consume aceeași acțiune cad
    pe același receipt (o singură creștere). Confirmarea/refuzul: copy server-owned, determinist."""
    from src.commerce.cart_models import CartCommand  # noqa: PLC0415 — evită ciclul de import
    from src.commerce.cart_service import (  # noqa: PLC0415
        CartService,
        action_idempotency_key,
    )

    if not get_settings().conversation_cart_enabled:
        # Rollback ÎNTRE emitere și consum: refuz onest (autorizarea refuză și ea, poarta e dublă).
        return _refuse(ctx, "action_unavailable", "unavailable", command.kind)
    service = CartService.for_turn(ctx, deps)
    key = action_idempotency_key(command.action_id)
    if command.kind == "checkout":
        from src.tools.commerce_tools import _checkout_base  # noqa: PLC0415 — aceeași sursă de URL

        base = _checkout_base(ctx)
        if not base:
            return _refuse(ctx, "action_unavailable", "checkout_failed", command.kind)
        out = await service.create_checkout(
            ctx.conversation_id,
            idempotency_key=key,
            turn_id=ctx.turn_id,
            base_url=base,
            action_id=command.action_id,
        )
        if out.ok and out.receipt is not None and out.receipt.url:
            table = _CART_CONFIRM_COPY.get((ctx.language or "ro")[:2]) or _CART_CONFIRM_COPY["ro"]
            ctx.set_reply(table["checkout"].format(url=out.receipt.url), cacheable=False)
            return Handled(command.kind)
        if out.error == "cart_empty":
            return _refuse(ctx, "action_stale", "cart_empty", command.kind)
        return _refuse(ctx, "action_unavailable", "checkout_failed", command.kind)
    cart_command = CartCommand.parse(
        _COMMERCE_OPS[command.kind],
        {"product_id": command.args.product_ref, "quantity": command.args.quantity},
    )
    if cart_command is None:
        return _refuse(ctx, "action_invalid", "cart_failed", command.kind)
    out = await service.mutate(
        ctx.conversation_id,
        cart_command,
        idempotency_key=key,
        turn_id=ctx.turn_id,
        action_id=command.action_id,
    )
    if out.conflict:
        return _refuse(ctx, "action_stale", "cart_stale", command.kind)
    if not out.ok:
        return _refuse(ctx, "action_unavailable", "cart_failed", command.kind)
    ctx.state_patch["cart_ref"] = out.snapshot.to_state_ref()
    _cart_confirm(ctx, out.snapshot, cleared=(command.kind == "cart_clear"))
    return Handled(command.kind)


async def dispatch(ctx: TurnContext, deps: PipelineDeps, command: ActionCommand) -> ActionOutcome:
    """Dispatch EXHAUSTIV pe registry. Fără `else` care improvizează: un kind fără handler e un
    refuz onest, nu o încercare de a-i ghici sensul."""
    spec = spec_for(command.kind)
    if spec is None or not spec.available:
        # Nu ar trebui să ajungă aici (`authorize_action` refuză înainte), dar un kernel care se
        # bazează pe apelant pentru poarta de mutație e un kernel cu o singură poartă.
        return _refuse(ctx, "action_unavailable", "unavailable", command.kind)
    if spec.mutating:
        # NX-237: comerțul — poarta de runtime (flag + serviciu) e în handler, tot dublată.
        return await _handle_commerce(ctx, deps, command)
    if command.kind in ("select_product", "request_details", "request_reviews"):
        return await _handle_product_action(ctx, deps, command)
    if command.kind == "compare_selection":
        return await _handle_compare(ctx, deps, command)
    if command.kind == "show_more":
        return _handle_show_more(ctx, command)
    if command.kind == "answer_clarification":
        return _handle_answer_clarification(ctx, command)
    return _refuse(ctx, "action_unavailable", "unavailable", command.kind)


async def action_kernel_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Stagiul: rulează kernelul dacă turul poartă o acțiune. No-op altfel (byte-identic).

    Poziția în pipeline (după `language`, înaintea lui `clarify_resume`/`greeting`/`alias`/`cache`/
    `faq`/`triage`) e parte din contract: un `Handled` iese cu reply și nimic nu-l mai atinge, iar
    un `Continue` setează `ctx.route`, pe care straturile gratuite îl RESPECTĂ (skip)."""
    command = action_command(ctx)
    if command is None:
        return
    started = perf_counter()
    try:
        if not get_settings().web_actions_enabled:
            # Rollback ÎNTRE accept și execuție: turul poartă o comandă reală, dar feature-ul e
            # stins. Refuzăm ONEST — a-l lăsa să curgă ar însemna un turn de text cu mesaj gol.
            outcome = _refuse(ctx, "action_unavailable", "unavailable", command.kind)
        else:
            outcome = await dispatch(ctx, deps, command)
    except Exception:  # noqa: BLE001 — un handler crăpat nu are voie să lase turul mut (P6)
        log.exception("action_kernel: handler eșuat pentru kind=%s", command.kind)
        outcome = _refuse(ctx, "action_unavailable", "unavailable", command.kind)
    ctx.emit(
        "web_action_handler_ms",
        kind=command.kind,
        outcome=_outcome_label(outcome),
        elapsed_ms=round((perf_counter() - started) * 1000.0),
    )
    ctx.emit("web_action_consumed", kind=command.kind, outcome=_outcome_label(outcome))
    if isinstance(outcome, Rejected) and outcome.code == "action_stale":
        ctx.emit("web_action_stale", reason=outcome.reason)


def _outcome_label(outcome: ActionOutcome) -> str:
    """Etichetă low-cardinality (P10/P12): ce s-a întâmplat, nu cu ce argumente."""
    if isinstance(outcome, Handled):
        return "handled"
    if isinstance(outcome, Continue):
        return "continue"
    return outcome.code


# Garda structurală, la IMPORT: dacă cineva ar boteza vreodată o acțiune ca un tool, procesul nu
# pornește. E ieftin (două seturi de string-uri) și e singurul moment în care greșeala e gratis.
assert_registry_disjoint(TOOL_NAMES)


__all__: list[str] = [
    "action_kernel_stage",
    "dispatch",
]
