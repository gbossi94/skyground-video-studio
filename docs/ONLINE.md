# Metterlo online

Due forme di deploy, entrambe descritte da un blueprint nel repository.

| | `render.yaml` (attiva) | `deploy/render-split.yaml` |
|---|---|---|
| Servizi | uno: API e worker insieme | web + worker separati |
| Media | disco persistente da 20 GB | Cloudflare R2 |
| Serve procurarsi | niente | bucket R2 e token |
| Quando | adesso | quando il rendering merita una macchina sua |

Il motivo per cui la prima forma esiste: **un disco Render si monta su un
servizio solo**. Due servizi separati non possono condividere i media, quindi la
separazione web/worker obbliga a uno storage a oggetti. Finché il volume di
lavoro sta su una macchina, un container solo con un disco è più semplice e non
dipende da nessun altro fornitore.

## Primo deploy

1. Su Render, **New → Blueprint**, scegli `gbossi94/skyground-video-studio` e il
   branch `claude/phase-2-smart-cut`. Render legge `render.yaml` e crea il
   servizio con il disco e il database già collegati.
2. Alla fine, apri l'URL del servizio. Non esiste ancora nessun account: la
   schermata ti chiede di crearne uno, quello diventa l'amministratore e riceve i
   progetti del checkout. Da quel momento la porta è chiusa.
3. Gli altri si aggiungono su invito:
   `python studio.py projects grant beauty-centers-growth-01 --email collega@skyground.online --role editor`

Non c'è nessun valore da inserire a mano: il segreto lo genera Render, la
connessione al database la collega il blueprint, la trascrizione gira dentro il
worker senza chiavi.

## Cosa costa

| Risorsa | Piano | Perché |
|---|---|---|
| `skyground-studio` | standard | la trascrizione locale vuole memoria: con `starter` (512 MB) il modello non ci sta |
| `skyground-db` | basic-256mb | utenti, revisioni, piani di taglio: dati piccoli |
| disco | 20 GB | girato, proxy e render; si alza senza perdere nulla, non si abbassa |

Per scendere di piano sul servizio bisogna spostare la trascrizione su un'API
(`SKYGROUND_TRANSCRIPTION_PROVIDER=deepgram` più la chiave): a quel punto il
container non deve più far girare un modello.

## Un amministratore senza passare dalla pagina

Normalmente il primo che apre l'URL crea l'account e chiude la porta. Quando
serve invece un account che esiste *prima* — per un operatore che deve rientrare,
o per un agente che lavora al deployment e deve usare le API dello studio invece
di aggirarle — bastano due variabili:

```
SKYGROUND_ADMIN_EMAIL=servizio@skyground.online
SKYGROUND_ADMIN_PASSWORD=<una password lunga>
```

All'avvio il container crea quell'amministratore se non esiste. Se esiste già
non lo tocca: né la password, né i permessi. Impostare queste variabili su uno
studio in funzione non può quindi portare via a nessuno il proprio account, e
toglierle non cancella niente — per revocare l'accesso si disattiva l'account:

```bash
python3 studio.py users deactivate servizio@skyground.online
```

## Portare dentro il girato

I media non stanno in Git. Dopo il primo deploy il progetto c'è ma il suo girato
no, e l'editor lo dice: *«il girato non è ancora stato ascoltato»*.

Dal tuo Mac, con il checkout e gli asset già scaricati (`studio.py pull`), in
un comando solo:

```bash
./deploy/carica-girato.sh https://<host>
```

Chiede email e password, fa i tre passi qui sotto e verifica che i byte
arrivati siano quelli partiti. Il resto di questa sezione è cosa fa, per
quando serve farlo a mano.

Prima il cookie di sessione, che serve a tutte le chiamate tranne una:

```bash
HOST=https://<host>
PROGETTO=beauty-centers-growth-01

curl -sS -c cookie.txt -X POST "$HOST/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email": "tu@skyground.online", "password": "..."}'
```

Poi tre passi. Il secondo non porta credenziali: la firma nell'URL è il
permesso, e vale pochi minuti per quella chiave e per il solo metodo `PUT`.

```bash
# 1. l'URL firmato
URL=$(curl -sS -b cookie.txt -X POST "$HOST/api/projects/$PROGETTO/assets/upload-url" \
  -H 'Content-Type: application/json' \
  -d '{"path": "assets/raw.mov"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')

# 2. i byte, che non passano dall'API
curl -sS -X PUT "$URL" \
  -H 'Content-Type: video/quicktime' \
  --upload-file projects/$PROGETTO/assets/raw.mov

# 3. registrare l'oggetto appena caricato
curl -sS -b cookie.txt -X POST "$HOST/api/projects/$PROGETTO/assets" \
  -H 'Content-Type: application/json' \
  -d "{\"key\": \"projects/$PROGETTO/assets/raw.mov\", \"kind\": \"raw\"}"
```

Per un file piccolo esiste anche la via breve, che fa tutto in una chiamata
sola passando però dall'API:

```bash
curl -sS -b cookie.txt -X PUT "$HOST/api/projects/$PROGETTO/assets/assets/raw.mov" \
  -H 'Content-Type: video/quicktime' -H 'X-Skyground-Kind: raw' \
  --upload-file projects/$PROGETTO/assets/raw.mov
```

Nessuna delle due tiene il file in memoria: il corpo della richiesta viene
scritto su disco mentre arriva. Il limite è 5 GB.

Poi, dall'editor, **Analizza il girato**: il worker trascrive, il motore propone
il taglio e le ambiguità diventano domande.

## Verifiche dopo il deploy

```bash
curl https://<host>/healthz
# {"status":"ok","environment":"production","storage":"local","authMode":"password"}
```

- il pannello storico risponde su `/`, l'editor del montaggio su `/app`;
- nei log del servizio compare `skyground: applico le migrazioni` e poi
  l'avvio del worker;
- un utente senza membership riceve `404` sul progetto, non `403`;
- il cookie di sessione ha `Secure` e `HttpOnly`.

## Cosa resta scoperto

- Un solo servizio: il rendering occupa la stessa macchina che serve le pagine.
- Nessun backup oltre a quello del piano PostgreSQL scelto.
- Il disco non si riduce, si può solo ampliare.
- Nessun dominio personalizzato: Render assegna `*.onrender.com`.
