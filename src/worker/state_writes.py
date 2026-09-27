"""NX-331 (kernel.v1.0, pasul 3b) — SINGURUL scriitor al vederii v1 pe siturile mutate pe propuneri.

Până la NX-331, trei situri scriau starea de DOUĂ ori: o dată direct în forma v1 (`ctx.state`), o
dată ca `StateUpdateProposal` pentru reducerul v2. Cele două jumătăți erau scrise una lângă alta,
deci puteau diverge fără ca vreun test să observe. Acum propunerea e SURSA, iar forma v1 se
derivă din ea aici, cât timp v1 e forma persistată (`_build_new_state`).

Matricea NX-327 (`scripts/state_writers_fate.json`) dă funcției soarta `stays` până la retragerea
formatului v1; atunci modulul dispare întreg, iar I3 („starea se schimbă doar prin propuneri")
rămâne adevărat fără excepții.

Traducerea e mecanică și păstrează EXACT scrierea v1 de dinainte (paritate per sit, testată în
`tests/test_state_writes_parity.py`, cu flagurile stinse):

- `set_need` → `constraints[key] = value` (răspunsul brut, ca înainte: cititorii v1 îl văd în tur);
- `resolve_question` → cheia în `asked_intents` (dacă lipsește, plafon 8); `close_pending=True`
  închide și întrebarea în așteptare. Doar kernelul de acțiuni o închide în tur: `clarify` o lasă
  până la scriere, fiindcă `set_clarify` citește `attempts` din ea în același tur;
- `note_asked` → cheia MUTATĂ la coada `asked_intents` (plafon 8), forma întrebării de îngustare.
"""

from __future__ import annotations

import logging

from src.conversation.state_reducer import StateUpdateProposal
from src.models import TurnContext

log = logging.getLogger(__name__)

#: Plafonul listei v1 `asked_intents` (NX-112), același pe toate siturile.
MAX_ASKED_INTENTS = 8


def apply_v1_view(
    ctx: TurnContext, proposal: StateUpdateProposal, *, close_pending: bool = False
) -> None:
    """Scrierea v1 echivalentă a unei propuneri. O operație fără vedere v1 nu scrie nimic."""
    key = proposal.key or ""
    if proposal.op == "set_need":
        ctx.state.constraints[key] = proposal.value
    elif proposal.op == "resolve_question":
        if key not in ctx.state.asked_intents:
            ctx.state.asked_intents.append(key)
            ctx.state.asked_intents[:] = ctx.state.asked_intents[-MAX_ASKED_INTENTS:]
        if close_pending:
            ctx.state.pending_question = None
    elif proposal.op == "note_asked":
        asked = [k for k in (ctx.state.asked_intents or []) if k != key]
        ctx.state.asked_intents[:] = [*asked, key][-MAX_ASKED_INTENTS:]
    else:
        # Nu e o eroare de tur (P6): propunerea tot ajunge la reducer. Semnalăm doar că un apelant
        # a cerut o vedere v1 care nu există.
        log.warning("apply_v1_view: operație fără vedere v1 op=%s", proposal.op)


__all__ = ["MAX_ASKED_INTENTS", "apply_v1_view"]
