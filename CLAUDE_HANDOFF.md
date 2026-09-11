# Handoff dello sviluppo a Claude

## Obiettivo

Claude Code diventa l'ambiente principale per sviluppare Skyground Video Studio. La repository GitHub è la fonte condivisa; ogni modifica passa da branch e pull request. L'app deve diventare un prodotto cloud multiutente, mantenendo completamente modificabile il progetto video esistente.

## Stato consegnato

- Repository privata: `gbossi94/skyground-video-studio`.
- Applicazione locale funzionante in `studio.py` e `web/`.
- Primo progetto: `projects/beauty-centers-growth-01`.
- Dati editoriali versionati: timeline, captions, cards, angles, brand e audio.
- Media correnti nella release privata GitHub `assets-v1`, descritti e verificati da `assets.lock.json`.
- Composizione HyperFrames validata: lint, runtime, layout, motion e contrasto senza errori.
- Clone pulito, download degli asset e validazione già verificati.

## Vincoli di prodotto

- La qualità video resta prioritaria.
- Tutte le modifiche editoriali devono essere revisionabili e ripristinabili.
- Raw e render non entrano nella storia Git.
- Le credenziali non entrano mai nella repository.
- Il video esistente deve continuare ad aprirsi, sincronizzarsi e renderizzare durante la migrazione.
- La UI è destinata alle persone. Claude sviluppa il prodotto attraverso il codice e GitHub; non serve costruire una UI speciale per Claude.

## Architettura target

1. Web app e API su Render.
2. PostgreSQL per utenti, progetti, revisioni, asset metadata e render job.
3. Cloudflare R2 come object storage S3-compatible per raw, proxy, angoli, audio e render.
4. Worker separato per proxy, trascrizione, rendering e controlli media.
5. URL firmati e accesso privato agli asset.
6. Autenticazione e ruoli owner/editor/viewer.
7. Cronologia revisioni con restore e audit log.

## Ordine di implementazione

### Fase 1 — Fondamenta cloud

- Convertire il server locale in un'app web production-ready mantenendo le API attuali.
- Aggiungere modello dati e migrazioni PostgreSQL.
- Introdurre un'interfaccia object storage con backend locale e backend R2.
- Aggiungere autenticazione, autorizzazione dei progetti e gestione sicura delle configurazioni.
- Aggiungere Dockerfile e `render.yaml` per web, worker, database e coda.

### Fase 2 — Esperienza editoriale

- Dashboard progetti.
- Timeline visuale con clip, captions, motion card e angoli.
- Autosave con revisioni e conflitti espliciti.
- Upload, proxy, preview e stato dei job.
- Commenti e revisione del collega.

### Fase 3 — Pipeline video

- Job riproducibili e idempotenti.
- Rendering HyperFrames in worker isolato.
- Integrazioni Higgsfield e trascrizione tramite adapter, senza provider hardcoded nella timeline.
- Quality gate automatici su durata, audio, dimensioni, frame rate e frame campione.

## Primo incarico per Claude

Fare un audit della repository, proporre il confine tecnico della Fase 1 e implementarla su un branch dedicato senza creare risorse cloud e senza richiedere segreti. Conservare il backend locale per lo sviluppo. Aggiungere test per permessi, revisioni, storage e compatibilità del progetto esistente. Aprire una pull request in bozza con istruzioni di migrazione e lista precisa delle credenziali necessarie al deploy.

## Verifica minima obbligatoria

```bash
python3 studio.py validate
npm ci
npm run check
```

Il file `CLAUDE.md` contiene le regole editoriali permanenti del video.
