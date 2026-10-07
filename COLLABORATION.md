# Lavorare su Skyground Video Studio

Guida per chi entra nel progetto. Da zero a «vedo l'editor sul mio Mac» in una decina di minuti.

## Cos'è, in due righe

Lo studio monta da solo un video parlato: si carica il girato, viene trascritto, un modello decide cosa tenere, e l'editor nel browser serve a correggere i tagli prima che tutto venga rigenerato (sottotitoli, grafiche, render). Il backend è Python (FastAPI, `skyground/`), l'editor è React (`app/`), il render dei film con grafiche usa HyperFrames.

Prima di toccare il codice leggi `CLAUDE.md` (regole del progetto) e `CLAUDE_HANDOFF.md` (stato e storia). L'architettura è in `docs/ARCHITETTURA.md`.

## Prima installazione (Mac)

Ti servono: [Homebrew](https://brew.sh), un account GitHub con l'invito alla repository accettato, e Claude Code con il tuo account.

```bash
brew install python@3.13 node ffmpeg gh
gh auth login
gh repo clone gbossi94/skyground-video-studio
cd skyground-video-studio
```

Dipendenze, una volta sola:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
npm ci
cd app && npm ci && cd ..
```

I media del progetto di esempio stanno in una release privata della repository (non in Git). Li scarica questo comando, verificando i checksum:

```bash
python3 studio.py pull beauty-centers-growth-01
```

## Avviare tutto in locale

Due terminali.

Lo studio (API, database SQLite e storage su disco, un solo utente, nessun login):

```bash
SKYGROUND_ENV=local SKYGROUND_AUTH_MODE=open SKYGROUND_CUT_ENGINE=heuristic .venv/bin/python studio.py serve --port 4173
```

L'editor, con ricaricamento a caldo:

```bash
cd app && npm run dev
```

Apri **http://localhost:5173/app/**.

`SKYGROUND_CUT_ENGINE=heuristic` usa il motore di taglio semplice, che non chiama nessun modello: basta per lavorare sull'editor e sul resto. Per far decidere i tagli al modello vero serve una chiave Claude tua, mai nella repository:

```bash
export SKYGROUND_ADVISER_API_KEY=...   # la tua chiave, nel terminale o in un .env non versionato
```

e togli `SKYGROUND_CUT_ENGINE=heuristic`.

Per puntare l'editor locale contro la produzione invece che contro lo studio locale (serve un account sull'app online):

```bash
cd app && SKYGROUND_API=https://skyground-studio.onrender.com npm run dev
```

## Controlli prima di ogni commit

```bash
.venv/bin/pytest -q --ignore=tests/test_browser_editor.py
cd app && npm run typecheck && npm test && npm run build
python3 studio.py validate beauty-centers-growth-01
```

Alcuni test con FFmpeg falliscono anche sul ramo pulito (13 al momento in cui è scritta questa guida): confronta con il ramo di partenza prima di preoccuparti.

## Come si lavora

- **La produzione** (https://skyground-studio.onrender.com) si aggiorna da sola a ogni push sul ramo `claude/phase-2-smart-cut`. Un deploy riavvia il server e interrompe i caricamenti in corso.
- **Non si fa push su quel ramo.** Si parte da lì, si lavora su un ramo proprio e si apre una pull request verso `claude/phase-2-smart-cut`. La pubblica Gabriele.

```bash
git fetch origin
git switch -c gianluca/nome-della-modifica origin/claude/phase-2-smart-cut
# ... lavoro, commit ...
git push -u origin gianluca/nome-della-modifica
gh pr create --base claude/phase-2-smart-cut --draft
```

- **Le credenziali non entrano mai nella repository**: niente chiavi, password o URL firmati nei file versionati. `.env` è ignorato da Git.
- **Un render approvato non si sovrascrive**: ogni render è una versione nuova.

## Con Claude Code

Apri la cartella con Claude Code: legge da solo `CLAUDE.md`. Chiedigli le cose come le chiederesti a un collega, per esempio:

> Nell'editor, quando trascino un bordo vicino al playhead si aggancia anche se sono a 30 pixel: stringi la calamita a 8 pixel, aggiungi un test e apri una PR in bozza.

## Il render dei film con grafiche

I film con motion graphics e angolazioni AI non si rendono sul server (2 GB di memoria, ore di lavoro): si rendono su un Mac che ha i media del progetto e il risultato viene caricato in produzione come nuova versione:

```bash
python3 studio.py render-remote <progetto>
```

Chiede email e password dell'app online e rende la timeline **applicata** in produzione. I film senza grafiche si rendono da soli sul server con FFmpeg.
