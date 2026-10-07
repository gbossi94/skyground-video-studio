# Migrazione

## Cosa cambia per chi lavora oggi sul video

Niente di obbligatorio. I comandi restano gli stessi e producono lo stesso
output:

```bash
python3 studio.py list
python3 studio.py validate beauty-centers-growth-01
python3 studio.py sync beauty-centers-growth-01
python3 studio.py build-source beauty-centers-growth-01
python3 studio.py render beauty-centers-growth-01
python3 studio.py serve
```

`serve` avvia l'applicazione completa se le dipendenze sono installate, altrimenti
il server locale di prima, dicendolo. Per forzare il vecchio server:
`python3 studio.py serve --legacy`.

Due differenze visibili, entrambe volute:

1. `render` scrive un file con la versione nel nome
   (`renders/beauty-centers-growth-01-20260911-140322.mp4`) e aggiorna
   `renders/latest.mp4` come copia. Un render approvato non viene più
   sovrascritto, come chiede `CLAUDE.md`.
2. Il pannello mostra la revisione dopo ogni salvataggio e, se il file è cambiato
   nel frattempo, ricarica la versione salvata invece di sovrascriverla.

## Installazione dell'applicazione in locale

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python3 studio.py serve
```

Senza configurazione l'applicazione usa SQLite in `.skyground/studio.db`, lo
storage su disco in `.skyground/storage` e la modalità a utente singolo: nessun
login, il progetto del checkout viene registrato da solo. `.skyground/` è
ignorata da Git.

Per provare la modalità multiutente in locale:

```bash
export SKYGROUND_AUTH_MODE=password
export SKYGROUND_SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
python3 studio.py db upgrade
python3 studio.py users create gabriele@skyground.online --admin
python3 studio.py projects register --email gabriele@skyground.online
python3 studio.py serve
```

## Dal checkout al cloud

L'ordine conta.

1. **Preparare il database.**

   ```bash
   export SKYGROUND_DATABASE_URL=postgresql://…
   python3 studio.py db upgrade
   ```

   Su Render lo fa `preDeployCommand` a ogni deploy.

2. **Creare le persone.**

   ```bash
   python3 studio.py users create gabriele@skyground.online --admin
   python3 studio.py users create collega@skyground.online
   ```

   La password non si passa mai sulla riga di comando: viene chiesta, oppure
   letta da `SKYGROUND_NEW_PASSWORD` per l'automazione.

3. **Registrare il progetto e assegnare i ruoli.**

   ```bash
   python3 studio.py projects register --email gabriele@skyground.online
   python3 studio.py projects grant beauty-centers-growth-01 --email collega@skyground.online --role editor
   python3 studio.py projects members beauty-centers-growth-01
   ```

   La registrazione è idempotente e non tocca né i file né i membri esistenti.
   Il contenuto iniziale di ogni documento diventa la revisione 1, quindi anche
   lo stato precedente al cloud è ripristinabile.

4. **Caricare i media su R2.** I file pesanti non entrano in Git e oggi stanno
   nella release privata `assets-v1`. Dopo `studio.py pull`, si caricano con le
   chiavi `projects/<slug>/…`, per esempio con `rclone` o con l'API:

   ```bash
   curl -X PUT --data-binary @projects/beauty-centers-growth-01/assets/raw.mov \
     -H "Content-Type: video/quicktime" -b cookie.txt \
     https://<host>/api/projects/beauty-centers-growth-01/assets/raw/raw.mov
   ```

   Per i file grandi conviene chiedere un URL firmato
   (`POST /api/projects/<slug>/assets/upload-url`) e caricare direttamente su R2
   senza passare dal servizio web.

5. **Spostare i documenti nel database, quando si decide di farlo.**

   ```bash
   python3 studio.py projects adopt beauty-centers-growth-01
   ```

   Da quel momento la sorgente di verità sono le righe del database. Il passo
   inverso esiste e riporta tutto su disco in formato identico, pronto per un
   `git diff`:

   ```bash
   python3 studio.py projects export beauty-centers-growth-01
   ```

   **Consiglio:** finché la fase 2 non offre una timeline visuale, conviene
   lasciare il progetto approvato in modalità `workspace`. Il passaggio è
   reversibile, ma la review su pull request è oggi lo strumento di controllo
   migliore.

## Ritorno indietro

- L'applicazione non modifica il formato dei file editoriali: un checkout della
  versione precedente continua a funzionare senza conversioni.
- La migrazione del database ha un `downgrade` completo
  (`python3 studio.py db downgrade --revision base`).
- Nessun dato editoriale vive solo nel database finché non si esegue `adopt`, e
  `export` lo riporta indietro.

## Verifica dopo ogni intervento

```bash
python3 studio.py validate
python -m pytest
npm ci && npm run check
```

`npm run check` segnala `missing_local_asset` su un checkout senza media: è
atteso, i file stanno fuori da Git. Dopo `studio.py pull` il controllo deve
passare del tutto.
