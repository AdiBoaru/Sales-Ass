"""NX-241 — contractele de RUNTIME ale unui tur: timpul și bugetul.

`deadline.py` = UN singur deadline monoton end-to-end (queue + execuție + reclaim).
`turn_budget.py` = plafoanele explicite (runde de model, tool calls, tokeni, cost, query-uri)
pe clase de tur, versionate ca manifest.

Ambele trăiesc într-un ContextVar (ca `src/agent/usage.py` / `src/db/op_metrics.py`): turul le
împinge o dată, iar operațiile le CITESC — nu le primesc prin zece semnături. Fără tur activ,
`current()` e `None` și tot codul se comportă exact ca înainte de card (P6, dark by default).
"""
