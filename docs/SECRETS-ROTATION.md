# Secrete: inventar, livrare, rotație — NX-248

## 1. Inventar

Fiecare secret, cine îl are, ce se strică la rotație. Coloana „cine" e cea importantă: un secret
pe care îl are un proces care nu-l folosește e o suprafață de atac gratuită.

| Secret | Cine îl are | Rotabil fără downtime | Ce se strică |
|---|---|---|---|
| `SUPABASE_DB_URL` (control plane) | webhook, worker, dispatcher, scheduler | da (restart rulant) | rezolvarea canal→tenant |
| `DATABASE_URL_BOT` (`bot_runtime`) | webhook, worker, dispatcher | da | tot ce e tenant-scoped |
| **`DATABASE_URL_MIGRATION` (DDL)** | **DOAR jobul `migrate`** | da | migrările |
| `REDIS_PASSWORD` | toate + redis | nu (restart coordonat) | coada, rate limit, lease-uri |
| `OPENAI_API_KEY` | webhook, worker | da | triaj + agent |
| `WEB_ACTION_KEYS` (NX-236) | webhook, worker | **da, cu overlap** | butoanele deja afișate |
| `WEB_TURN_FINGERPRINT_SECRET` | webhook | atenție (§4) | idempotența acceptului |
| `WEB_FEEDBACK_PROMPT_SECRET` | webhook, worker | atenție (§4) | „un vot per prompt" |
| `OBSERVABILITY_TRACE_SECRET` | toate | da | corelarea traceurilor vechi |
| `OPS_HEALTH_TOKEN` | webhook | da | `/health/detail` |
| `META_*` | webhook, dispatcher | da | WhatsApp (ÎNGHEȚAT) |
| `TELEGRAM_BOT_TOKEN` | telegram-poller | da | Telegram (ÎNGHEȚAT) |
| `VPS_SSH_KEY` + `VPS_HOST_KEY` | GitHub Environments | da | deployul |
| `GITHUB_TOKEN` (GHCR) | CI, efemer per rulare | automat | push-ul imaginii |
| `RELEASE_MANIFEST_KEY` | CI | da | semnătura manifestului |

**Regula de aur:** `DATABASE_URL_MIGRATION` nu apare în `.env`. Trăiește în `.env.migrate`
(gitignored, mod 0400), montat exclusiv în serviciul `migrate`. Un proces care nu-l are nu poate
face DDL nici din greșeală, nici compromis. Verificat mecanic:
`tests/test_ops_release.py::test_credentialul_de_migrare_nu_ajunge_in_runtime`.

---

## 2. Livrare: de ce fișier și nu `.env`

Un `.env` citit ca `env_file` ajunge în **mediul containerului**. De acolo e vizibil în:

- `docker inspect <container>` (oricine e în grupul `docker`),
- `/proc/<pid>/environ` (orice proces din container),
- orice dump de mediu făcut de o bibliotecă de erori.

Un fișier montat read-only cu mod 0400 n-are niciuna dintre ele: nu e în env, nu e în `inspect`,
iar procesul îl citește o dată la boot.

Suportul e în `src/config.py` (`FileSecretsSource`) și în `scripts/migrate.py`:

```bash
OPENAI_API_KEY_FILE=/run/secrets/openai_api_key
DATABASE_URL_BOT_FILE=/run/secrets/database_url_bot
```

```yaml
# în docker-compose.prod.yml, pe serviciul care chiar are nevoie
volumes:
  - ./secrets/openai_api_key:/run/secrets/openai_api_key:ro
```

**Setarea AMBELOR forme (`X` și `X_FILE`) oprește procesul.** Deliberat: „fișierul câștigă" pare
prietenos până când cineva rotește secretul în fișier, uită env-ul vechi în `.env`, și jumătate
din flotă rulează cu credentialul revocat — tăcut, fiindcă ambele „funcționează". Un fișier
inexistent e tot eroare de boot: fail-closed, nu „pornim fără cheie".

`.env` root-readable rămâne **tranziție documentată** (datoria D2 din
[PRODUCTION-READINESS.md](PRODUCTION-READINESS.md)), nu ținta finală.

---

## 3. Rotație de rutină

### 3.1 Cheile de acțiuni (NX-236) — singurele cu overlap real

Inelul de chei: prima EMITE, toate VERIFICĂ.

```bash
# 1. generează
python -c "import base64,os;print('k2:'+base64.b64encode(os.urandom(32)).decode())"

# 2. cheia NOUĂ în FAȚĂ, cea veche rămâne
WEB_ACTION_KEYS=k2:…,k1:…

# 3. deploy; ține overlapul ≥ WEB_ACTION_TTL_S (implicit 1800s)
# 4. abia apoi scoate k1
WEB_ACTION_KEYS=k2:…
```

Scoaterea lui `k1` înainte de expirare invalidează butoanele deja afișate: clientul primește
`action_invalid` (copy server-owned — nu tăcere, dar nici comportamentul dorit).

### 3.2 Credentiale DB

