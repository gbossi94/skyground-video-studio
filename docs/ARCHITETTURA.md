# Architettura — Fase 1

## Confine tecnico proposto

La fase 1 costruisce le fondamenta cloud **senza spostare il video esistente fuori
da Git**. Il criterio con cui è stato tracciato il confine è uno solo: tutto ciò
che serve per ospitare il prodotto in modo sicuro e multiutente entra adesso;
tutto ciò che riguarda l'esperienza editoriale e la pipeline video resta alle
fasi 2 e 3.

**Dentro la fase 1**

- Applicazione web production-ready con le API attuali invariate.
- Modello dati PostgreSQL e migrazioni Alembic.
- Interfaccia di object storage con backend locale e backend R2.
- Autenticazione, ruoli per progetto, gestione sicura della configurazione.
- Cronologia revisioni con restore, rilevamento dei conflitti e audit log.
- Coda dei job, worker separato, Dockerfile e `render.yaml`.

**Fuori dalla fase 1**

- Timeline visuale, autosave, commenti (fase 2).
- Proxy, trascrizione, integrazione Higgsfield, quality gate (fase 3).
- Migrazione dei contenuti editoriali dentro il database: l'infrastruttura c'è
  ed è testata (`studio.py projects adopt`), ma il progetto approvato resta in
  modalità `workspace` finché non c'è un deployment su cui spostarlo.

## Moduli

```
studio.py                  entry point della CLI (invariata nei comandi editoriali)
skyground/
  core/                    formato del progetto video, validazione, sync, render
  config.py                configurazione da ambiente e controlli di sicurezza
  errors.py                errori di dominio, con lo status HTTP associato
  security.py              hashing delle password e token di sessione
  storage/                 interfaccia oggetti + backend locale e S3/R2
  db/                      modelli SQLAlchemy e migrazioni Alembic
  services/                regole applicative: account, progetti, documenti, asset, job
  api/                     app FastAPI, dipendenze, rotte, serializzazione
  worker/                  loop che consuma la coda
  legacy_server.py         il server originale, conservato come fallback locale
```

`skyground.core`, `skyground.config`, `skyground.errors`, `skyground.security` e
`skyground.legacy_server` usano **solo la libreria standard**. È una regola
vincolante: un checkout senza dipendenze installate deve continuare a validare,
sincronizzare e renderizzare il video. Il resto del pacchetto richiede
`requirements.txt`.

## Le due modalità di archiviazione di un progetto

| | `workspace` | `managed` |
|---|---|---|
| Sorgente di verità | i JSON nel checkout Git | le righe nel database |
| Diff e review | `git diff`, pull request | API e cronologia revisioni |
| Usato da | il video approvato, lo sviluppo locale | i progetti cloud |
| Revisioni e audit | nel database, contenuto incluso | nel database |

Questa è la scelta che rende possibile «diventare un prodotto cloud mantenendo
completamente modificabile il progetto video esistente». Il database conosce il
progetto, i suoi membri e la sua storia, ma non gli toglie il file da sotto i
piedi: `beauty-centers-growth-01` continua a essere modificato, validato e
renderizzato esattamente come prima, e ogni modifica resta un diff leggibile.

Quando un documento cambia fuori dall'applicazione — un `git pull`, un agente che
scrive direttamente il JSON — l'applicazione se ne accorge confrontando l'impronta
del contenuto e registra una revisione di tipo «modifica applicata fuori
dall'applicazione». La cronologia non mente mai, nemmeno quando non è l'app a
scrivere.

## Modello dati

```
users ──< memberships >── projects ──< documents
  │                          │            └──< revisions (snapshot immutabili)
  │                          ├──< assets      (metadati; i byte stanno su R2)
  │                          ├──< render_jobs (coda)
  └──< auth_sessions         └──< audit_events
```

Scelte di portabilità, verificate dai test che girano sia su SQLite sia su
PostgreSQL 16:

- Identificatori: stringhe esadecimali di 32 caratteri, non tipi UUID nativi.
- JSON: `JSON` con variante `JSONB` su PostgreSQL.
- Timestamp: `UTCDateTime`, che normalizza in UTC in scrittura e restituisce
  valori con fuso in lettura. Senza questo accorgimento un confronto come
  `expires_at < adesso` cambia significato passando da un motore all'altro.

