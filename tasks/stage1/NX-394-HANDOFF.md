# Predare NX-394 (agentul unic) — de citit primul într-o sesiune nouă

Scris pe 2026-10-09, la finalul sesiunii în care s-a luat decizia. Planul complet:
[`NX-394.md`](NX-394.md). Prototipul și măsurătorile: [`NX-393.md`](NX-393.md).

## Decizia
Kernelul (`kernel.v10.0`) se înlocuiește cu UN agent cu unelte. Modelul înțelege, decide și scrie tot
textul; codul dă fapte (unelte), face acțiunile, aplică regulile dure și verifică adevărul. Codul
vechi (~26.000 de linii: kernel, drumurile fixe v1, creierul unic) se șterge abia la 100% pe trafic.
Varianta cu doi agenți (agent + agent de căutare) se MĂSOARĂ în Faza 1 lângă un singur agent.

## Ce s-a măsurat (de ce)
- `mixed-2026-10-09` (30 de conversații nevăzute, scrise de un agent independent), kernelul pe release
  `1e94649`: m1–m16 rulate, 37/58 corecte, 8 greșite. Pe 11 din 16 ture greșite (din două rulări)
  modelul înțelesese corect, iar codul a stricat (text corect aruncat, drum lipsă, decizie peste model).
- Prototipul agentului (`scripts/sim/agent_a_prototype.py`) pe aceleași 58 de ture: 40 corecte, 4
  greșite, p50 ~5 s față de 15-17 s. Rapoartele locale: `reports/nx393/agent-mixed-2026-10-09-*`.
- Unde a pierdut agentul (toate ale PROTOTIPULUI): nu întreabă zona unei rutini neclare; inventează
  identificatori în loc de handle-urile `P1…`; un preț inventat (prins de poartă); oferă „vrei să
  caut?” în loc să caute; carduri duplicate pentru nuanțe (produse separate cu același nume scurt).

## Starea codului (ramuri și PR-uri)
- `main` = `1e94649` + PR-urile intrate azi: #569 (NX-390), #570 (NX-390b), #571 (setul mixed +
  runner), #574 (NX-391, totalul setului), #575 (NX-392, turul mixt). Toate sunt în producție.
- PR #576 (draft), ramura `feat/NX-393-single-agent-prototype`: prototipul + testele + cardul NX-393.
  Nu s-a făcut merge (e un script; se poate face merge oricând, nu atinge producția).
- Ramura `plan/NX-394-single-agent`: cardul acesta și planul. Fără PR încă.

## Starea producției
- `bot.nativextech.com` rulează `1e94649` (`/health/startup`), kernelul 100% pe `sole-ro`.
- `ROUTINE_FAMILY_QUESTION_ENABLED` e încă aprins (verdictul NX-389 a fost NO-GO, apoi NX-390/390b
  au reparat cauzele; un verdict nou nu s-a rulat).
- **Incident deschis, necunoscut ca rădăcină:** de trei ori azi `bot.*` a răspuns 503/404 de la
  Traefik câteva secunde până la ~2 minute, cu `EMAXCONNSESSION … pool_size: 15` la orice conexiune
  nouă. Ipoteza: poolerul Supabase plin ⇒ `/health/ready` pică ⇒ Traefik scoate singurul container.
  Logurile de pe VPS (`docker compose logs webhook …`) n-au fost citite (fără acces SSH din sesiune).
  Merită un card separat; nu e parte din NX-394.

## Faza 1 — ce e de făcut (următorul pas)
În `scripts/sim/agent_a_prototype.py` (ramura #576):
1. handle-urile ca meniu închis: `product_details.handles`, `add_to_cart.handle`, `answer.show[]` și
   `search_catalog.exclude` cu `enum` = handle-urile cunoscute în conversație (schema se reconstruiește
   pe tur);
2. regula de întrebare: o cerere care poate fi pentru față, corp, păr sau machiaj, fără zona spusă ⇒
   o singură întrebare scurtă (decizia D1 din NX-389); generic, fără exemple în română;
3. „caută singur”: instrucțiunea să nu întrebe „vrei să caut?” când poate căuta;
4. nuanța sau varianta în numele scurt al cardului (atributele `shade_code` / `shade`);
5. poarta de adevăr cu o reîncercare: la `answer` respins, `function_call_output` cu motivul și încă
   o rundă;
6. varianta B pentru comparație: unealta `find_products(cerere)`, care rulează un sub-agent de căutare
   (aceleași unelte de citire, plafon 3 runde) și întoarce produsele potrivite cu motivul.
Apoi rularea (o pornește Adi, credite): cele 16 conversații din `mixed`, `routines-2026-10-09`,
`fresh-2026-10-07`, A și B. Pentru `routines` și `fresh` trebuie și raportul kernelului pe release-ul
curent (`scripts/sim/prod_set_run.py --set … --yes`). Regula G1 e în NX-394.

## Capcane (au costat timp azi)
- **Poolerul Supabase (15 sesiuni) e comun cu producția.** Orice script local pe DB: o singură
  conexiune (`DB_BOT_POOL_MAX=1`, `DB_ADMIN_POOL_MAX=1`), conversațiile pe rând, niciodată în timpul
  unui deploy.
- **Importul circular:** în scripturi, `import src.conversation` ÎNAINTEA lui `src.db.queries.catalog`.
- **`prod_set_run.py`** reia singur 503-urile de la proxy (#571), are `--only`; la final leagă
  conversațiile pe DB, iar asta poate pica pe pooler plin (turele rămân salvate).
- Adi rulează în **PowerShell**: `$env:VAR="x"; comanda`. Rulările cu model le pornește Adi.
- Notarea turelor (corect / parțial / greșit pe `expect`) o face Claude, nu e oarbă; Adi citește un
  eșantion.
- `gpt-6-luna` cu efort `low` a raportat ~0 tokeni de raționament: de verificat înainte de Faza 2.
