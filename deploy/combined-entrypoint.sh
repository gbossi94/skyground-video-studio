#!/usr/bin/env bash
# Start the worker beside the API, and make the pair behave like one process:
# if either half dies the container exits, so Render restarts a whole studio
# rather than leaving an API with nothing to run its jobs.
set -euo pipefail

echo "skyground: applico le migrazioni"
python studio.py db upgrade

# Crea l'amministratore descritto dall'ambiente, se non esiste già. Senza
# SKYGROUND_ADMIN_EMAIL e SKYGROUND_ADMIN_PASSWORD non fa niente, e non tocca
# mai un account che esiste: l'istanza resta rivendicabile dalla pagina.
python studio.py users ensure

# The disk is mounted empty on first boot; the media directory has to exist
# before either half writes to it.
mkdir -p "${SKYGROUND_STORAGE_ROOT:-/var/skyground/storage}"

python studio.py worker &
WORKER=$!

uvicorn skyground.asgi:app \
    --host 0.0.0.0 --port "${PORT:-10000}" \
    --proxy-headers --forwarded-allow-ips='*' &
API=$!

terminate() {
    echo "skyground: arresto richiesto"
    kill "$WORKER" "$API" 2>/dev/null || true
    wait || true
    exit 0
}
trap terminate TERM INT

# Whichever half stops first takes the container down with it.
wait -n
STATUS=$?
echo "skyground: un processo è terminato (codice $STATUS), chiudo il container"
kill "$WORKER" "$API" 2>/dev/null || true
exit "$STATUS"
