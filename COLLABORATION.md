# Lavorare sullo stesso video

## Prima installazione

1. Accetta l'invito alla repository privata su GitHub.
2. Installa GitHub CLI, Node.js, Python 3 e FFmpeg.
3. Esegui:

```bash
gh auth login
gh repo clone gbossi94/skyground-video-studio
cd skyground-video-studio
npm ci
python3 studio.py pull beauty-centers-growth-01
python3 studio.py serve
```

Apri `http://127.0.0.1:4173` per vedere il pannello.

## Con Claude Code

Apri la cartella `skyground-video-studio` con Claude Code. `CLAUDE.md` descrive formato, controlli e regole creative. Una richiesta può essere formulata così:

> Nel progetto beauty-centers-growth-01 sposta l'angolo overhead a 48,5 secondi, controlla che non copra una motion card, valida la modifica e mostrami il diff prima del render.

Claude deve modificare i JSON, validare, sincronizzare la composizione e lasciare il cambiamento su un branch. Dopo la revisione, il branch viene unito in `main`.

## Avvio quotidiano su Mac

Dopo la prima installazione, fai doppio clic su `start.command`. Il programma controlla gli asset e avvia il pannello. Per ricevere le ultime modifiche esegui `git pull` prima di iniziare; alla fine crea un commit e fai push sul tuo branch.

## Aggiungere un progetto

Ogni nuovo video ha una cartella sotto `projects/` con lo stesso formato del primo progetto. Media e render restano fuori da Git; il loro manifest con checksum va in `assets.lock.json`.
