"""NX-396 — agentul unic (NX-394 Faza 2): un model cu unelte pe `/v1/responses` înțelege mesajul,
citește faptele și scrie tot textul; codul execută uneltele, aplică regulile dure și verifică
adevărul. Se importă LENEȘ din `agent_stage`, doar cu `ASSISTANT_AGENT_ENABLED` aprins.

- `memory`: handle-urile produselor (`P1`…) și notele, persistate în `state["assistant"]`;
- `menus`: meniurile închise ale uneltelor, din catalog;
- `schemas`: schemele STRICTE ale uneltelor, byte-identice pe conversație;
- `prompt`: instrucțiunile (generice, P11) și vederea turului;
- `tools`: executorul uneltelor peste `ctx`/`deps` reale, cu siguranța NX-173;
- `gate`: poarta de adevăr pe tot ce scrie agentul;
- `turn`: bucla, răspunsul, `ctx.retrieval`, starea și evenimentele.
"""
