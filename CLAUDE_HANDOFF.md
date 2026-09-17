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

## Passaggio alla sessione sul Mac (settembre 2026)

La sessione remota ha portato lo studio a montare da solo un girato caricato via UI (branch `claude/phase-2-smart-cut`, PR #2: leggila, è la storia completa). Quello che resta si fa solo su una macchina con gli editor installati, quindi la sessione continua sul Mac di Gabriele. Stato e istruzioni:

- **Produzione**: https://skyground-studio.onrender.com (Render, servizio `skyground-studio`, deploy automatico dal branch). Account di Gabriele già creato. I job (`full`) si seguono da `GET /api/projects/{id}/jobs`.
- **DaVinci Resolve — fatto sul Mac (17 settembre 2026)**: il ponte è stato eseguito dentro Resolve 21.1 (edizione free) e corretto su ciò che l'API vera rifiutava. Tre scoperte:
  1. **Su questo Mac Resolve non ha Python.** Il menu Scripts elenca solo `.lua`; `fuscript` esegue i `.py` come Lua; nessuna `libpython` viene mai caricata dal processo (verificato con `lsof`, con il log e con un finto `python3` mai invocato). `fusionscript.so` cerca l'interprete via `PYTHON3HOME`, `/usr/local/bin/python3` (qui python.org 3.13) o il suo `ResolvePython` 3.14, ma su questa installazione la registrazione del linguaggio non parte affatto, con qualunque variabile. Lo scripting esterno (`DaVinciResolveScript.scriptapp`) è solo Studio. Quindi il ponte è **in due metà**: `scripts/davinci/Skyground.py` da terminale (login, download del montaggio, `~/Movies/Skyground/latest.lua`), e `scripts/davinci/Skyground.lua` da Workspace → Scripts → Skyground (costruisce progetto e timeline con l'API di Resolve, in Lua, dal manifesto). Il Lua di Resolve è una sandbox: niente `io`, `require`, `os.execute`; ci sono `os.getenv`, `loadfile` e gli oggetti `resolve`/`project`. `build()` in Python resta, per quando Resolve avrà un Python: allora `Skyground.py` dal menu farà tutto in un clic.
  2. **Correzioni dall'API vera** (applicate a Python, Lua e test): `endFrame` di `AppendToTimeline` è esclusivo (con l'inclusivo ogni clip perdeva un frame: 3454 invece di 3478); `timelinePlaybackFrameRate` è di sola lettura; l'SRT importato nel pool arriva in timeline solo dopo `timeline:AddTrack("subtitle")`, e allora `AppendToTimeline({srt})` mette tutte le battute (74).
  3. **Esito verificato**: progetto «Skyground — Crescita centri estetici 01», timeline «Skyground» 1080×1920 a 30 fps, 24 clip per 3478 frame esatti, 74 sottotitoli su ST1, viewer che decodifica il girato HEVC. Restano nel Project Manager di Resolve anche i progetti vuoti «New Project»/«New Project 1» creati per le prove: si possono cancellare.
  Installazione sul Mac: i due file più `skyground.json` stanno in `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`; `skyground.json` punta alla produzione con `email`/`password` vuoti da compilare (Gabriele, con il suo account). Resolve legge la cartella **solo all'avvio**: dopo aver copiato o rinominato uno script va riavviato. Diagnostica: i `print` degli script vanno in Workspace → Console (F6); gli errori Lua, con messaggio, finiscono in `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/logs/ResolveDebug.txt`, e la parte Python scrive `~/Movies/Skyground/skyground.log`. Il girato locale è stato scaricato con `studio.py pull` (`gh` è autenticato); lo studio locale (`studio.py serve`, modalità single-user) fa da banco di prova senza credenziali di produzione.
- **DaVinci Resolve (com'era)**: lo studio esporta FCPXML e SRT (`GET …/export/fcpxml|srt`). Il ponte `scripts/davinci/Skyground.py` va copiato in `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/` con accanto `skyground.json` (`url`, `email`, `password`, `project` opzionale): da Workspace → Scripts → Utility → Skyground scarica l'ultimo montaggio e costruisce progetto e timeline via API di Resolve. **Non è mai stato eseguito dentro Resolve**: i test (`tests/test_davinci_bridge.py`) usano oggetti finti. Primo incarico: lanciarlo, leggere la console di Resolve, correggere ciò che l'API vera rifiuta (nomi dei setting, `AppendToTimeline` con i dizionari `startFrame/endFrame`, l'import dell'SRT), e poi tenere la logica pura nello script — che deve restare un solo file, sola libreria standard, perché Resolve lo esegue col Python di sistema.
- **CapCut** (9.4.0-beta6): `GET …/export/capcut` scrive una bozza nella forma della bozza campione caricata nel workspace (`capcut/sample/`, `PUT /api/capcut/sample` con lo zip della cartella). CapCut la elencava ma la prima versione non si apriva; la seconda (base = bozza vera svuotata, file di corredo copiati) è da verificare aprendola. Se non si apre: confrontare la cartella generata con `0909 (1)` in `~/Movies/CapCut/User Data/Projects/com.lveditor.draft`, e cercare il log di CapCut sotto `~/Library/Containers/com.lemon.lvoverseas/Data/Movies/CapCut/User Data/`. La logica sta in `skyground/core/capcut.py`, i test in `tests/test_capcut.py` (campione finto scritto a mano: la bozza vera non entra mai nel repository).
- **Credenziali da ruotare** (esposte in chat nella sessione remota): chiave Anthropic, chiave OpenAI (`SKYGROUND_OPENAI_API_KEY`), password dell'account di servizio `claude@skyground.online`, `SKYGROUND_SECRET_KEY`, e la password temporanea di Gabriele.
- **Aperto**: il nastro effetti `sfx.wav` non è ri-temporizzabile (serve una lista di eventi); il piano generato per `-01` non è mai stato applicato alla timeline approvata (scelta di Gabriele).