## Permessi

| Permesso | viewer | editor | owner |
|---|:--:|:--:|:--:|
| leggere progetto, documenti, revisioni, asset, job | ✓ | ✓ | ✓ |
| scrivere documenti, ripristinare revisioni, caricare asset, accodare job | | ✓ | ✓ |
| gestire i membri, cambiare il progetto, cancellare asset, annullare job | | | ✓ |

Chi non è membro riceve `404`, non `403`: gli id dei progetti circolano nei link
e non devono confermare l'esistenza del lavoro di qualcun altro. Un
amministratore dell'istanza è owner ovunque, così un progetto rimasto senza owner
resta riparabile.

## Scritture e conflitti

`GET` di un documento restituisce il documento e basta — la forma che il pannello
legge da sempre — con la revisione negli header (`ETag`,
`X-Skyground-Revision`). `PUT` con `If-Match` è condizionale: se il documento è
cambiato nel frattempo la richiesta viene rifiutata con `409` e il contenuto
attuale, invece di cancellare in silenzio il lavoro di un collega. Senza header la
scrittura è incondizionata, che è ciò che fa oggi il pannello.

Il ripristino non riscrive la storia: ripristinare la revisione 3 crea la
revisione 8 con lo stesso contenuto, e il fatto che ci sia stato un ripristino
resta visibile.

## Storage

Un'unica interfaccia (`put`, `get`, `stat`, `delete`, `list`, `signed_url`) con
due implementazioni. In locale i file stanno su disco e gli URL firmati puntano
all'applicazione con un token HMAC che copre metodo, chiave e scadenza; in
produzione gli stessi URL sono presigned R2 e i byte non passano dal servizio
web. Le chiavi sono sempre `projects/<slug>/...` e vengono validate: una chiave
non può uscire dal proprio progetto.

## Coda

I job vengono presi da PostgreSQL con `SELECT … FOR UPDATE SKIP LOCKED`: è
esattamente-una-volta con un numero qualsiasi di worker e non richiede un broker.
Su SQLite lo stesso codice usa una transazione semplice, che basta a un worker
locale. Un job il cui worker muore viene rimesso in coda da `reap_stalled`. Il
`render.yaml` dichiara comunque un'istanza key-value: serve alla fase 3 per il
fan-out e gli eventi di avanzamento, non è usata adesso, e può essere rimossa
senza conseguenze.

I gestori disponibili in questa fase sono `validate`, `sync` e `render`. `proxy` e
`transcribe` sono dichiarati e falliscono con un messaggio esplicito invece di
fingere di funzionare.

## Sicurezza

- Password con `hashlib.scrypt`, parametri memorizzati dentro l'hash.
- Token di sessione ad alta entropia, salvati solo come digest: un dump del
  database non si può rigiocare come login.
- Cookie `HttpOnly`, `SameSite=Lax`, `Secure` in produzione.
- Le scritture cross-site vengono rifiutate confrontando l'header `Origin`.
- `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` e, in
  produzione, `Strict-Transport-Security`.
- L'account locale `local@skyground.local` non ha password utilizzabile e non può
  essere usato per entrare in un'istanza esposta.
- `Settings.check_deployable()` impedisce l'avvio in produzione senza segreto
  esplicito, con SQLite, con storage su disco, con autenticazione aperta o con
  cookie non sicuri.

## Compromessi consapevoli

- **Un pacchetto, non due servizi separati.** Web e worker condividono codice e
  immagine di base perché condividono il modello del progetto; si separano per
  Dockerfile e comando, non per repository.
- **Niente broker in fase 1.** Una coda su PostgreSQL è meno potente di Redis ma
  elimina un servizio, un segreto e una modalità di guasto, e il volume atteso
  (pochi render al giorno) non la mette in difficoltà.
- **boto3 invece di una firma scritta a mano.** È la scelta convenzionale per
  S3/R2 ed è testata contro un endpoint finto locale, senza credenziali reali.
- **Il server originale resta.** Sono circa 150 righe che garantiscono che il
  video sia modificabile su una macchina senza `pip install`, e servono da
  riferimento per verificare che le API non cambino.
