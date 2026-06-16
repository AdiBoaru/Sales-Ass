# G5c — Detecție de limbă (RO/HU/EN) în Gates
**Owner:** S · **Faza:** P1 · **Zi/Ord:** după G5a (gates) + G5b (cache) · **Branch:** `feat/G5c-language-detect` · **Complexitate:** S/M · **Estimare:** 3h

## Goal
Setează corect `ctx.language` din mesajul clientului, ÎNAINTE de straturile locale-keyed
(cache, faqs, triaj). Azi `ctx.language` vine doar din `conversations.locale`/default →
un mesaj în HU sau EN pe o conversație cu locale `ro` rămâne tratat ca `ro`. Principiul 11
spune că „limba e parte din cheie": un cache hit / FAQ în limba greșită e un BUG. Detecția
e determinist (cod pur, fără LLM), printre limbile pe care businessul le suportă.

## Business Context
Verticalul țintă (RO) are clienți care scriu și în maghiară (Ardeal) sau engleză. Un bot
care răspunde în limba clientului convertește mai bine. Detecția corectă face ca TOATE
lookup-urile (semantic_cache, faqs, wa_templates) să cadă pe limba potrivită — altfel
servim un răspuns cache-uit în limba greșită (hit fals). Cost zero (determinist).

## Technical Description

### Detector determinist (`src/lang/detect.py`)
```python
def detect_language(text: str | None, supported: list[str]) -> str | None:
    """RO/HU/EN după stopwords + diacritice specifice. Întoarce un locale DOAR dacă e
    în `supported` ȘI semnalul e clar; altfel None (= fără semnal → păstrăm limba curentă)."""
```
- Normalizează `lower()` PĂSTRÂND diacriticele (sunt semnal). Tokenizează cuvinte
  (`[a-zà-ÿ]+`).
- Per limbă din `supported`: scor = nr. stopwords-uri găsite + bonus pentru diacritice
  specifice (RO: `ă î â ș ț`; HU: `ő ű` — distincte de RO).
- Întoarce limba cu scorul cel mai mare DACĂ scor ≥ 1 ȘI strict > a doua (margine);
  egalitate / zero → None (incertitudine = nu schimbăm, NU ghicim — precision-first).
- Seturi de stopwords compacte, hardcodate în modul (nu DB — sunt constante lingvistice,
  nu config de tenant). Doar limbile RO/HU/EN (cele din `businesses.supported_locales`).

### Stagiul `language_stage` (stagiul 3, după Gates, înainte de Cache)
Inserat în `DEFAULT_STAGES` între `gates_stage` și `cache_stage`:
`[gates, language, cache, triage, agent, fallback]`. Rulează DOAR cod determinist.
```python
async def language_stage(ctx, deps):
    supported = ctx.business.supported_locales
    if len(supported) <= 1:            # tenant mono-lingv → nimic de detectat
        return
    detected = detect_language(ctx.message.body, supported)
    if detected is None or detected == ctx.language:
        return
    prev = ctx.language
    ctx.language = detected                                  # owner: language_stage
    await set_conversation_locale(deps.conn, ctx.business.id, ctx.conversation_id, detected)
    ctx.emit("language_detected", **{"from": prev, "to": detected})
```
- **Niciun apel extern, fără LLM** — pur cod (principiul 2). Early-exit imposibil (nu
  setează `reply`), doar refină `ctx.language` pentru stagiile următoare.
- **Persistăm** `conversations.locale` → limba „se lipește": un follow-up scurt fără semnal
  („da", „ok") păstrează limba detectată (procesorul seedează `ctx.language` din
  `conv.locale` la turul următor), nu sare înapoi la default.

### Proprietatea câmpului `ctx.language`
`processor` SEEDează `ctx.language` din `conv.locale`/default (valoarea persistată).
`language_stage` e DETECTORUL per-tur care o poate REFINA + persista. Owner efectiv al
deciziei per-tur = `language_stage`; processorul doar inițializează din DB. (Notat în
docstring-ul câmpului din `models.py`.)

### Error handling
Detector pur, fără I/O → nu aruncă pe input. `set_conversation_locale` e best-effort:
un eșec se loghează, NU rupe turul (limba detectată rămâne pe `ctx` pentru acest tur).

## Principii CLAUDE.md aplicabile
- **P2 (LLM doar triaj+agent):** detecția e determinist, ZERO LLM.
- **P11 (limba e parte din cheie):** scopul direct — `ctx.language` corect ÎNAINTE de
  cache/faqs/triaj, ca lookup-urile să cadă pe limba reală.
- **P7 (business_id pe tot):** `set_conversation_locale` are `where business_id = $1`.
- **P3 (owner per câmp):** `language_stage` e detectorul; processorul doar seedează din DB.
- **Precision-first:** incertitudine (tie/zero semnal) → None → păstrăm limba (nu ghicim).

