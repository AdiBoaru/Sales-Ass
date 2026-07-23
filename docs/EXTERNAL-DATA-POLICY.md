# Politica de date către servicii externe

**Status:** APPROVED (parte din NX-200, ratificat 2026-07-23) · **Decizie ADR:** D14
**Parent:** [docs/QUALITY-OVERHAUL-2026.md](QUALITY-OVERHAUL-2026.md)

Precondiție OBLIGATORIE pentru: reranker API (NX-209), exporter de tracing către backend extern
(NX-201 felia C), orice serviciu terț nou care primește date de conversație sau de catalog.

> **Regula de bază:** OpenTelemetry rămâne **stratul neutru** de instrumentare. Orice backend
> (Langfuse sau altul) e un **consumator înlocuibil**, niciodată o dependență de arhitectură.
> Codul nu importă SDK-ul unui vendor de observabilitate în stagii — doar OTel.

---

## 1. Ce are voie să plece extern (allowlist, nu blocklist)

| Serviciu | Câmpuri PERMISE | Interzis explicit |
|---|---|---|
| **Reranker API** (Cohere/Voyage) | textul query-ului **normalizat** (post-redactare), textele de produs din `positive_search_document` (conținut de catalog, public pe site) | `raw_query` neredactat · orice câmp din `channel_identities` · `contact_id`/`conversation_id` reale · istoric de conversație · needs profile |
| **Tracing extern** (dacă se activează) | ID-uri **pseudonimizate** (hash cu sare per business), nume de stagiu, durate, coduri de status, counts, `business_id` pseudonimizat, model/tokeni/cost | textul mesajelor · `raw_query` · body de răspuns · orice PII · conținut de `state`/profil · sloturi sensibile (NX-212) |
| **LLM provider** (OpenAI — existent) | conform contractului existent al pipeline-ului (prompt + tool results) | telefon/E.164 · adrese · date de plată |

**Default fail-closed:** un câmp care nu e în allowlist NU pleacă. Adăugarea unui câmp nou în
allowlist = modificare a acestui document, cu review.

## 2. Redactare înainte de export

- Redactarea se face **la sursă**, înainte de serializare — nu la destinație, nu „prin
  configurația vendorului".
- `raw_query` (D6, `RuntimeQuerySpec`) **nu părăsește niciodată procesul**: către reranker pleacă
  doar forma normalizată, trecută prin redactarea PII (telefon, email, IBAN, CNP, adresă, nume
  proprii detectate).
- Sloturile marcate `sensitivity: high` (NX-212 — sarcină, afecțiuni, alergii) **nu pleacă extern
  în nicio formă**, nici pseudonimizate.
- Test obligatoriu per integrare: payload-ul serializat nu conține niciun pattern PII (aceeași
  suită ca testele „no PII in properties" din NX-163).

## 3. Pseudonimizare

- Identificatorii care trebuie corelați între trace-uri (`conversation_id`, `contact_id`,
  `business_id`) pleacă **doar ca hash** (sare per business, stocată local, nerotită fără plan
  de migrare a trace-urilor).
- Corelarea inversă (hash → entitate) e posibilă **doar local**, prin tabelele proprii. Serviciul
  extern nu poate reidentifica pe cont propriu.

## 4. Retenție

| Destinație | Retenție maximă | Ștergere |
|---|---|---|
| Reranker API | fără stocare persistentă cerută; se alege un plan/vendor **fără antrenare pe datele clientului** și cu retenție ≤30 zile pentru abuse-monitoring | contractual |
| Tracing extern | ≤90 zile | ștergere programatică la cerere (GDPR) + la dezactivarea per business |

Ștergerea GDPR (`gdpr_erase_contact`) trebuie să declanșeze și ștergerea trace-urilor
corelate prin hash-ul pseudonim — pas obligatoriu în DoD-ul integrării de tracing extern.

## 5. Rezidență

- Preferință: procesare în **UE** (endpoint european) pentru orice serviciu care primește text
  de conversație.
- Dacă un vendor nu oferă endpoint UE, integrarea cere decizie explicită documentată aici
  (nu se activează tacit).

## 6. Separare pe tenant + opt-out (TESTABIL)

- **Opt-out per business:** flag în `businesses.settings` (ex. `external_services.rerank_enabled`,
  `external_services.tracing_enabled`), **default OFF** la introducerea fiecărui serviciu nou.
- Un business cu opt-out activ: **zero apeluri externe** pentru el; sistemul degradează pe calea
  internă (rerank absent → ranking existent; tracing extern absent → doar spans locale).
  Degradarea **nu produce regresie funcțională și nu produce tăcere** (P6).
- **Test obligatoriu:** un business cu opt-out nu generează nicio cerere externă (mock la nivel
  de transport, assert pe zero apeluri) + un business fără opt-out nu poate „împrumuta" date de
  la altul (test adversarial cross-tenant pe payload).

## 7. Kill-switch

Fiecare integrare externă are kill-switch global (env/config) care o oprește complet, cu
comportament vechi complet funcțional. Oprirea nu cere redeploy de cod.

---

**Definition of Done pentru orice integrare externă nouă:** allowlist de câmpuri definit aici ·
redactare la sursă testată · pseudonimizare implementată · retenție + rezidență documentate ·
opt-out per business default OFF, cu test de zero-apeluri · kill-switch global · degradare fără
regresie · test adversarial cross-tenant.
