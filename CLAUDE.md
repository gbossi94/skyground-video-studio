# Istruzioni per Claude

Questa repository è la fonte modificabile dei video Skyground e dell'app Skyground Video Studio. Claude è l'ambiente principale di sviluppo. Prima di intervenire sull'app leggi anche `CLAUDE_HANDOFF.md`; lavora su branch dedicati e lascia ogni incremento in una pull request revisionabile. Non trattare l’MP4 finale come sorgente editoriale: modifica i file strutturati del progetto.

## Prima di modificare

1. Leggi `projects/<id>/project.json` e `projects/<id>/README.md`.
2. Controlla gli asset con `python3 studio.py status <id>`.
3. Esegui `python3 studio.py validate <id>` prima e dopo ogni intervento.

## File editoriali

- `timeline.json`: take originali, ordine e tempi di uscita.
- `captions.json`: parole e timestamp.
- `cards.json`: motion graphics. Il campo `body` contiene HTML interno controllato.
- `angles.json`: inserti video alternativi, tempi e offset interni.
- `brand.json`: colori, font e regole di stile.
- `audio.json`: voce, musica, effetti e livelli.
- `composition/index.html`: composizione renderizzabile generata/sincronizzata.

## Regole Skyground vincolanti

- Non mostrare contemporaneamente una motion graphic e sottotitoli che ripetono lo stesso contenuto.
- Le angolazioni AI sono inserti brevi; la voce originale deve rimanere continua.
- Non aggiungere header o watermark come “Skyground AI/01”.
- Non inventare metriche, claim o numeri.
- Conservare 1080×1920, 30 fps salvo richiesta esplicita.
- Non inserire credenziali o URL firmati nei file versionati.
- Non sovrascrivere un render approvato: produrre una nuova versione.

## Comandi

```bash
python3 studio.py list
python3 studio.py status beauty-centers-growth-01
python3 studio.py validate beauty-centers-growth-01
python3 studio.py sync beauty-centers-growth-01
python3 studio.py build-source beauty-centers-growth-01
python3 studio.py render beauty-centers-growth-01
python3 studio.py export beauty-centers-growth-01   # FCPXML per Resolve/Premiere/Final Cut, SRT dei sottotitoli; --format capcut scrive la bozza CapCut (serve una bozza campione)
python3 studio.py serve
```

Questi comandi funzionano con la sola libreria standard. L'applicazione cloud aggiunge `db`, `users`, `projects` e `worker`; il confine tecnico, il modello dati e i permessi sono descritti in `docs/ARCHITETTURA.md`, la migrazione in `docs/MIGRAZIONE.md`.

Quando modifichi una timeline, aggiorna gli `output_start` in sequenza e la durata totale. Se cambi la durata, ricontrolla cards, captions e angles. Dopo un render, verifica sempre decodifica, dimensioni, frame rate, audio e durata.
