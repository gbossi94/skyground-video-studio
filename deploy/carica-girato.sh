#!/usr/bin/env bash
# Carica un file pesante su uno studio online, in un comando solo.
#
#   ./deploy/carica-girato.sh https://studio.example.com beauty-centers-growth-01
#
# Fa i tre passi che servono — accesso, URL firmata, trasferimento — e in più
# registra l'oggetto e verifica che i byte arrivati siano quelli partiti. Se
# qualcosa non torna si ferma e dice cosa, invece di lasciare mezzo file sul
# disco e un progetto che sembra a posto.
set -euo pipefail

HOST="${1:-}"
PROGETTO="${2:-beauty-centers-growth-01}"
RELATIVO="${3:-assets/raw.mov}"

if [ -z "$HOST" ]; then
    echo "uso: $0 <https://host> [progetto] [percorso-relativo]" >&2
    exit 64
fi
HOST="${HOST%/}"

FILE="projects/$PROGETTO/$RELATIVO"
if [ ! -f "$FILE" ]; then
    echo "Non trovo $FILE." >&2
    echo "Scaricalo prima con: python3 studio.py pull $PROGETTO" >&2
    exit 66
fi

BYTE=$(wc -c < "$FILE" | tr -d ' ')
echo "File:     $FILE ($BYTE byte)"
echo "Studio:   $HOST"
echo

printf 'Email: '
read -r EMAIL
printf 'Password: '
read -rs PASSWORD
echo
echo

COOKIE=$(mktemp)
RISPOSTA=$(mktemp)
trap 'rm -f "$COOKIE" "$RISPOSTA"' EXIT

# `python3 -c` invece di jq: sul Mac jq non c'è, python sì.
campo() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"; }

echo "1/4 accesso"
if ! curl -sS -f -c "$COOKIE" -X POST "$HOST/api/auth/login" \
        -H 'Content-Type: application/json' \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": sys.argv[2]}))' "$EMAIL" "$PASSWORD")" \
        -o "$RISPOSTA"; then
    echo "Accesso rifiutato: $(cat "$RISPOSTA")" >&2
    exit 1
fi

echo "2/4 chiedo l'URL firmata"
curl -sS -f -b "$COOKIE" -X POST "$HOST/api/projects/$PROGETTO/assets/upload-url" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"path": sys.argv[1]}))' "$RELATIVO")" \
    -o "$RISPOSTA"
URL=$(campo url < "$RISPOSTA")
CHIAVE=$(campo key < "$RISPOSTA")
[ -n "$URL" ] || { echo "Nessuna URL nella risposta: $(cat "$RISPOSTA")" >&2; exit 1; }

# Il tipo giusto conta: il worker decide da lì se serve un proxy riproducibile
# nel browser.
case "$RELATIVO" in
    *.mov) TIPO="video/quicktime" ;;
    *.mp4) TIPO="video/mp4" ;;
    *.wav) TIPO="audio/wav" ;;
    *.m4a) TIPO="audio/mp4" ;;
    *)     TIPO="application/octet-stream" ;;
esac

echo "3/4 trasferimento ($TIPO) — l'URL firmata scade fra pochi minuti"
curl -# -f -X PUT "$URL" -H "Content-Type: $TIPO" --upload-file "$FILE" -o "$RISPOSTA"
CARICATI=$(campo size < "$RISPOSTA")
if [ "$CARICATI" != "$BYTE" ]; then
    echo "Arrivati $CARICATI byte su $BYTE: trasferimento incompleto, rilancia." >&2
    exit 1
fi

echo "4/4 registrazione"
curl -sS -f -b "$COOKIE" -X POST "$HOST/api/projects/$PROGETTO/assets" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"key": sys.argv[1], "kind": "raw"}))' "$CHIAVE")" \
    -o "$RISPOSTA"
REGISTRATI=$(campo size < "$RISPOSTA")
[ "$REGISTRATI" = "$BYTE" ] || { echo "Registrato con $REGISTRATI byte invece di $BYTE." >&2; exit 1; }

echo
echo "Fatto: $BYTE byte come $CHIAVE."
echo "Ora apri $HOST/app e premi «Analizza il girato»."
