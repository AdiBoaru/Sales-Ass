# NX-201 felia A — Reconcilierea tarifelor LLM

**Data:** 2026-07-31 · **Card:** [tasks/NX-201.md](../tasks/NX-201.md) · **Descoperit în:** NX-204a (PR #252)
**Sursă tarife:** https://developers.openai.com/api/docs/pricing (lista publicată, verificată 2026-07-31)

---

## 1. Ce era greșit

`src/agent/pricing.py` purta tarife **inventate ca estimări**, nu preluate de la furnizor. Docstring-ul
era onest că sunt estimări — dar nimeni nu le comparase vreodată cu prețul real.

| Model | Tip token | Implicit (înainte) | Publicat | Factor |
|---|---|---:|---:|---:|
| `gpt-5.4-mini` | input | 0.25 | 0.75 | **3,00x** |
| `gpt-5.4-mini` | cached_input | 0.025 | 0.075 | **3,00x** |
| `gpt-5.4-mini` | output | 2.00 | 4.50 | **2,25x** |
| `gpt-5.4-nano` | input | 0.05 | 0.20 | **4,00x** |
| `gpt-5.4-nano` | cached_input | 0.005 | 0.02 | **4,00x** |
| `gpt-5.4-nano` | output | 0.40 | 1.25 | **3,12x** |
| `gpt-5.4` (frontier) | toate | *absent* | 2.50 / 0.25 / 15.00 | n/a |
| `text-embedding-3-small` | input | 0.02 | 0.02 | ✅ corect |
| `omni-moderation-latest` | — | 0.0 | gratuit | ✅ corect |

**Abaterea NU e uniformă: 2,25x–4,00x, diferă per model ȘI per tip de token.** Consecința practică:
**nu se poate aplica un multiplicator global** pe istoricul de cost ca să-l „repare".

## 2. Impactul asupra plafoanelor de cost

Măsurat pe tururi reale (prefixul curat al rulării NX-204a, 24 execuții pe calea `/web/chat`,
configurație de producție `gpt-5.4-mini`): **cost median $0,00597/tur** la tarife reale — calculat
per apel, cu tokenii cached la tarif redus.

### Cine alimentează contoarele de cost — TREI surse, DOUĂ unități

Ăsta e faptul care contează, și nu e uniform. Tarifele corectate ating **doar** feliile alimentate
din `pricing.cost_for`; restul contoarelor merg pe o euristică fixă din config, pe care acest PR
NU o schimbă:

| Contor | Alimentat din | Atins de acest PR |
|---|---|---|
| business daily — felia **pipeline** | `ctx.usage.cost_usd` = `pricing.cost_for` ([`processor.py:347`](../src/worker/processor.py#L347)) | **DA** (2,25–4,00x) |
| business daily — felia **aftercare** | `settings.cost_triage_usd` fix ([`aftercare.py:240,290`](../src/worker/aftercare.py#L240)) | nu |
| web **per-vizitator** (`webcost:*`) | `cost_triage_usd + cost_agent_usd` fix ([`app.py:257`](../src/web/app.py#L257)) | nu |

Consecințe directe:

- **`web_cost_cap_per_visitor_usd` NU e afectat de acest PR.** Contorul lui e euristica fixă
  (0,0003 + 0,003 = $0,0033/tur implicit), deci pragul rămâne unde era, indiferent de tarife.
- **`daily_cost_cap_usd` e afectat PARȚIAL.** Același contor primește cost real (pipeline) ȘI
  euristică (aftercare) — deci nu se poate exprima onest ca „N tururi până la plafon": N depinde
  de proporția pipeline/aftercare a traficului, care variază.

> **Nu dăm aici cifre de tip „~N tururi până la plafon".** O versiune anterioară a acestui document
> conținea „~838" și „~84"; ambele erau derivate presupunând un contor alimentat uniform din tarife.
> Prima e parțială (ignoră felia aftercare), a doua e pur și simplu falsă (contorul web nici nu
> citește tarifele). O decizie de business luată pe astfel de cifre ar recalibra plafoanele greșit.

### ⚠️ Decizie de business, dar NU pe cifrele de acum

Corectarea tarifelor **nu schimbă câți bani se cheltuie în realitate** — schimbă cât de repede
urcă *felia pipeline* a contorului de business (de 2,25–4,00x, neuniform per model și per tip de
token). Direcția e sigură: plafonul zilnic se atinge **mai devreme decât până acum**, deci merită
o recalibrare deliberată înainte de trafic real de pilot.

**Cât de devreme nu se poate spune corect până când cele trei contoare nu vorbesc aceeași unitate.**
Remedierea structurală (un singur alimentator, din cost real, pe toate cele trei căi — cu teste)
e schimbare de COMPORTAMENT pe web + aftercare și **nu intră în acest PR**, care rămâne o
reconciliere de tarife. Card separat: [tasks/NX-201.md](../tasks/NX-201.md) → felia B.

## 3. Ce s-a schimbat în cod

- `_DEFAULT_PRICING` — valorile publicate + intrarea `gpt-5.4` (lipsea; NX-204a avea nevoie de
  override în `.env` doar din cauza asta).
- `_DEFAULT` (fallback pentru model necunoscut) — actualizat la noile tarife `mini`. Convenția
  „prudent = mini" e **moștenită și păstrată deliberat**, ca să nu schimbăm două lucruri odată:
  un model necunoscut e mai probabil unul NOU, deci mai scump (`gpt-5.4` costă 3,3x cât mini),
  așa că fallback-ul tot subevaluează. Remediul corect nu e să ghicim mai bine, ci să facem
  fallback-ul **detectabil** — `has_rates()` (NX-204a, PR #252).
- Test `test_published_rates_are_pinned` — pinuiește valorile publicate într-un **singur loc**,
  cu sursa. Restul testelor citesc din `_DEFAULT_PRICING` în loc să hardcodeze (trei dintre ele
  hardcodau valorile vechi și au ruginit exact aici).

## 4. Ce NU acoperă asta

- **Reconcilierea cu factura REALĂ.** Lista publicată nu arată praguri de volum, tarife
  long-context (`gpt-5.4` urcă la 5,00/22,50 peste 272K context), sau discounturi de cont.
  `usage_daily.cost_usd` rămâne o **plasă**, nu facturare. Cere acces la billing — rămas lui Adi.
- **Unificarea celor trei alimentatori de contor** (web per-vizitator + aftercare încă pe euristica
  fixă din config). E schimbare de comportament pe două căi de producție, cu teste proprii →
  felia B, nu aici. Până atunci, orice cifră „N tururi până la plafon" e o aproximare, nu un fapt.
- **Rescrierea istoricului `usage_daily`.** Rândurile vechi rămân subevaluate; nu se rescriu orb
  (factorul nu e un scalar). Dacă e nevoie de o serie comparabilă, se recalculează din
  `analytics_events` cu defalcarea `by_model`, nu prin înmulțire.
