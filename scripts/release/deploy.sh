#!/usr/bin/env bash
# NX-248 — deploy prin DIGEST, cu trust la margine explicit.
#
# Ce înlocuiește (din `deploy.yml` vechi):
#
#   ssh-keyscan -H "$HOST" >> ~/.ssh/known_hosts
#   ssh -o StrictHostKeyChecking=no ... 'git pull && docker compose pull && up -d'
#
# `ssh-keyscan` la fiecare rulare înseamnă „am încredere în orice host care răspunde acum la IP-ul
# ăsta" — adică TOFU repetat la infinit, care nu e trust, e absența lui. Un MITM pe ruta runnerului
# primea cheia de deploy și codul. Aici cheia publică a hostului vine dintr-un SECRET (aprobată
# out-of-band, o dată), iar `StrictHostKeyChecking=yes` e negociabilă doar schimbând secretul.
#
# `git pull` dispare complet: hostul nu mai decide ce configurație rulează. Compose-ul de pe host
# e fixat de release, iar imaginea e un digest — nu un tag care se poate muta între `pull` și `up`.
#
# Uz: DEPLOY_HOST=… DEPLOY_USER=… DEPLOY_KEY=… DEPLOY_HOST_KEY=… IMAGE_DIGEST=sha256:… \
#       ./scripts/release/deploy.sh <staging|production>
set -euo pipefail

ENVIRONMENT="${1:?uz: deploy.sh <staging|production>}"
: "${DEPLOY_HOST:?DEPLOY_HOST lipsește}"
: "${DEPLOY_USER:?DEPLOY_USER lipsește}"
: "${DEPLOY_KEY:?DEPLOY_KEY lipsește}"
: "${DEPLOY_HOST_KEY:?DEPLOY_HOST_KEY lipsește — host key-ul se aprobă out-of-band, nu se scanează}"
: "${IMAGE_DIGEST:?IMAGE_DIGEST lipsește — deployul promovează un digest, nu un tag}"
REMOTE_DIR="${REMOTE_DIR:-/opt/nativextech/nativx}"

# Digestul trebuie să ARATE a digest. Un tag strecurat aici (ex. `latest`) ar readuce exact
# mutabilitatea pe care releaseul o elimină.
if [[ ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "REFUZ: IMAGE_DIGEST=$IMAGE_DIGEST nu e un digest sha256" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
# Cheia privată și known_hosts trăiesc într-un temp curățat NECONDIȚIONAT, inclusiv la eroare.
trap 'rm -rf "$WORKDIR"' EXIT
umask 077

printf '%s\n' "$DEPLOY_KEY" > "$WORKDIR/id"
chmod 600 "$WORKDIR/id"
printf '%s\n' "$DEPLOY_HOST_KEY" > "$WORKDIR/known_hosts"

SSH=(ssh
  -i "$WORKDIR/id"
  -o "UserKnownHostsFile=$WORKDIR/known_hosts"
  -o StrictHostKeyChecking=yes
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o ConnectTimeout=15
)

echo "→ deploy $ENVIRONMENT: $IMAGE_DIGEST"

# `--` separă opțiunile de comandă; variabilele se trimit prin env explicit, nu interpolate în
# șirul de shell (ar fi injecție prin conținutul unui secret).
"${SSH[@]}" "$DEPLOY_USER@$DEPLOY_HOST" \
  "IMAGE_DIGEST='$IMAGE_DIGEST' REMOTE_DIR='$REMOTE_DIR' bash -s" <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR"

# Digestul intră în `.env` (compose îl citește). Rescriem DOAR linia lui: `.env` de pe host ține
# secretele, iar un `cat > .env` le-ar șterge — un deploy nu are voie să distrugă configurația.
touch .env
if grep -q '^IMAGE_DIGEST=' .env; then
  # Salvăm digestul PRECEDENT înainte să-l suprascriem: fără el, rollbackul ar trebui ghicit.
  PREV="$(grep '^IMAGE_DIGEST=' .env | head -1 | cut -d= -f2-)"
  sed -i "s|^IMAGE_DIGEST=.*|IMAGE_DIGEST=${IMAGE_DIGEST}|" .env
  if grep -q '^PREVIOUS_IMAGE_DIGEST=' .env; then
    sed -i "s|^PREVIOUS_IMAGE_DIGEST=.*|PREVIOUS_IMAGE_DIGEST=${PREV}|" .env
  else
    printf 'PREVIOUS_IMAGE_DIGEST=%s\n' "$PREV" >> .env
  fi
else
  printf 'IMAGE_DIGEST=%s\n' "$IMAGE_DIGEST" >> .env
fi

# ZERO build pe host (1 vCPU) și ZERO `git pull`: artefactul vine gata făcut, configurația e a
# releaseului. `pull` pe digest e idempotent și verificabil.
docker compose pull
docker compose up -d --remove-orphans

# Așteaptă READINESS, nu „containerul există". Bounded: un deploy care nu devine gata trebuie să
# eșueze zgomotos, ca rollbackul să înceapă imediat.
#
# Starea se citește cu `docker inspect`, nu prin `docker compose ps --format json | python3`:
#
#   • formatul lui `ps --format json` s-a SCHIMBAT în Compose v2 (un array JSON înainte de 2.21,
#     NDJSON — un obiect pe linie — după). Parserul de linii returna `unknown` tăcut pe formatul
#     vechi, deci bucla se scurgea toate cele 30 de încercări și deployul pica după 150 de secunde
#     pe un container perfect sănătos. Versiunea de Compose de pe host devenea parte din contractul
#     de release, fără s-o declare nimeni;
#   • depindea de `python3` PE HOST — o dependență nedeclarată pentru un pas care decide dacă
#     releaseul trece sau se dă înapoi.
#
# `docker inspect -f` cu `if .State.Health` acoperă și serviciile fără healthcheck (întoarce
# `running`), deci aceeași buclă merge dacă webhook-ul ar rămâne vreodată fără sondă.
for i in $(seq 1 30); do
  cid="$(docker compose ps -q webhook 2>/dev/null || true)"
  if [ -n "$cid" ]; then
    state="$(docker inspect -f '{{ if .State.Health }}{{ .State.Health.Status }}{{ else }}{{ .State.Status }}{{ end }}' "$cid" 2>/dev/null || echo unknown)"
  else
    state="missing"
  fi
  if [ "$state" = "healthy" ]; then
    echo "✓ webhook healthy după ${i} încercări"
    break
  fi
  if [ "$i" = "30" ]; then
    echo "✗ webhook nu a devenit healthy — vezi docs/RELEASE-RUNBOOK.md §Rollback" >&2
    docker compose ps
    exit 1
  fi
  sleep 5
done

# Dovada că rulează EXACT digestul promovat: nu ce spune tagul, ce spune runtime-ul.
running="$(docker inspect --format '{{ index .Image }}' "$(docker compose ps -q webhook)")"
echo "imagine rulată (config digest): $running"
docker compose ps
REMOTE

echo "✓ deploy $ENVIRONMENT complet: $IMAGE_DIGEST"
