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

## Portare dentro il girato

I media non stanno in Git. Dopo il primo deploy il progetto c'è ma il suo girato
no, e l'editor lo dice: *«il girato non è ancora stato ascoltato»*.

Dal tuo Mac, con il checkout e gli asset già scaricati (`studio.py pull`):

```bash
# chiede un URL firmato e carica direttamente, senza passare dall'API
curl -X POST https://<host>/api/projects/beauty-centers-growth-01/assets/upload-url \
  -H 'Content-Type: application/json' -b cookie.txt \
  -d '{"path": "assets/raw.mov"}'
```

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
