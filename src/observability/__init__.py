"""NX-241 — observabilitatea de LATENȚĂ a turului (faze, contoare, bucket-uri).

Separată de `src/agent/observability.py` (traiectoria semantică a agentului) fiindcă întrebarea e
alta: nu „ce a decis", ci „unde s-a dus timpul". Tot ce iese de aici e low-cardinality și fără PII.
"""
