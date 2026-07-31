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

| Plafon | Valoare | Tururi până la plafon — ACUM (real) | Cât *părea* înainte |
|---|---:|---:|---:|
| `daily_cost_cap_usd` (business) | $5,00 | **~838** | ~1.880 – 3.350 |
| `web_cost_cap_per_visitor_usd` | $0,50 | **~84** | ~188 – 335 |
| `contact_daily_cost_cap_usd` | (vezi config) | ÷2,25–4,00 | — |

> Intervalele „înainte" sunt derivate din factorii per-celulă, nu recalculate exact: harness-ul
> nu înregistrează tokenii **cached** per tur, iar fără ei costul la tarifele vechi nu se poate
> reconstitui punctual. `UsageAccumulator.cached_tokens` îi are — de instrumentat în felia B,
> pentru raportul de baseline.

### ⚠️ Decizie necesară: plafoanele se strâng de 2,25–4x fără să le fi atins nimeni

Corectarea tarifelor **nu schimbă câți bani se cheltuie în realitate** — schimbă cât de repede
*contorul* ajunge la plafon. Un tenant care mergea confortabil sub $5/zi poate fi acum tăiat la
~838 tururi în loc de ~2.500. **Plafoanele trebuie recalibrate deliberat** (proporțional, sau
la o valoare nouă gândită), altfel corectarea de acuratețe se transformă într-o întrerupere de
serviciu. Nu e o decizie de cod — e una de business.

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
- **Rescrierea istoricului `usage_daily`.** Rândurile vechi rămân subevaluate; nu se rescriu orb
  (factorul nu e un scalar). Dacă e nevoie de o serie comparabilă, se recalculează din
  `analytics_events` cu defalcarea `by_model`, nu prin înmulțire.
