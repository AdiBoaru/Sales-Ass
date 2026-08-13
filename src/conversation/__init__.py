"""NX-235 — memoria conversației ca STARE REDUSĂ, nu ca jurnal de presupuneri.

Pachetul ține contractul `ConversationStateV2` (`state_v2`), vocabularul de nevoi derivat din
DomainPack (`needs`), reducerul pur care e singurul care are voie să schimbe starea
(`state_reducer`) și politica de clarificare cu information gain (`clarification_policy`).

Totul aici e PUR: fără DB, fără LLM, fără ceas, fără random. Persistarea, telemetria și
rehidratarea rămân ale apelantului (processor / stagii), exact ca la `TurnSnapshot` (NX-234).
"""

from src.conversation.clarification_policy import (
    ClarificationCandidate,
    ClarificationDecision,
    ClarificationPolicy,
    decide_clarification,
    estimate_information_gain,
)
from src.conversation.needs import (
    NeedKind,
    NeedSpec,
    NeedVocabulary,
    NormalizedNeed,
    normalize_need,
)
from src.conversation.state_reducer import (
    Applied,
    ProposalOp,
    ReducedState,
    ReducerPolicy,
    RejectedUpdate,
    StateUpdateProposal,
    reduce,
    reduce_all,
)
from src.conversation.state_v2 import (
    STATE_SCHEMA_VERSION,
    AskedQuestion,
    ConversationStateV2,
    DisplayedRef,
    Need,
    PendingClarification,
    References,
    Revocation,
    Topic,
    adapt_v1,
    hydrate_state_v2,
    project_v1,
)

__all__ = [
    "STATE_SCHEMA_VERSION",
    "Applied",
    "AskedQuestion",
    "ClarificationCandidate",
    "ClarificationDecision",
    "ClarificationPolicy",
    "ConversationStateV2",
    "DisplayedRef",
    "Need",
    "NeedKind",
    "NeedSpec",
    "NeedVocabulary",
    "NormalizedNeed",
    "PendingClarification",
    "ProposalOp",
    "ReducedState",
    "References",
    "ReducerPolicy",
    "RejectedUpdate",
    "Revocation",
    "StateUpdateProposal",
    "Topic",
    "adapt_v1",
    "decide_clarification",
    "estimate_information_gain",
    "hydrate_state_v2",
    "normalize_need",
    "project_v1",
    "reduce",
    "reduce_all",
]