```bash
# 1. rol nou / parolă nouă la provider
# 2. pune-o în secret store, actualizează .env(.migrate) sau fișierul montat
# 3. restart RULANT: webhook → worker → dispatcher → scheduler
# 4. verifică ÎNAINTE de a revoca vechea parolă
curl -s -H "X-Ops-Token: $OPS_HEALTH_TOKEN" localhost:8000/health/detail \
  | python -c "import json,sys; d=json.load(sys.stdin); print([p for p in d['probes'] if p['component'].startswith('postgres')])"
# aștept: postgres_control ok, postgres_tenant ok cu role_ok=true
# 5. revocă vechea parolă
```

`postgres_tenant` verifică și ROLUL efectiv: dacă cineva pune din greșeală un DSN privilegiat,
sonda o spune (`auth_failed`, `role_ok: false`) în loc să ruleze cu RLS ocolit.

### 3.3 Cheia SSH de deploy

```bash
ssh-keygen -t ed25519 -f deploy_key_new -C "nativx-deploy-2026-08"
# publică pe VPS în ~/.ssh/authorized_keys al contului de deploy (NU root)
# privată în GitHub Environments (VPS_SSH_KEY)
# rulează un deploy de test pe staging ÎNAINTE de a scoate cheia veche
```

`VPS_HOST_KEY` (cheia publică a HOSTULUI) se schimbă doar la reinstalarea hostului. Se obține
out-of-band, de pe consola providerului — **niciodată** cu `ssh-keyscan` de pe runner: asta ar fi
„am încredere în orice host care răspunde acum la IP-ul ăsta", adică absența trustului.

---

## 4. Secrete care NU se rotesc liber

Trei secrete sunt și **chei de determinism**. Rotația lor nu e o schimbare de credential, e o
schimbare de identitate a datelor deja scrise:

| Secret | Ce derivă | Ce se întâmplă la rotație |
|---|---|---|
| `WEB_TURN_FINGERPRINT_SECRET` | amprenta de idempotency a acceptului | un retry al aceluiași request devine tur NOU (dublare) |
| `WEB_FEEDBACK_PROMPT_SECRET` | `feedback_prompt_id` (HMAC pe `turn_id`) | „un vot per prompt" devine „un vot per rotație" |
| `OBSERVABILITY_TRACE_SECRET` | trace-id derivat din `turn_id` | traceurile vechi nu se mai corelează (doar operațional) |

Primele două se rotesc doar într-o fereastră fără trafic activ, sau deloc. Dacă ajung compromise,
paguba e limitată (nu dau acces la date), deci **prioritatea e corectitudinea, nu viteza** — exact
invers față de o cheie de API.

---

## 5. Incident: secret compromis

Ordinea contează. Postmortemul e ultimul, nu primul.

1. **Revocă la sursă** (provider, OpenAI, GitHub). Un secret „scos din config" dar nerevocat e în
   continuare valabil.
2. **Rotește** conform §3. Pentru chei de acțiuni: scoaterea din inel e instantanee și deliberată.
3. **Verifică**: `/health/detail`, apoi un smoke (`scripts/release/smoke_web_v2.py`).
4. **Caută răspândirea** — unde a mai ajuns valoarea:
   ```bash
   docker history --no-trunc <imagine> | grep -i <fragment>
   docker inspect <container> | grep -i <fragment>
   git log -p --all -S '<fragment>' | head          # istoricul, nu doar HEAD
   ```
   `.git` nu mai intră în contextul de build (`.dockerignore`), deci imaginile NOI nu mai poartă
   istoricul. Cele vechi îl poartă.
5. **Abia apoi** postmortem: cum a ieșit, ce poartă a lipsit, ce test o adaugă.

---

## 6. Ce împiedică sistemul singur

- **Amprenta de config nu conține secrete.** `config_revision` (`src/ops/build_info.py`) e
  calculată doar peste câmpurile ne-secrete; rotația unei chei nu mișcă revizia, deci nu apare în
  manifest ca „schimbare de config" care invită pe cineva să caute diferența.
- **Inventarul e o poartă de test.** `tests/test_ops_build_and_secrets.py` enumeră TOATE câmpurile
  din `Settings` și fixează verdictul fiecăruia. Un câmp nou care arată a secret pică testul până
  când cineva decide explicit.
- **Health-ul nu spune nimic.** Răspunsul public are status/release/config/schema/timestamp.
  `/health/detail` cere token, îl compară în timp constant, iar fără token configurat răspunde
  **404** (nu 401 — un 401 confirmă că ruta există).
- **Imaginea e verificată.** `scripts/release/image_contract.py` caută tipare de secret în
  `docker history`, ENV și label-uri, plus un canary injectat de CI. Poarta chiar poate pica: pe o
  imagine construită intenționat greșit a raportat toate cele patru probleme.
- **`ARG`/`ENV` cu secrete nu trec.** Dockerfile-ul primește doar `RELEASE_SHA` și `BUILT_AT` —
  publice prin definiție (sunt în git).
