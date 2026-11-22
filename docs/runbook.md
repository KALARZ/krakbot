# Runbook — krakbot DCA

Procedure operative. Le regole che queste procedure applicano sono in `docs/plan.md`: giorno pulito §5.1, DMS §5.2, canary §5.3, ordine dubbio §4.7.

Tutti i comandi usano l'alias del README:

```bash
alias kb='sudo -u krakbot /opt/krakbot/.venv/bin/krakbot --config /opt/krakbot/config.toml --env /opt/krakbot/.env'
```

Codici di uscita: `0` fatto, `2` rifiutato (lo stato non cambia; per un halt, l'halt resta), `1` errore.

---

## 1. I 7 giorni di DRY_RUN

### Avvio
1. Installazione come da README, `krakbot.service` attivo e `.env` **senza chiavi Kraken**.
2. Il primo giorno pulito possibile è il **primo giorno UTC completo** dopo l'avvio. Il giorno dell'avvio non conta: è incompleto, quindi l'uptime è sotto il 99%.
3. Il gate richiede anche che almeno un ordine DRY_RUN arrivi a stato terminale. Succede alla finestra DCA di lunedì 10:00 UTC, quindi i 7 giorni devono contenere un lunedì.

### Controllo giornaliero (2 minuti)

```bash
kb gate
```

Una riga per giorno: `uptime`, `halted`, la peggiore copertura di campioni (con la coppia), riconciliazioni ok/totali, uscite sporche, `CLEAN`. I giorni in corso non compaiono: il gate guarda solo giorni UTC completi.

```bash
kb status
```

```bash
journalctl -u krakbot --since "24 hours ago" -p warning -o cat
```

```bash
kb spreads --days 7
```

Che cosa rompe un giorno pulito, e cosa fare:

| Sintomo | Causa probabile | Azione |
|---|---|---|
| `uptime < 0.99` | WebSocket Kraken giù > 14 min nel giorno, VPS spento, processo fermo | `journalctl`; se è stato Kraken, nulla da fare: il giorno è perso e il conteggio dei 7 riparte |
| `halted yes` | ordine dubbio simulato, riconciliazione fallita, halt manuale | §2 sotto |
| `<pair> samples < 0.90` | coppia delistata o sospesa, book con checksum ripetutamente errato | `journalctl \| grep checksum`; se una coppia è stata tolta da Kraken, toglierla da `sample_pairs` è un cambio di config da annotare: i giorni ricominciano |
| `recon 0/0` | il bot non ha girato in quel giorno | — |
| `unclean > 0` | crash, OOM, kill, riavvio da watchdog | `journalctl -u krakbot -b` e cerca `Traceback`, `watchdog`, `Main process exited` |

**Durante i 7 giorni evita riavvii e aggiornamenti.** Un `systemctl restart` è un'uscita pulita, ma costa circa un minuto di uptime. Un riavvio della macchina è pulito se systemd ferma il servizio con SIGTERM.

### Run ombra (in parallelo, facoltativa)
La run ombra applica le stesse regole DCA con una tranche **ogni ora**: in 7 giorni esercita repricing, scadenze GTD, fill parziali e riporto molte volte invece di una (plan §5.4).
- Ha un DB proprio (`data-shadow/shadow.db`) e **non entra mai nel gate**: i suoi problemi non sporcano i giorni puliti.
- Non fa chiamate REST (usa solo il WebSocket) e non campiona lo spread.
- Gira a bassa priorità, con al massimo 6 connessioni WebSocket ogni 10 minuti.

```bash
kb shadow-report
```

Ogni voce dice `COVERED` (vista almeno una volta) o `not yet`. Dopo un giorno di solito sono tutte coperte, tranne forse "post-only reject", che richiede un book incrociato. Log:

```bash
journalctl -u krakbot-shadow --since today -o cat | grep -v '"level": "INFO"'
```

Se servisse fermarla, l'istanza principale e il gate non cambiano:

```bash
sudo systemctl disable --now krakbot-shadow
```

### Al termine

```bash
kb gate
```

Deve dire `"allowed": true`. Poi:

```bash
kb liquidity-derive
```

```bash
kb liquidity-derive --yes
```

Il primo mostra cosa verrebbe derivato, il secondo lo registra. Le soglie sostituiscono quelle del campione di 30 minuti. Da qui in poi **ci si ferma e si chiede l'OK per il live** (§3).

---

## 2. Incidenti

### Halt `unknown_order` (ordine dal risultato dubbio)
L'allarme critico ripete ogni 15 minuti con il `cl_ord_id`.
1. Leggi lo stato:

   ```bash
   kb status
   ```

2. Raccogli l'evidenza da Kraken senza cambiare nulla:

   ```bash
   kb resolve <cl_ord_id> --dry
   ```

3. Se l'ordine è `found` (lo adotta) o `absent` (assenza provata: oltre `deadline` + 60 s e interrogazioni riuscite):

   ```bash
   kb resolve <cl_ord_id>
   ```

4. Se Kraken non risponde: **aspetta e riprova**. L'halt resta, ed è voluto.
5. Solo se hai verificato tu su kraken.com (Orders → Closed, filtro per data) che l'ordine non esiste:

   ```bash
   kb resolve <cl_ord_id> --operator-confirms-absent --reason "verificato su kraken.com alle HH:MM UTC" --yes
   ```

In DRY_RUN il book simulato muore col processo: dopo `deadline` + 60 s l'assenza è provata e lo sblocco è di norma automatico.

### Halt `recon` (riconciliazione fallita o sforamento > 1% del budget)
1. Leggi lo stato:

   ```bash
   kb status
   ```

2. Guarda gli eventi critici:

   ```bash
   journalctl -u krakbot -o cat | grep -E 'RECONCILIATION|OVERSPEND|CRITICAL'
   ```

3. Capisci la causa prima di ripartire. Un fill reale non si cancella mai dal registro.
4. Riparti solo quando la causa è chiara:

   ```bash
   kb resume
   ```

### `degraded (no fresh WS book)` in `systemctl status`
Il WebSocket pubblico non dà un book valido; si usa il fallback REST per il prezzo ma **nessun nuovo ordine**. Se dura più di qualche minuto, controlla `status.kraken.com` e la rete del VPS. Non serve riavviare: il client si riconnette da solo con backoff.

### Il servizio non riparte (`start-limit-hit`)
5 riavvii in 15 minuti: systemd si ferma. Leggi il motivo, poi riparti:

```bash
journalctl -u krakbot -b -o cat | tail -100
```

```bash
sudo systemctl reset-failed krakbot && sudo systemctl start krakbot
```

### Exit code 2 all'avvio
Rifiuto voluto (config, gate, interlock): il servizio non riprova. Il motivo è nel journal (`REFUSED: ...`).

### Fermare gli acquisti subito

```bash
kb halt --reason "motivo"
```

Aggiungi `--mode live` in live. Non vende nulla. Per ripartire:

```bash
kb resume
```

---

## 3. Passaggio al live: canary (solo con l'OK esplicito)

Prerequisiti, tutti veri:
- `kb gate` → `allowed: true` e `liquidity-derive` fatto;
- chiave API Kraken creata con **solo** "Query Funds", "Query Open Orders & Trades", "Query Closed Orders & Trades", "Create & Modify Orders", "Cancel/Close Orders"; **nessun prelievo**; **IP whitelist** = IP del VPS;
- nessun ordine manuale aperto su questo account: il dead man's switch li cancellerebbe (§5.2);
- **nessun movimento di BTC** sull'account (depositi, prelievi, vendite manuali) per tutta la canary: il controllo del saldo è un'uguaglianza esatta;
- EUR disponibili ≥ 20% di `CAPITAL` (programma DCA); la canary ne usa circa 3,5 per tranche.

Passi:
1. In `.env`: `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`, `KRAKBOT_LIVE_ENABLED=true`.
2. In `config.toml`: `[strategies.dca] mode = "live"`.
3. Ferma il DRY_RUN e avvia il live (le due unità si escludono a vicenda):

   ```bash
   sudo systemctl disable --now krakbot.service
   ```

   ```bash
   sudo cp /opt/krakbot/deploy/systemd/krakbot-live.service /etc/systemd/system/ && sudo systemctl daemon-reload
   ```

   ```bash
   sudo systemctl enable --now krakbot-live.service
   ```

4. Nel journal: evento `LIVE_UNLOCKED` (il gate è passato; vale una volta sola, i riavvii successivi non lo richiedono) e `live baseline` con il saldo BTC iniziale.

   ```bash
   journalctl -u krakbot-live -f -o cat
   ```

5. **Lunedì 10:00 UTC** si apre la tranche canary: un allarme dice budget e comando di halt. Sorveglia l'ordine su kraken.com: un solo buy post-only da 0,00006 BTC circa.
6. Alla chiusura della tranche il DCA va in halt `canary` e l'allarme critico dà il `tranche_id`. Prima solo controllo, poi conferma:

   ```bash
   kb canary-verify <tranche_id>
   ```

   ```bash
   kb canary-verify <tranche_id> --yes
   ```

   Il primo mostra i 4 controlli (fill, registro, riconciliazione, saldo) senza registrarli. Guarda l'ordine e il saldo su kraken.com, poi conferma con il secondo. Un controllo fallito si registra, l'halt resta e quella tranche non sarà mai "pulita": indaga prima di tutto.
7. Ripeti per 3 settimane. La size piena arriva **solo dopo 3 tranche canary pulite consecutive** (`kb status` → `canary: clean streak 3/3, phase complete`). Una tranche non eseguita, parziale o fallita azzera la serie.

Per tornare indietro in qualsiasi momento:

```bash
kb halt --mode live --reason "..."
```

Poi `systemctl stop krakbot-live`. Gli ordini a riposo muoiono per GTD (300 s) e per il dead man's switch (60 s).

---

## 4. Manutenzione

Backup automatico giornaliero (`krakbot-backup.timer`, 00:20 UTC, conservati 30 giorni in `data/backups/`). A mano:

```bash
kb backup /opt/krakbot/data/backups/manuale.db
```

Aggiornamento (fuori dai 7 giorni, e mai con una tranche live aperta):

```bash
sudo GIT_SSH_COMMAND="ssh -i /root/.ssh/krakbot_deploy -o IdentitiesOnly=yes" git -c safe.directory=/opt/krakbot -C /opt/krakbot pull --ff-only && sudo chown -R krakbot:krakbot /opt/krakbot
```

```bash
sudo -u krakbot /opt/krakbot/.venv/bin/pip install /opt/krakbot
```

```bash
sudo systemctl restart krakbot krakbot-shadow
```

Le migrazioni del DB si applicano da sole all'avvio. Il bot rifiuta di partire se una migrazione già applicata è stata modificata o se il DB è più nuovo del codice.

Ripristino da backup (servizio fermo):

```bash
sudo systemctl stop krakbot
```

```bash
sudo -u krakbot cp /opt/krakbot/data/backups/<file>.db /opt/krakbot/data/krakbot.db && sudo -u krakbot rm -f /opt/krakbot/data/krakbot.db-wal /opt/krakbot/data/krakbot.db-shm
```

```bash
sudo systemctl start krakbot
```

In LIVE un ripristino perde i fill successivi al backup: si ripristina solo con la riconciliazione verso Kraken in mano, mai alla cieca.
