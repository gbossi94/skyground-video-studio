# Skyground Video Studio

Repository collaborativa per i video Skyground. Ogni progetto è descritto da file JSON leggibili da persone, Claude, Codex e altri agenti. I media pesanti restano fuori dalla storia Git e vengono scaricati da una release privata o, in seguito, da object storage.

## Avvio rapido

Requisiti: Python 3.11+, Node.js, FFmpeg e GitHub CLI autenticata per scaricare gli asset privati.

```bash
npm ci
python3 studio.py list
python3 studio.py validate
python3 studio.py pull beauty-centers-growth-01
python3 studio.py serve
```

La UI locale si apre su `http://127.0.0.1:4173`. La prima configurazione su un nuovo computer richiede `gh auth login`, poi il comando `pull` scarica raw, anteprima e sorgenti con checksum verificati.

I comandi editoriali funzionano con la sola libreria standard di Python. Per l'applicazione completa — account, ruoli, cronologia revisioni, storage e coda di rendering — serve una volta sola:

```bash
pip install -r requirements-dev.txt
python3 studio.py serve
```

Senza configurazione l'applicazione parte in modalità locale a utente singolo, su SQLite e disco locale, e registra da sola i progetti del checkout. Architettura, migrazione e deploy sono documentati in [`docs/`](docs/).

Su Mac, dopo la prima configurazione, è possibile avviare tutto con un doppio clic su `start.command`. Le istruzioni per invitare e configurare un collega sono in `COLLABORATION.md`.

## Flusso di lavoro

1. Modifica `timeline.json`, `cards.json`, `captions.json`, `angles.json` o `brand.json` dalla UI oppure direttamente.
2. Esegui `python3 studio.py validate <project-id>`.
3. Esegui `python3 studio.py sync <project-id>` per aggiornare la composizione.
4. Se cambia la selezione delle take, esegui `python3 studio.py build-source <project-id>`.
5. Esegui `python3 studio.py render <project-id>`.
6. Crea un branch e una pull request per far revisionare il cambiamento.

## Cosa contiene Git

- Brief e regole creative.
- Timeline e riferimenti temporali.
- Testi, captions, grafiche e angolazioni.
- Composizione HTML e strumenti di rendering.
- Manifest e checksum degli asset.
- Codice dell'applicazione, migrazioni e manifesti di deploy.

Raw, proxy, render e tracce audio pesanti sono esclusi tramite `.gitignore`. Credenziali e stringhe di connessione non entrano mai nella repository: la configurazione si legge dall'ambiente e `.env.example` la descrive senza contenere valori.

## Documentazione

- [`docs/ARCHITETTURA.md`](docs/ARCHITETTURA.md) — confine della fase 1, moduli, modello dati, permessi, compromessi.
- [`docs/MIGRAZIONE.md`](docs/MIGRAZIONE.md) — cosa cambia per chi lavora al video e come si passa al cloud.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — elenco esatto dei dati necessari a un deploy su Render.
- [`COLLABORATION.md`](COLLABORATION.md) — come lavorare in due sullo stesso video.

## Primo progetto

`beauty-centers-growth-01` è la versione strutturata del primo video Skyground completo: 1080×1920, 30 fps, 115,933 secondi, quattro inserti Higgsfield più l’angolazione iniziale, diciotto motion card e captions alternate alle grafiche.
