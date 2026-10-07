# Deploy su Render — dati necessari

Questa fase **non crea nessuna risorsa cloud e non richiede nessun segreto**: il
blueprint e le immagini sono pronti, ma vanno applicati da te. Qui c'è l'elenco
esatto di ciò che serve, e nient'altro.

## 1. Cosa devi creare tu, prima del deploy

| Risorsa | Dove | Perché |
|---|---|---|
| Bucket R2 | Cloudflare → R2 | raw, proxy, angoli, audio, render |
| API token R2 con lettura e scrittura sul bucket | Cloudflare → R2 → Manage API Tokens | l'applicazione firma gli URL e carica i file |
| Blueprint Render | Render → New → Blueprint, puntato a questa repository | crea web, worker, database e coda |

Il database PostgreSQL e l'istanza key-value li crea il blueprint; non serve
prepararli a mano.

## 2. Valori da inserire nel dashboard Render

Il gruppo di variabili `skyground-shared` chiede questi quattro valori, marcati
`sync: false` nel blueprint proprio perché non devono stare in Git:

| Variabile | Da dove si prende | Esempio di forma |
|---|---|---|
| `SKYGROUND_S3_ENDPOINT` | Cloudflare R2 → dettagli del bucket → S3 API | `https://<account-id>.r2.cloudflarestorage.com` |
| `SKYGROUND_S3_BUCKET` | nome del bucket | `skyground` |
| `SKYGROUND_S3_ACCESS_KEY_ID` | token R2 | stringa di 32 caratteri |
| `SKYGROUND_S3_SECRET_ACCESS_KEY` | token R2, mostrato una sola volta | stringa di 64 caratteri |

Sul servizio web, facoltativa:

| Variabile | Quando serve |
|---|---|
| `SKYGROUND_ALLOWED_ORIGINS` | solo se il pannello viene servito da un dominio diverso dall'API. Lista separata da virgole, per esempio `https://studio.skyground.online` |

Generate o collegate da Render, **non devi inserirle**:

| Variabile | Origine |
|---|---|
| `SKYGROUND_SECRET_KEY` | `generateValue: true` |
| `SKYGROUND_DATABASE_URL` | connessione del database `skyground-db` |
| `SKYGROUND_ENV`, `SKYGROUND_STORAGE_BACKEND`, `SKYGROUND_AUTH_MODE`, `SKYGROUND_COOKIE_SECURE`, `SKYGROUND_S3_REGION` | valori fissi nel blueprint |
| `PORT` | Render |

Se una di queste manca o è incoerente, il processo **non parte** e dice quale:
`Settings.check_deployable()` rifiuta la produzione con SQLite, con storage su
disco, con autenticazione aperta, senza segreto esplicito o con cookie non
sicuri.

## 3. Primo avvio

Le migrazioni girano da sole (`preDeployCommand: python studio.py db upgrade`).
Poi, dalla shell del servizio web:

```bash
python studio.py users create gabriele@skyground.online --admin
python studio.py projects register --email gabriele@skyground.online
python studio.py projects grant beauty-centers-growth-01 --email collega@skyground.online --role editor
```

La password viene chiesta interattivamente. Se la shell non è interattiva, si può
passare da `SKYGROUND_NEW_PASSWORD` per la durata del comando e poi rimuoverla.

I passi successivi — caricamento dei media su R2 ed eventuale passaggio del
progetto in modalità gestita — sono in [MIGRAZIONE.md](MIGRAZIONE.md).

## 4. Verifiche dopo il deploy

```bash
curl https://<host>/healthz
# {"status":"ok","environment":"production","storage":"s3","authMode":"password"}
```

Poi, dal pannello: entrare con l'account creato, aprire il progetto, controllare
che `Progetto valido` compaia e che l'anteprima si carichi (l'anteprima arriva da
un URL firmato con scadenza 15 minuti).

Controlli utili subito dopo:

- il worker è `live` e nei log scrive `worker … avviato`;
- `POST /api/projects/<slug>/jobs` con `{"kind":"validate"}` torna `queued` e
  diventa `succeeded` entro pochi secondi;
- un utente senza membership riceve `404` sul progetto;
- il cookie di sessione ha `Secure` e `HttpOnly`.

## 5. Costo indicativo

| Servizio | Piano nel blueprint |
|---|---|
| `skyground-studio` (web) | `starter` |
| `skyground-worker` | `standard` — il rendering è il processo pesante |
| `skyground-db` | `basic-256mb` |
| `skyground-queue` | `free`, non usata in questa fase, rimovibile |
| R2 | a consumo, fuori da Render |

I piani sono un punto di partenza: il worker è l'unico che va dimensionato sul
render reale, e conviene misurarlo prima di salire.

## 6. Cosa resta scoperto in questa fase

- Nessun backup automatico oltre a quello del piano PostgreSQL scelto.
- Nessun dominio personalizzato configurato: Render assegna `*.onrender.com`.
- Nessun invito via email: gli account si creano da CLI.
- Nessuna metrica applicativa oltre ai log e a `/healthz`.
