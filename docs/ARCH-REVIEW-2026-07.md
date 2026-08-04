# Nativx Assistant — Architecture Review 2026-07 (go-to-market readiness)

> Review strategic „putem intra în piloturi reale?" — scor general **8.1 / 10**.
> Verdict: **DA pentru Early Access / Pilot 30 zile** pe 3-5 magazine, cu scope definit
> (recomandări, întrebări magazin, coș/checkout, handoff, măsurare), NU ca platformă
> enterprise „gata". Concluzia auditului: **NU rescriere — evoluție controlată**, cu
> următoarele 2-6 săptămâni pe fiabilitate + calitate conversațională + observabilitate,
> nu pe Kafka / multi-agent / rescrieri.
>
> Acest doc e INDEXUL auditului: scorurile, riscurile P0, și **maparea celor 15 elemente
> de backlog (A1–A15) la cardurile NX**. Cardurile complete sunt în `tasks/`.

---

## 1. Scoruri pe domenii

| Domeniu | Scor | Notă |
|---|---:|---|
| Overall architecture | 8.5 | Separare pe procese, coadă inbound, outbox outbound, pipeline explicit. |
| Event flow | 8.0 | Clar și decuplat, dar există drop tăcut pe eroare. |
| Pipeline | 8.5 | Liniar, observabil, ieftin→scump. |
| Worker design | 7.0 | Solid ca formă; riscuri ACK-on-error + DB-held-through-LLM. |
| Agent design | 6.5 | Puternic, dar `agent.py` = God Module (1411 linii). |
| LLM orchestration | 7.0 | 2 puncte LLM controlate; lipsesc planner/router/eval matur. |
| Retrieval / Search | 8.0 | Hybrid + semantic + grounding; next = reranking/evals. |
| Grounding / Validator | 8.8 | Una dintre cele mai bune părți. |
| Memory | 6.8 | Bun pt MVP, insuficient pt conversații lungi + personalizare. |
| Multi-tenant / RLS | 8.7 | Matur, izolare în straturi. |
| Dispatcher / Outbox | 7.5 | Concept corect; implementarea serială devine bottleneck. |
| Cost governance | 8.0 | Bugete per business/contact/web visitor. |
| Security | 7.8 | RLS, PII boundary, HMAC; de întărit prompt/tool injection. |
| Observability | 7.0 | Evenimente bune; lipsesc tracing/replay/eval data feed pentru dashboardul extern. |
| Testing / AI Quality | 6.2 | Zona cu cel mai mare ROI imediat. |
| Developer Experience | 7.0 | Docs excepționale, dar module mari + docs stale. |

---

## 2. Riscuri critice (P0) — DEJA CARDUITE

Cele 2 P0 (tăcere/drop) + top-P1 scalare erau deja prinse de auditul de workflow-uri anterior
(`docs/ARCHITECTURE-WORKFLOWS.md`) și au carduri LIVE. Acest review le confirmă independent:

| Risc audit | Card existent | Acoperire |
|---|---|---|
| **A1** — excepție în consumer → ACK tăcut → tur pierdut | **NX-140** | Retry capped + fallback în outbox + `turn_failed`. **Delta:** `dead_letter_inbound` durabil + vizibilitate operator (vezi §4, NX-154). |
| **A2** — lock ocupat → requeue cap → drop tăcut | **NX-140** | Cardul acoperă EXPLICIT și calea `_requeue_busy` drop (consumer.py:95-96). |
| **A3** — conexiunea DB ținută pe durata apelurilor LLM | **NX-141** | Fazare turn (post-tur → conn propriu; ConnFactory; load/run/commit). |

**Regula-invariant propusă de audit** (de verificat în NX-140 + NX-154): un mesaj inbound NU are
voie să dispară fără o stare finală — `completed(reply)` / `completed(halt intenționat)` /
`failed(fallback)` / `dead-letter vizibil + alertat`.

---

## 3. Backlog A4–A15 → carduri NX noi

Restul de 12 elemente de backlog devin carduri noi. Grupare pe temă (ordinea din audit §8, Roadmap):