### Câmpuri TurnContext scrise
`ctx.language` (refinat). Niciun câmp nou. NU setează `reply`/`halt`.

## Implementation Steps
1. `src/lang/detect.py`: `detect_language` + seturile de stopwords RO/HU/EN (pur).
2. `src/db/queries/conversations.py`: `set_conversation_locale`.
3. `src/worker/stages/language.py`: `language_stage`.
4. `src/worker/runner.py`: `language_stage` în `DEFAULT_STAGES` între gates și cache.
5. `src/models.py`: adnotare owner pe `TurnContext.language`.
6. Teste (`tests/test_language_detect.py`): detector pur (RO/HU/EN, tie→None, mono-lingv)
   + `language_stage` (set + persist mock, no-op pe mono-lingv / același locale).
7. `ruff check . && ruff format . && pytest -x -q` verde.

## Files To Create / Files To Modify
**Create:** `src/lang/__init__.py` · `src/lang/detect.py` · `src/worker/stages/language.py` ·
`tests/test_language_detect.py`
**Modify:** `src/db/queries/conversations.py` · `src/worker/runner.py` · `src/models.py`

## Database Changes
**None (DDL).** `conversations.locale` există (schema_v2). Query nou:
`set_conversation_locale` = `update conversations set locale = $3 where business_id = $1
and id = $2`.

## API Changes
None.

## Events de emis (analytics)
- `language_detected` {from, to} — la o schimbare de limbă (FĂRĂ corpul mesajului, P12).
  Justificare: doar la schimbare (nu pe fiecare tur) → semnal de mix lingvistic per tenant.

## Dependencies
**G5a** (gates_stage, stagiul 3) — în main (#54). **G2b** (runner/DEFAULT_STAGES) — în main.
`businesses.supported_locales` în `BusinessConfig` (load_business) — în main.
`conversations.locale` citit de processor → `ctx.language` — în main.

## Out of Scope
- **Detecție via LLM / librărie grea** (langdetect/fasttext) — v1 = stopwords determinist.
- **Alte limbi** decât RO/HU/EN — se adaugă seturi când un tenant le cere.
- **Histerezis la switch-back** (a nu flippa limba pe un singur mesaj străin) — v1 persistă
  la fiecare semnal clar; rafinare ulterioară dacă apare nevoia.
- **Traducerea răspunsurilor** — agentul răspunde în limba clientului din prompt; separat.
- **Detecție pe voce/poze** (media routing) — alt task (STT/Vision).

## Definition of Done
- [ ] `detect_language("szeretnék egy arckrémet", ["ro","hu","en"])` == `"hu"` (test).
- [ ] `detect_language("do you have face cream", ["ro","hu","en"])` == `"en"` (test).
- [ ] `detect_language("caut o cremă de față", ["ro","hu","en"])` == `"ro"` (test).
- [ ] Limbă detectată în afara `supported` (ex. HU dar tenant `["ro"]`) → None (test).
- [ ] Semnal ambiguu / mesaj scurt fără stopwords → None (nu ghicește) (test).
- [ ] `language_stage`: detectat ≠ curent → `ctx.language` actualizat + `set_conversation_locale`
  apelat + `language_detected` emis (test).
- [ ] Tenant mono-lingv (`supported`=1) → `language_stage` no-op, fără apel DB (test).
- [ ] `ruff check . && ruff format . && pytest -x -q` verde.

## Test Cases
**Happy Path:**
1. `language_stage` cu business `["ro","hu","en"]`, mesaj HU → `ctx.language="hu"`,
   `set_conversation_locale` mock apelat cu `"hu"`, event `language_detected{from:ro,to:hu}`.
2. `detect_language` RO/HU/EN pe propoziții clare → limba corectă.

**Edge Cases:**
1. Mesaj scurt fără stopwords („ok", „:)") → None → `language_stage` no-op (limba neschimbată).
2. Detectat == limba curentă → `language_stage` nu persistă, fără event.
3. Tenant `["ro"]` (mono-lingv) → `language_stage` return imediat, `detect_language` neapelat.

**Failure Cases:**
1. `text=None`/gol → `detect_language` întoarce None (nu aruncă).
2. `set_conversation_locale` aruncă (DB) → eroarea e prinsă/loghată, `ctx.language` rămâne
   setat pentru turul curent (best-effort persist).

> Teste pur unit: `detect_language` (fără I/O); `language_stage` cu `set_conversation_locale`
> monkeypatch-uit. ZERO LLM, ZERO apeluri reale.

## Cost
**$0** — detector determinist (stopwords), niciun apel extern. Beneficiu: lookup-urile
locale-keyed (cache/faqs/templates) cad pe limba reală → mai puține miss-uri și răspunsuri
în limba greșită. Latență adăugată: neglijabilă (set lookup pe câteva zeci de cuvinte).
