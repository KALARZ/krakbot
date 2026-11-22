# krakbot

Bot privato e single-user per **Kraken Spot**: un **DCA su BTC/EUR** con ordini maker post-only. Un processo Python, SQLite, systemd.

Lo stato dei lavori e le regole sono in `docs/plan.md`. La ricerca che ha portato a un DCA invece di una strategia attiva è in `docs/research.md` (verdetto: NO-GO per il trading attivo).

## Cosa fa, e cosa non fa mai

- **Default DRY_RUN**: broker simulato, zero ordini reali. Il LIVE richiede quattro chiavi indipendenti:
  1. `[strategies.dca].mode = "live"`;
  2. `KRAKBOT_LIVE_ENABLED=true` in `.env`;
  3. `run --live --i-understand-live`;
  4. **7 giorni UTC di DRY_RUN puliti**, con la definizione pre-registrata del `docs/plan.md` §5.1.
- **Il primo live è una canary** (§5.3): tranche di un solo ordine alla size minima di Kraken, ognuna verificata da te (`krakbot canary-verify`) prima della successiva. La size piena arriva solo dopo 3 tranche pulite consecutive.
- Solo acquisti limit **post-only GTD**: **mai market, mai vendite**. Il kill switch non vende mai i BTC del DCA.
- **Nessun leverage, futures o margin.** Nessun LLM nel percorso degli ordini.
- **Ordine dal risultato dubbio** (timeout dopo l'invio): mai reinviato. Il DCA va in halt e si sblocca solo con un'evidenza da Kraken o con `krakbot resolve` (§4.7).
- **Dead man's switch** di Kraken armato prima di ogni ordine live: se il bot muore, l'ordine a riposo è cancellato entro 60 s (§5.2).
- **Segreti solo in `.env`**, con permessi 600 e proprietario l'utente del servizio (il bot rifiuta di partire altrimenti). Mai in git, mai nei log: chiavi, firme, token e URL di heartbeat vengono redatti.

## Requisiti

- Linux con systemd: Debian 12 / Ubuntu 22.04+ o equivalente.
- Python ≥ 3.12.
- Orologio sincronizzato (chrony o systemd-timesyncd).
- Uscita HTTPS/WSS verso `api.kraken.com` e `ws.kraken.com`; opzionali `api.telegram.org` e il servizio di heartbeat esterno.
- Nessuna porta in ingresso oltre SSH.

## Installazione sul VPS

Il repository è privato: sul VPS serve una **deploy key** in sola lettura. Generala sul VPS, aggiungila su GitHub in *Settings → Deploy keys* del repository, **senza** "Allow write access":

```bash
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/krakbot_deploy && sudo cat /root/.ssh/krakbot_deploy.pub
```

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv git chrony
```

```bash
sudo GIT_SSH_COMMAND="ssh -i /root/.ssh/krakbot_deploy -o IdentitiesOnly=yes" git clone git@github.com:KALARZ/krakbot.git /opt/krakbot
```

Poi tutto il resto con un solo comando: crea l'utente, il venv, il `.env` (permessi 600, **senza chiavi Kraken**), installa le unità e avvia DRY_RUN, backup e run ombra. Rifiuta di partire se nel `.env` trova chiavi Kraken.

```bash
sudo CAPITAL=5000 /opt/krakbot/deploy/install_vps.sh --with-shadow
```

Per gli allarmi Telegram e l'heartbeat esterno (consigliati, vedi sotto) modifica il `.env` e riavvia, **prima** che inizi il primo giorno UTC completo, così il riavvio non costa uptime a un giorno che conta:

```bash
sudo -u krakbot nano /opt/krakbot/.env
```

```bash
sudo systemctl restart krakbot
```

I passi manuali equivalenti, se preferisci farli uno per uno:

```bash
sudo useradd --system --home-dir /opt/krakbot --shell /usr/sbin/nologin krakbot
```

```bash
sudo chown -R krakbot:krakbot /opt/krakbot
```

```bash
sudo -u krakbot python3 -m venv /opt/krakbot/.venv
```

```bash
sudo -u krakbot /opt/krakbot/.venv/bin/pip install /opt/krakbot
```

```bash
sudo -u krakbot install -m 600 /opt/krakbot/.env.example /opt/krakbot/.env
```

```bash
sudo -u krakbot mkdir -p /opt/krakbot/data /opt/krakbot/data-shadow
```

In `/opt/krakbot/.env` imposta `CAPITAL` (EUR). Per i 7 giorni di DRY_RUN **lascia vuote le chiavi Kraken**: non servono. Consigliati: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` per gli allarmi, e `HEALTHCHECK_URL` di un servizio di heartbeat esterno. Un processo morto non può avvisarti: lo può fare solo il servizio esterno, che non riceve più ping.

```bash
sudo -u krakbot nano /opt/krakbot/.env
```

Servizio DRY_RUN, backup giornaliero e run ombra:

```bash
sudo cp /opt/krakbot/deploy/systemd/krakbot.service /opt/krakbot/deploy/systemd/krakbot-backup.service /opt/krakbot/deploy/systemd/krakbot-backup.timer /opt/krakbot/deploy/systemd/krakbot-shadow.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now krakbot.service krakbot-backup.timer krakbot-shadow.service
```

Verifica:

```bash
systemctl status krakbot
```

```bash
journalctl -u krakbot -f -o cat
```

`systemctl status` mostra `healthy` oppure `degraded (no fresh WS book)`. Il watchdog di systemd riavvia il processo solo se il loop si blocca per 120 s, non quando Kraken è lento.

## Comandi

Per comodità, un alias (aggiungilo al tuo `~/.bashrc`):

```bash
alias kb='sudo -u krakbot /opt/krakbot/.venv/bin/krakbot --config /opt/krakbot/config.toml --env /opt/krakbot/.env'
```

| Comando | A cosa serve |
|---|---|
| `kb status` | halt, ordini non risolti, tranche corrente, totali, stato canary, ultima run |
| `kb gate` | tabella dei 7 giorni (uptime, halt, campioni, riconciliazioni) ed esito del gate |
| `kb spreads --days 7` | percentili giornalieri di spread e profondità per coppia |
| `kb liquidity-derive --yes` | soglie di liquidità dai 7 giorni puliti (rifiuta altrimenti) |
| `kb halt --reason "..." [--mode live]` | ferma i nuovi acquisti (non vende nulla) |
| `kb resume [--mode live]` | toglie un halt `manual` o `recon`; mai `unknown_order` né `canary` |
| `kb resolve <cl_ord_id> [--dry]` | risolve un ordine dubbio con l'evidenza di Kraken (§4.7) |
| `kb canary-verify <tranche_id> [--yes]` | verifica una tranche canary live: fill, registro, riconciliazione, saldo |
| `kb backup <file>` | copia coerente del DB anche a bot acceso |
| `kb shadow-report` | cosa ha già esercitato la run ombra oraria (repricing, scadenze GTD, parziali, riporto) |

Le procedure operative (controlli giornalieri, incidenti, passaggio al live) sono in **`docs/runbook.md`**.

## Sviluppo

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
.venv/bin/pytest && .venv/bin/ruff check src tests && .venv/bin/mypy
```

I test non toccano mai la rete: Kraken è simulato con respx e un server WebSocket locale.

## Struttura

```
src/krakbot/
  config.py          validazione rigorosa di config.toml e .env (valori pre-registrati bloccati)
  kraken_rest/       firma, nonce persistito, rate limit, classificazione errori, nessun reinvio se dubbio
  kraken_ws/         book v2 con checksum CRC32, trade, heartbeat, riconnessione
  market_data.py     top of book e campionatore di liquidità a griglia fissa
  strategies/dca.py  logica DCA pura (budget, riporto, sizing, canary)
  execution/         broker paper/Kraken, motore DCA, resolve, DMS, riconciliazione, verifica canary
  state/             SQLite WAL, migrazioni con checksum, repository, gate del giorno pulito
  monitor/           log JSON con redazione, Telegram, heartbeat systemd/esterno
  app.py, cli.py     composizione e comandi
  shadow.py          run ombra a calendario orario: DB proprio, solo WebSocket, mai nel gate
deploy/systemd/      unità DRY_RUN, LIVE (mutuamente esclusive), ombra oraria, backup
deploy/install_vps.sh  installazione idempotente sul VPS (DRY_RUN senza chiavi)
docs/                research.md, plan.md, runbook.md
research/            script e risultati della Fase 0 (i dati scaricati non sono in git)
```