| ID audit | Temă | Card | Tip | Prio |
|---|---|---|---|---|
| A4 | Extrage **validatorul** din `agent.py` | **NX-142** | full | P1 |
| A5 | Extrage **deterministic intents + tool executor** din `agent.py` | **NX-143** | full | P1 |
| A6 | **Response Planner** + response templates | **NX-144** | full | P1 |
| A7 | **Golden conversations** + regression evals | **NX-145** | full | P1 |
| A8 | **Turn Replay** intern | **NX-146** | full | P1 |
| A9 | **Dispatcher concurrency** bounded + outbox lag metrics | **NX-147** | full | P1 |
| A10 | **Conversation/User facts** structurate | **NX-148** | full | P1 |
| A11 | Docs/docstrings stale + rerun arch explorer | **NX-149** | compact | P2 |
| A12 | Prompt/tool **injection tests** + tool authorization layer | **NX-150** | compact | P2 |
| A13 | **Retrieval evals** + reranking | **NX-151** | compact | P2 |
| A14 | **Cost per intent/stage** + model router simplu | **NX-152** | compact | P2 |
| A15 | Mută **migration assert** din `scripts` în modul runtime | **NX-153** | compact | P2 |

Cardurile P2 (NX-149…NX-153) + delta-urile de fiabilitate (NX-154) sunt în
[`tasks/NX_backlog_arch_review_2026-07.md`](../tasks/NX_backlog_arch_review_2026-07.md).

---

## 4. Delta de fiabilitate peste NX-140 (P1)

| ID | Temă | Card | Notă |
|---|---|---|---|
| A1-delta | `dead_letter_inbound` durabil + data feed pentru dashboard extern (ops visibility) | **NX-154** | NX-140 face retry+fallback; auditul cere ȘI o coadă dead-letter vizibilă + date stabile pentru dashboardul din proiectul extern. UI-ul dashboardului este out of scope pentru acest repo. |

---

## 5. Roadmap (din audit §8)

**Quick Wins (1-2 săpt):** NX-140 (ACK/drop → fallback+DLQ) · NX-154 (data feed pentru dashboard extern) ·
fallback final localizat RO/HU/EN · NX-145 felia 1 (30-50 golden) · NX-149 (docs) ·
NX-144 felia 1 (response templates).

**Medium (1-2 luni):** NX-142→NX-143→NX-144 (spargere agent, în ordinea validator→intents→planner) ·
NX-148 (facts) · NX-146 (turn replay) · NX-147 (dispatcher concurrency) · NX-152 (cost/intent) ·
NX-151 (retrieval evals + rerank) · NX-141 felia 1 (eliberare conn post-tur).

**Major (6-12 luni):** refactor complet processor (loader/executor/committer/aftercare — vezi NX-141) ·
LLM Gateway intern (provider abstraction + model router + prompt registry) · Memory v2 (working/user/semantic) ·
Advanced search (reranker + feedback loop) · enterprise observability · data-plane scaling.

**Explicit NU acum (over-engineering la acest stadiu):** Kafka/NATS, microservicii per stagiu,
multi-agent complex, model router cu 10 modele, vector memory complet înainte de date reale.

---

## 6. Condiții minime înainte de primul pilot PLĂTIT (din audit §11)

1. P0-urile de tăcere/drop rezolvate (NX-140 + NX-154).
2. Data feed minim pentru dashboardul extern (NX-154; UI-ul traieste in proiect separat).
3. 30-50 scenarii de test trecute (NX-145).
4. Handoff/operator clar (există; de verificat pe canalele cu handoff_enabled).
5. Limite de cost active (există — cost guard per business/contact/web visitor).
6. Logging/replay minim pentru orice conversație problematică (NX-146).

---

## Sursă
Review de arhitectură primit 2026-07-06 (scor 8.1/10, backlog A1–A15). Grounding în cod:
`src/worker/stages/agent.py` (1411 linii), `src/worker/dispatcher.py`, `src/worker/compose.py`,
`src/worker/context.py`, `src/db/queries/analytics.py`, `src/evals/golden.py`. Audituri conexe:
`ARCHITECTURE-AUDIT.md`, `ARCHITECTURE-WORKFLOWS.md` (sursa NX-140/141), `ARCH-2026-ROADMAP.md`.
