# Piano tecnico — bot Kraken Spot (single-user, single-process)

> Stato: **approvato (opzione 1)**; implementazione a blocchi, avanzamento nel §10. Nessun ordine reale è mai stato inviato. Il verdetto della ricerca è in `docs/research.md` §G e condiziona il §0.

## 0. Cosa si costruisce, dato il verdetto della ricerca
La Fase 0 si è chiusa con **NO-GO per il trading attivo**: nessuna delle 5 famiglie supera i gate OOS. Walk-forward pre-registrato: PF 0.78–0.95, Sharpe < 0, expectancy lorda ≤1.26× il round-trip (`docs/research.md` §E–G).
Il piano costruisce comunque l'infrastruttura richiesta (tutti i 12 moduli, gli stessi standard di sicurezza e i test), con due strategie collegabili e un vincolo duro:
1. **`dca`** (l'unica abilitabile in LIVE, e solo dopo 7 giorni UTC di DRY_RUN puliti, `state/gates.py`):
   - BTC/EUR, il 20% di `CAPITAL` in 52 tranche settimanali (lunedì 10:00 UTC);
   - regole operative complete nel §0.1;
   - DD di conto storico stimato ≈ 0.20 × 76.6% = 15%.
   È esposizione al mercato, non un edge.
2. **`candidate`** = donchian 4h (N=50, k_sl=2.5, k_tr=3.5, regime SMA200), universo point-in-time **top-4** Kraken EUR (vincolo 2–4 coppie), **solo DRY_RUN** come forward test su dati live, con capitale simulato pari all'80% di `CAPITAL`.
   - Baseline OOS con le stesse regole (`docs/research.md` §K.4): PF 0.98, Sharpe −0.03, DD 12%.
   - Il LIVE è rifiutato dal validatore di config. Le regole del forward test (lettura a fee tier 1, checkpoint di abbandono a 100 trade, gate di sblocco) sono pre-registrate nel §0.2.
   - Ramo di ricerca sul sizing (`docs/research.md` §K.5): la relazione stop/rendimento **non è monotona**, quindi nessuna correzione e la configurazione resta quella sopra.

### 0.1 DCA: regole operative (pre-registrate; `[strategies.dca]` in `config.toml`)
**Budget e sizing dai minimi reali.**
- Tranche nominale `T = allocation × CAPITAL / tranches` (EUR).
- A ogni piazzamento d'ordine:
  - `p` = best bid arrotondato al tick (lato passivo);
  - `f` = fee maker reale: `TradeVolume` in LIVE, 0.40% (tier 1) in DRY_RUN;
  - `qty` = il massimo multiplo del lotto per cui `qty × p + fee ≤ budget_residuo`, con la fee **arrotondata per eccesso** ai `cost_decimals` della coppia, come la addebita Kraken (`strategies/dca.py::order_qty`, `fee_for`).
  - **Un fill reale si registra sempre**, anche se supera il budget: rifiutarlo farebbe divergere il registro dall'exchange. Uno sforamento (arrotondamento, o fee peggiorata tra invio e fill, per esempio se l'AoP scende di tier) genera l'evento `DCA_OVERSPEND`. Oltre l'1% del budget della tranche (`OVERSPEND_HALT_FRACTION`) è un'anomalia: nuovi acquisti in halt (`recon`) finché non controlli e dai `krakbot resume`. Il registro basato sul dovuto (`spendable = due − speso cumulato`) assorbe lo sforamento nella tranche successiva; `carry_out` non scende sotto 0.
- Minimo reale: `min_cost = max(ordermin × p, costmin)`, con `ordermin` e `costmin` letti da `AssetPairs` a ogni avvio e ogni 24 h (`App._refresh_instruments`, evento `INSTRUMENTS_CHANGED` e allarme se cambiano). Mai costanti.
- Un ordine si invia solo se `qty ≥ ordermin` e `qty × p ≥ costmin`. Altrimenti il budget passa alla settimana successiva, senza perdersi.
- **Controllo all'avvio**: se `T < 1.5 × min_cost` (margine per i movimenti di prezzo), il numero di tranche scende a `floor(allocation × CAPITAL / (1.5 × min_cost))` con warning. Se il risultato è 0, il DCA è disabilitato con l'errore "capitale sotto il minimo: servono ≥ X EUR".
- Con BTC a 54.557 EUR, `min_cost` = 3.27 EUR, quindi con 52 tranche servono ≥ 851 EUR di capitale.

**Ordine e repricing dei maker non riempiti.**
- Limit **post-only** al best bid, `timeinforce=GTD` con `expiretm = now + reprice_seconds` (300 s), `deadline = now + 10 s`, `cl_ord_id = UUIDv5(tranche_id, attempt)`.
- Nessun `AmendOrder`: Kraken non permette di modificare `expiretm`.
- Il ciclo è:
  1. l'ordine scade lato server;
  2. il bot ne piazza uno nuovo al best bid corrente, per il solo budget residuo.
  - È crash-safe: se il bot muore, l'ordine muore entro 300 s.
  - Rifiuto post-only (il prezzo si è mosso): nuovo ordine subito al nuovo bid.
- **Limite di inseguimento**: il prezzo non supera mai `ref × (1 + max_chase)`, con `ref` = mid all'apertura della finestra e `max_chase` = 2%. Se il bid è sopra il limite, il bot attende senza ordini.
- Fill parziali: l'ordine successivo è dimensionato sul residuo. Se il residuo è sotto `min_cost`, la tranche è chiusa e il residuo va a riporto.
- **Mai market**, in nessun caso.

**Fine finestra** (24 h dall'apertura, cioè martedì alle 10:00 UTC):
1. nessun nuovo ordine; l'ultimo scade per GTD entro 300 s;
2. in LIVE, `OpenOrders` deve confermare che non resta nulla, altrimenti cancel per `cl_ord_id`;
3. esito della tranche:
   - `FILLED`: budget speso entro `min_cost`;
   - `PARTIAL`: speso in parte, il residuo va a riporto;
   - `UNFILLED`: nulla, l'intero budget va a riporto;
4. alert Telegram per `PARTIAL` e `UNFILLED`.

**Riporto.**
- Il budget della settimana successiva è `T` più il riporto, con un tetto di **2 × T** per settimana. L'eccedenza allunga il calendario oltre le 52 settimane.
- La spesa totale non supera mai `allocation × CAPITAL`.
- 4 settimane consecutive `UNFILLED` ⇒ alert `DCA_STALLED`.

**DMS e crash**: gli ordini DCA sono GTD, quindi non sopravvivono al bot. Al riavvio nella finestra si riprende con il budget residuo ricostruito dai fill; dopo la finestra si chiude la tranche come al passo 3.

### 0.2 Forward test del Donchian: regole pre-registrate (`[forward_test]` in `config.toml`, valori bloccati dal validatore)
**Congelamento.** Al via del forward test il bot salva nella tabella `forward_tests` la data di inizio e lo SHA-256 (`config.forward_test_freeze_hash`) di **esattamente** questi elementi, definiti campo per campo in `config.FREEZE_FROZEN`:
- `[strategies.donchian]`, tutti i campi tranne `mode`;
- `[forward_test]`;
- `[risk]`;
- `[liquidity]`;
- di `[execution]` solo i parametri d'ordine (`entry_*`, `exit_*`, `max_chase`, `stop_trigger`);
- le **soglie di liquidità per coppia** derivate dai 7 giorni di DRY_RUN (§6);
- la versione del codice della strategia (`DONCHIAN_STRATEGY_VERSION`, da incrementare a ogni modifica di strategia o di `forward_test.py`).

**Il DCA è escluso**, verificato da test: tutti i campi di `[strategies.dca]`, `[live_gate]`, `[monitor]`, `[general]` e i parametri operativi di `[execution]` (`deadline_seconds`, `dms_*`), che non cambiano i fill simulati.
- Ogni campo della config deve stare in `FREEZE_FROZEN` oppure in `FREEZE_EXCLUDED`, mai in entrambi né in nessuno. Un test lo impone, così un campo nuovo non può sfuggire alla classificazione.
- Un secondo test cambia ogni campo uno alla volta: l'hash cambia se e solo se il campo è congelato.
- Per separare davvero gli ambiti, il DCA non legge più `[risk]`: ha i propri `min_tranche_safety_factor` e `pause_spread_percentile`.

Qualunque differenza successiva dell'hash **chiude** il forward test in corso (`ABANDONED_CONFIG_CHANGED`). Un eventuale nuovo test riparte da zero con un nuovo `run_id`: non si "aggiustano" i parametri a metà.

**Lettura dei risultati: sempre a fee tier 1.**
- Fee 0.40% maker / 0.80% taker, più 5 bp di slippage per lato, più l'impatto sul book per le uscite taker, **indipendentemente dal tier reale del conto**.
- Il simulatore registra anche il PnL al tier reale, solo a titolo informativo.

**Checkpoint di abbandono** (unico punto di decisione intermedio):
- al **100° trade chiuso**: se PF (T1) **< 1.0** ⇒ forward test **chiuso** e candidato abbandonato. Se PF ≥ 1.0 ⇒ si prosegue fino a 300;
- prima del checkpoint nessuna metrica viene usata per decidere, salvo bug;
- il kill switch totale simulato (DD 15%) chiude il test anche prima dei 100 trade, perché il bot live si sarebbe fermato;
- il kill giornaliero simulato sospende gli ingressi per 24 h e si riprende da solo, con log.

**Gate di sblocco del LIVE**, tutti a tier 1, valutati una sola volta dopo ≥ 300 trade chiusi:
- PF > 1.3;
- Sharpe > 1 (equity oraria MTM);
- max DD < 20%;
- ≥ 80% dei mesi positivi;
- expectancy > 0 con fee ×2 (0.80/1.60).

Se passano, lo sblocco richiede comunque una modifica del codice (il validatore oggi rifiuta `live`) e un tuo OK esplicito.

**Ipotesi secondaria: specifica pre-registrata** (codice: `src/krakbot/forward_test.py::secondary_hypothesis`; valori bloccati in `[forward_test]`):

| Elemento | Definizione esatta |
|---|---|
| Ipotesi | H1: nei trade con stop iniziale più stretto (in % del prezzo) il rendimento netto medio a tier 1 è **maggiore**. H0: non è maggiore |
| Campione | i **primi 100 trade chiusi** del forward test, ordinati per orario di uscita (a parità, per `trade_id`) |
| Variabile di split | `stop_pct = 1 − stop_iniziale / prezzo_ingresso` |
| Split | i 100 trade ordinati per (`stop_pct`, orario d'ingresso, `trade_id`) crescenti: **LOW** = posizioni 1–50, **HIGH** = posizioni 51–100. Niente mediana interpolata né pareggi ambigui |
| Variabile di esito | `ret_t1` = PnL netto a fee tier 1 (0.40/0.80) più 5 bp per lato più impatto sul book, diviso il nozionale d'ingresso |
| Statistica | t di Welch `t = (x̄_LOW − x̄_HIGH) / √(s²_LOW/50 + s²_HIGH/50)` (varianze campionarie con n−1), gradi di libertà di Welch–Satterthwaite, **p a una coda** `P(T_ν > t)` (validata contro scipy nei test) |
| Soglia | **SUPPORTED se p < 0.05**, altrimenti NOT_SUPPORTED. Meno di 100 trade chiusi o varianza nulla ⇒ NOT_TESTABLE |
| Quando | una sola volta, al 100° trade chiuso, insieme alla regola primaria. Nessun altro sguardo intermedio |
| Effetto sulla configurazione | **nessuno sul forward test in corso**, qualunque sia l'esito: la regola primaria (PF < 1.0 ⇒ chiusura) decide da sola. Se SUPPORTED, diventa *ammissibile* proporre (con il tuo OK esplicito) un **nuovo** forward test della variante "Donchian con ingresso solo se `stop_pct` ≤ valore del 50° trade per rango". Quel valore è fissato in quel momento e registrato. La variante ha un hash nuovo, riparte da 0 trade, non eredita i 100 trade e segue le stesse regole. Se NOT_SUPPORTED o NOT_TESTABLE, l'ipotesi è chiusa senza azioni |

**Cosa aspettarsi, onestamente.**
- Nel backtest a tier 1 la stessa configurazione ha PF **0.87** e attiva il kill switch totale (`docs/research.md` §K.4). L'esito più probabile è l'abbandono al checkpoint, o prima per kill switch.
- Al ritmo del backtest (~89 trade l'anno sul top-4) il checkpoint arriva in circa **13–14 mesi** e i 300 trade in circa 3.4 anni.

Se rifiuti questa impostazione, l'alternativa coerente è costruire soltanto il bot DCA (circa il 30% dei moduli: config, kraken_rest, state, execution senza OCO, monitor, cli) oppure non costruire nulla. Non c'è un terzo modo onesto.

## 1. Architettura
Un solo processo Python 3.12, `asyncio`, avviato da systemd (`Restart=always`, `WatchdogSec=60`).

```
config ─┬─► kraken_rest ─┐
        ├─► kraken_ws  ──┼─► market_data ─► features ─► signals ─► risk ─► execution
        │                │                                          ▲          │
        └─► state (SQLite WAL) ◄────────────────────────────────────┴──────────┘
                 ▲
             portfolio ─► monitor (event bus, read-only)      cli (wiring, unico punto che importa tutto)
```
Regole di import (verificate in CI con `import-linter`):
- `features` è puro: importa solo numpy e i tipi.
- `signals` importa `features` e i tipi di `market_data`.
- `risk` importa `signals` (tipi), `state` e `portfolio` (letture).
- `execution` importa `risk` (decisioni), `kraken_rest`, `kraken_ws` e `state`.
- `monitor` riceve eventi da un bus interno e non importa nessun modulo di business.
- `cli` è l'unico composition root.
- Nessun modulo importa `cli`.
- Nessun import circolare (contratto "layers").

Dipendenze runtime minime: `httpx` (REST, HTTP/1.1), `websockets`, `pydantic-settings`, `numpy`. Niente pandas, niente ccxt (non semplifica rispetto a un wrapper firmato di circa 300 righe, e nasconderebbe i retry). Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`, `import-linter`, `detect-secrets`.

## 2. Moduli
| Modulo | Responsabilità | Punti critici |
|---|---|---|
| `config` | `pydantic-settings`. Segreti solo da `.env` (permessi 600, verificati all'avvio: altrimenti exit). Strategia e limiti da `config.toml` versionato. Validazione stretta dei range: rischio 0.25–1%, DD massimo ≤20%, holding atteso ≤48 h, posizioni 1–3, timeframe 5m–4h. Fail-fast | `CAPITAL` in `.env`, unico valore dell'utente. All'avvio calcola il capitale minimo sostenibile (§6) e **avvisa/blocca** |
| `kraken_rest` | Firma HMAC-SHA512. Nonce in ms con **regola esplicita `n_k = max(n_{k−1} + 1, clock_ms)`**, dove `n_0` è il massimo persistito: avanza di almeno +1 con orologio fermo, all'indietro o indietro rispetto al valore salvato (test in `tests/unit/test_nonce_rule.py`). Scritto in `meta` **prima** di essere restituito; se la scrittura fallisce non si emette nessun nonce. Token bucket sul contatore REST (Starter 15, −0.33/s) e per-pair sul motore. Backoff esponenziale con jitter. **Retry solo su idempotenti** (letture, Cancel*, CancelAllOrdersAfter); `AddOrder` mai ritentato alla cieca (§4.4) | Classificazione errori dalla guida ufficiale (`docs/research.md` A.8). Test vector della firma preso dalla doc |
| `kraken_ws` | WS v2: `book` (depth 10 + CRC32), `trade`, `ohlc`, `status`, `heartbeat`; privati `executions` (`snap_orders`, `snap_trades`, `ratecounter`) e `balances`, con token REST rinnovato. Deadline 5 s senza messaggi ⇒ reconnect. Backoff 1→60 s con jitter. Dopo ogni reconnect: snapshot autoritativi più **resync REST** (OpenOrders, TradesHistory dall'ultimo fill) | Il book si ricostruisce da zero dopo ogni gap; checksum errato ⇒ resubscribe |
| `market_data` | Normalizza in `Decimal`. Cache candele per coppia e timeframe (ring buffer numpy float64 per gli indicatori, `Decimal` per i prezzi operativi). Una candela è "chiusa" solo dopo `ts_open + tf` più 2 s di grazia, confermata da OHLC REST. Spread e profondità rolling | Zero lookahead: gli indicatori vedono solo le candele chiuse |
| `features` | Funzioni pure: ATR (Wilder), SMA/EMA, rolling max/min, z-score dei rendimenti, filtro di regime e di liquidità | Test anti-lookahead (stessi output su dati troncati, come `research/scripts/test_lookahead.py`) |
| `signals` | `Signal(action, pair, confidence, reason, sl, tp)`, con `signal_id` UUIDv7. `reason` è un dict numerico (valori e soglie): niente testo libero, niente LLM | Deterministico: stesso input, stesso output (test golden) |
| `risk` | Sizing dalla distanza dello stop. Limiti di esposizione. Breaker, cooldown, kill switch, filtro di liquidità, capitale minimo (§6) | Funzioni pure più lo stato persistito in `risk_state` |
| `execution` | Ordini, fill parziali, emulazione OCO, DMS, flatten (§4) | Write-ahead intent su DB prima di ogni invio |
| `state` | SQLite WAL, migrazioni numerate, macchina a stati dei trade (§3) | `synchronous=FULL` sulle tabelle ordini |
| `portfolio` | PnL realizzato e non realizzato (fee reali dai fill), equity curve oraria, esposizione | Ricostruibile al 100% da `fills` |
| `monitor` | Log JSON con `signal_id`/`trade_id`. Redazione automatica (API-Key, API-Sign, secret, token WS, regex base64 ≥40 caratteri). Telegram. Heartbeat (sd_notify più ping opzionale verso un monitor esterno) | Un processo morto non può allertare: serve il monitor esterno (§7) |
| `cli` | `run` (LIVE, richiede `--i-understand-live` più `LIVE_ENABLED=true`), `dry-run` (default), `backtest`, `status`, `flatten`, `halt`, `resume` | `run` senza flag parte in DRY_RUN |

## 3. Schema dati (SQLite, WAL; prezzi e quantità come TEXT Decimal)
Migrazioni: file `migrations/NNNN_*.sql` più `meta.schema_version`; applicate in transazione all'avvio, mai in discesa.
- `meta(key PK, value)`: schema_version, nonce_hwm, run_id, dms_mode.
- `instruments(pair PK, altname, wsname, base, quote, pair_decimals, lot_decimals, cost_decimals, tick_size, ordermin, costmin, status, updated_at)`.
- `candles(pair, tf, ts_open, o, h, l, c, v, n_trades, PK(pair, tf, ts_open))`.
- `signals(signal_id PK, ts, pair, strategy, strategy_version, action, confidence, sl, tp, reason_json)`.
- `trades(trade_id PK, signal_id FK, pair, state, qty_target, qty_filled, avg_entry, stop_price, tp_price, opened_at, closed_at, exit_reason, realized_pnl, fees, version)`: `version` per optimistic locking.
- `orders(local_id PK, trade_id FK, role ∈ {entry, stop, exit, tp, flatten}, cl_ord_id UNIQUE, txid UNIQUE NULL, type, side, price, trigger_price, qty, filled_qty, avg_price, status ∈ {intent, sent, open, partially_filled, filled, canceled, expired, rejected, unknown}, attempt, created_at, updated_at, last_error)`.
- `fills(exec_id PK, txid, trade_id, ts, price, qty, fee, fee_ccy, liquidity ∈ {m, t})`.
- `equity(ts PK, equity, cash, exposure, unrealized, realized, peak, dd)`.
- `risk_state(id=1, consecutive_losses, size_mult, pause_until, halted, halt_reason, day, day_start_equity, peak_equity)`.
- `cooldowns(pair PK, until)`.
- `events(id PK, ts, level, kind, signal_id, trade_id, payload_json)`: audit append-only.
- (0001) `runs`, `heartbeats`, `recon_checks`: base del gate dei 7 giorni.
- (0002, blocco 3) `book_samples(pair, ts, spread_bp, bid_depth_25bp, ask_depth_25bp)`, `spread_daily(pair, day, n, p50, p90, p95, p99, max, depth_p5, depth_p50)`, `liquidity_thresholds(pair, spread_bp_max, depth_min, ref_start, ref_end, n_samples, coverage, derived_at)`, `forward_tests(run_id, started_at, freeze_hash, status, checkpoint_json, secondary_json, closed_at)`, `dca_tranches(tranche_id, seq, mode, window_start, window_end, nominal, carry_in, budget, spent, qty, status, carry_out)`; `risk_state.halt_reason` esteso con `unknown_order` e `recon`.

## 4. Esecuzione (fatti verificati in `docs/research.md` §A)
### 4.1 Macchina a stati del trade
```
SIGNALLED → ENTRY_WORKING ──(0 fill, M reprice esauriti)──► ENTRY_CANCELLED
               │(fill parziale/totale)
               ▼
           OPEN_PROTECTED ⇄ STOP_AMEND (alla chiusura di ogni barra: amend trigger_price, solo al rialzo)
               │ exit signal / time stop / TP watcher / trailing
               ▼
           EXITING (post-only limit sell, reprice M volte) ──fallback──► EXIT_MARKET
               │                                  │
               ▼                                  ▼
             CLOSED ◄──────── STOPPED (stop server eseguito)
Qualsiasi stato con posizione > 0 e nessuno stop valido ⇒ UNPROTECTED ⇒ re-protect immediato o flatten
KILL ⇒ FLATTENING ⇒ CLOSED + HALT
```
Ogni transizione segue lo stesso schema: transazione DB → invio → conferma da `executions` o REST → transazione DB.

### 4.2 Ingresso
1. Creo `trade` più un `order(intent)` con `cl_ord_id` = UUIDv5(trade_id, attempt).
2. `AddOrder`:
   - `ordertype=limit`, `oflags=post`, `price` = best bid arrotondato a `tick_size`;
   - `timeinforce=GTD` con `expiretm=+90`: l'ordine muore lato server anche se il bot muore;
   - `deadline=now+10s`;
   - `close[ordertype]=stop-loss`, `close[price]=stop`: ogni fill genera subito il suo stop sul server.
3. Reprice: `AmendOrder limit_price` con `post_only=true`, ogni 60 s, al massimo **3 volte** e mai oltre `max_chase` (0.25%). Poi cancel per `cl_ord_id`.
4. Fill parziali: ogni fill crea uno stop secondario. Li traccio tutti (`role=stop`) e li modifico tutti in blocco. A fine ingresso, se ci sono più di 3 stop, li consolido: **prima** piazzo lo stop unico (se i fondi lo consentono), **poi** cancello i vecchi. Se il venue riserva i fondi agli stop (smoke test §8.4), consolido solo in condizioni normali di book, con guardia client attiva.

### 4.3 Uscite e OCO emulato
- **Stop**: stop-loss market lato server, trigger `last`, sempre presente con posizione aperta. La logica di trailing lo aggiorna con `AmendOrder trigger_price` a ogni barra chiusa, solo al rialzo.
- **Uscita discrezionale** (segnale, time stop, TP):
  1. Se gli stop non riservano fondi: lo stop resta e piazzo la limit di uscita in parallelo: **OCO vero**, con entrambi i `txid` salvati e cancel del fratello al fill dell'altro, via `executions`.
  2. Se li riservano: cancello lo stop, salvo `guard_price` = stop nel DB, metto la limit post-only all'ask e attivo la **guardia client**: se il best bid scende sotto `guard_price`, market sell immediato. Finestra di uscita massima 3 reprice per 60 s, poi market.
- **TP** (se `Signal.tp` non è None): nessun ordine TP a riposo (verificato: il close condizionale non fa SL+TP). Un watcher su WS book avvia l'uscita discrezionale quando il bid raggiunge il TP.
- Crash durante EXITING: gestito al boot (§4.5, caso b).

### 4.4 Idempotenza e niente doppi invii
- Timeout o `EService:Deadline elapsed` su `AddOrder` ⇒ **non si ritenta**: stato `unknown`.
- Risoluzione:
  - snapshot `executions` (contiene `cl_ord_id`) più `OpenOrders` e `ClosedOrders` recenti;
  - se l'ordine esiste, lo adotto;
  - se dopo 2 cicli non esiste, chiudo l'attempt e quello successivo usa un **nuovo** `cl_ord_id`.
- Kraken rifiuta i `cl_ord_id` duplicati tra gli ordini aperti [V]; non c'è protezione sugli ordini chiusi, da qui la risoluzione obbligatoria.

### 4.5 Riavvio e riconciliazione (sequenza esatta; anche "il processo muore con una posizione aperta")
1. Lock single-instance (`flock`), config valida, permessi `.env`.
2. DB: migrazioni, `nonce_hwm`.
3. REST: `SystemStatus`. Se non `online`, si resta in attesa senza inviare ordini.
4. REST: `AssetPairs` (refresh `instruments`), `TradeVolume` (fee reali), `BalanceEx`, `OpenOrders`, `ClosedOrders` e `TradesHistory` dall'ultimo fill noto.
5. Ricostruzione: per ogni trade non CLOSED, la quantità reale è la somma dei fill da `TradesHistory`, che vince sul DB. Abbinamento ordini per `txid`, poi per `cl_ord_id`.
6. Casi:
   - **(a)** posizione > 0 con stop a riposo di qty corretta ⇒ OPEN_PROTECTED; ricalcolo lo stop sull'ultima barra chiusa ed eventualmente faccio amend.
   - **(b)** posizione > 0 **senza stop** (DMS scattato, crash in EXITING, cancel manuale) ⇒ se bid ≤ stop del DB, market sell; altrimenti stop-loss immediato per l'intera qty. Alert `UNPROTECTED_RECOVERED`.
   - **(c)** entry GTD scaduto ⇒ ENTRY_CANCELLED, oppure OPEN_PROTECTED se ha fillato.
   - **(d)** ordini con prefisso del bot non presenti nel DB (orfani) ⇒ cancel per `cl_ord_id`/`txid`.
   - **(e)** ordini o saldi estranei (manuali) ⇒ mai toccati; alert se ≥ ordermin.
   - **(f)** `risk_state.halted` ⇒ nessun nuovo ingresso, solo protezione.
7. DMS secondo il §5. WS subscribe; secondo cross-check REST contro lo snapshot (race del reconnect).
8. Telegram: riepilogo del recovery. Poi ingresso nel loop.

Tempo massimo senza stop dopo un crash = tempo di riavvio systemd più passi 1–6 (obiettivo < 30 s), **solo se il DMS non ha già cancellato gli stop**. Da qui la scelta del §5.

### 4.6 Riavvio con posizione aperta e stop cancellati dal dead man's switch (sequenza esatta)
**Quando può succedere.**
- Con `DMS_MODE=always`: a ogni crash, freeze o partizione di rete più lunga del timeout (60 s).
- Con `DMS_MODE=entry_only` (default): solo nella finestra residua tra il fill di un ingresso (che crea lo stop secondario) e il disarmo del DMS che il bot invia appena riceve il fill. Il DMS, se armato, scatta anche quando il motore Kraken torna da un downtime.
- Nella configurazione approvata (DCA live senza stop, Donchian solo DRY_RUN) lo scenario non riguarda soldi reali finché il Donchian non viene sbloccato. Lo si implementa e testa comunque ora.

**Fatti verificati su cui si basa:**
- allo scadere il DMS cancella tutti gli ordini, stop compresi, e poi resta disabilitato finché non si invia un nuovo timeout ≠ 0;
- lo stop-loss è un ordine indipendente;
- `EService:Deadline elapsed` richiede di controllare gli ordini prima di ritentare.

**Stato persistito prima del crash** (già previsto dallo schema §3):
- `trades.state` e `trades.stop_price`: l'ultimo livello di stop deciso, scritto **prima** di ogni amend;
- `orders` con `txid`/`cl_ord_id` di ogni stop;
- `fills`;
- `meta.dms_armed_until` e `meta.last_heartbeat`.

**Sequenza al riavvio** (`T0` = avvio del processo; systemd `RestartSec=5`):

| # | Azione | Chiamate | Regola di decisione | Esito / errore |
|---|---|---|---|---|
| 1 | Lock single-instance, config, `.env` 600, DB, migrazioni, `nonce_hwm` | — | se un altro processo ha il lock ⇒ exit | log `BOOT` |
| 2 | **Gate di rischio chiuso**: stato `RECOVERING`, nessun nuovo ingresso finché il passo 14 non chiude | — | — | — |
| 3 | Stato del mercato | `SystemStatus` | `online` ⇒ avanti. `cancel_only` o `maintenance` ⇒ nessun ordine possibile: retry con backoff 5→60 s, alert `UNPROTECTED_WAITING_MARKET` ogni 5 min. `post_only` ⇒ avanti, ma al passo 8 si usa il ramo post-only | — |
| 4 | **Disarmo esplicito del DMS** (idempotente) | `CancelAllOrdersAfter timeout=0` | sempre, prima di qualunque nuovo ordine, così il timer non cancella ciò che si sta per piazzare | errore ⇒ retry ×3 con backoff (è idempotente) |
| 5 | Fotografia del conto | `AssetPairs`, `TradeVolume`, `BalanceEx`, `OpenOrders`, `ClosedOrders` (da `last_heartbeat − 1h`), `TradesHistory` (dall'ultimo fill noto − 1 h) | — | — |
| 6 | Quantità reale per trade | — | `qty_pos` = Σ fill d'acquisto − Σ fill di vendita del trade (fonte: `TradesHistory`, che vince sul DB). Controllo: `BalanceEx[base].balance` ≥ Σ `qty_pos` dei trade su quella base, al netto dei saldi estranei fotografati all'installazione | discrepanza > 1 lot ⇒ alert `RECON_MISMATCH`; si usa il minimo tra fill e saldo |
| 7 | Stato degli stop | — | per ogni stop del DB: se il `txid` è in `OpenOrders`, è vivo; se è in `ClosedOrders` con stato `canceled`, è morto (si salva la `reason`; la stringa esatta del DMS è da registrare nello smoke test [L]); se è `closed` con fill, lo stop è scattato ⇒ trade STOPPED. Trade **UNPROTECTED** se `qty_pos > 0` e la qty degli stop vivi è < `qty_pos` | — |
| 8 | **Ri-protezione**, un trade alla volta, prima quello con il rischio € maggiore (`qty_pos × (bid − stop)`) | `Ticker` (best bid REST; il WS non è ancora su) | `stop_ref` = `trades.stop_price` dal DB. **(8a)** `bid ≤ stop_ref` (lo stop sarebbe già scattato): vendita **market** di `qty_pos`, `cl_ord_id` = UUIDv5(trade_id,"recover",n), motivo `stop_missed_recovery`. **(8b)** `bid > stop_ref`: `AddOrder stop-loss sell qty_pos`, `trigger=last`, `price=stop_ref` arrotondato al tick, `cl_ord_id` = UUIDv5(trade_id,"restop",n). **(8c)** mercato in `post_only`: niente stop possibili; guardia client attiva e, solo se `bid ≤ stop_ref`, limit post-only all'ask con reprice ogni 10 s | `EOrder:Insufficient funds` ⇒ rileggo `BalanceEx.hold_trade`, cancello gli orfani con prefisso bot che trattengono fondi, riprovo **una** volta; se fallisce ancora ⇒ market sell (non potendo proteggere si chiude). Timeout o `Deadline elapsed` ⇒ `QueryOrders`/`OpenOrders` prima di ritentare (§4.4) |
| 9 | Verifica | `QueryOrders(txid)` | lo stop deve risultare `open` con `vol = qty_pos`; poi `trades.state = OPEN_PROTECTED` | non verificato entro 10 s ⇒ ripeti dal passo 8 (stesso `cl_ord_id`: il server rifiuta i duplicati aperti) |
| 10 | Ingressi interrotti | — | trade `ENTRY_WORKING` con `qty_pos = 0` ⇒ `ENTRY_CANCELLED` (il DMS o il GTD li hanno chiusi); con `qty_pos > 0` ⇒ già gestiti ai passi 8–9 | — |
| 11 | **Recupero della logica persa durante il downtime** | `OHLC` (candele chiuse del downtime) | ricalcolo trailing stop e time stop sulle barre chiuse perse. Nuovo stop > bid ⇒ la strategia sarebbe già uscita ⇒ uscita market. Nuovo stop > stop attuale ⇒ `AmendOrder trigger_price` (solo al rialzo). Holding oltre il massimo ⇒ procedura di uscita §4.3 | — |
| 12 | Orfani ed estranei | `CancelOrder` | ordini con prefisso del bot non nel DB ⇒ cancel; ordini senza prefisso ⇒ mai toccati, alert | — |
| 13 | Rischio post-gap | — | ricalcolo equity MTM, perdita giornaliera e drawdown. Kill giornaliero o totale superato (es. gap oltre lo stop) ⇒ flatten e HALT (§6) | — |
| 14 | Riapertura | `CancelAllOrdersAfter` secondo `DMS_MODE` (in `entry_only` resta disarmato perché ci sono stop a riposo); WS subscribe (`executions` con snapshot, `book`, `balances`, `status`, `heartbeat`) | **secondo cross-check**: i fill dello snapshot WS non visti al passo 5 ⇒ si rifanno i passi 6–9 per quei trade. Poi gate di rischio aperto | Telegram `RECOVERED`: durata downtime, trade ri-protetti o chiusi, prezzo d'uscita vs `stop_ref` (slippage del downtime) |

**Tempi obiettivo:**
- dal riavvio a tutte le posizioni ri-protette o chiuse (passi 1–9): **< 30 s** con API sane;
- il rischio non coperto resta dal momento in cui il DMS ha cancellato gli stop fino al passo 9. Dipende dalla durata del downtime ed è **illimitato** se il VPS resta giù. Per questo il default è `entry_only`, e c'è l'heartbeat esterno (§7) che ti avvisa se il processo non torna.

**Test:** `tests/integration/test_recovery_dms.py` copre gli scenari 8a, 8b, 8c, insufficient funds, deadline elapsed, fill arrivato tra REST e WS, mercato in `cancel_only` al riavvio, gap oltre il kill switch giornaliero. Ognuno ha un Kraken mockato che simula lo scadere del DMS (cancellazione di tutti gli ordini e timer disabilitato).

### 4.7 Ordine dal risultato dubbio: percorso di halt (`OutcomeUnknownError`)
**Quando.** Il client REST si ferma con `OutcomeUnknownError` se una chiamata non idempotente (`AddOrder`, `AmendOrder`) potrebbe essere stata eseguita:
- timeout dopo l'invio;
- HTTP 5xx;
- `EService:Deadline elapsed`;
- `EService:Unavailable` o `EGeneral:Internal error` su un ordine;
- risposta illeggibile.

Il client **non ritenta mai** in questi casi (§4.4). La sequenza è la stessa in LIVE e, simulata, in DRY_RUN.

| # | Azione | Dettaglio |
|---|---|---|
| 1 | Marcatura | `orders.status = unknown` per quel `cl_ord_id`; evento critico `ORDER_OUTCOME_UNKNOWN`; `risk_state.halted = 1` con `halt_reason = unknown_order` **per la strategia coinvolta** (DCA o Donchian; l'altra continua) |
| 2 | Gate chiuso | per quella strategia nessun nuovo ingresso, amend o repricing. Restano permesse **solo** le azioni di protezione: stop, ri-protezione, flatten richiesto dal kill switch |
| 3 | Allarme | Telegram **critico** immediato (strategia, coppia, lato, qty, prezzo, `cl_ord_id`, errore), ripetuto ogni 15 minuti finché l'halt è attivo |
| 4 | Risoluzione attiva (fino a 2 minuti) | ogni 5 s: `OpenOrders` e `ClosedOrders` **filtrati lato server per `cl_ord_id`** (parametro documentato da Kraken; `ClosedOrders` con `closetime=open` e `start` = invio − 60 s), poi `QueryTrades` sui trade elencati dall'ordine per i fill. **Trovato** ⇒ si adotta (`txid`, stato, fill), `status` reale, evento `ORDER_RESOLVED`. **Assenza provata** ⇒ `status = rejected`. Prova di assenza: siamo oltre `deadline` + 60 s (il motore rifiuta gli ordini non processati entro il `deadline`, 10 s), entrambe le interrogazioni sono riuscite, la pagina di `ClosedOrders` **non è troncata** (`count` = righe ricevute; altrimenti è UNAVAILABLE) e l'ordine non c'è. Ogni interrogazione lascia un evento `ORDER_EVIDENCE`. *Scostamento dal testo originale:* il canale WS privato `executions` non è ancora costruito (arriva con il Donchian, blocco 6); `TradesHistory` è sostituito da `QueryTrades`, perché un ordine assente da Open e Closed non può avere fill |
| 5 | **Ri-protezione delle posizioni aperte** (subito dopo il passo 4, anche se la risoluzione è fallita) | quantità reale = fill da `TradesHistory` più `BalanceEx` (mai la qty del DB), come nel §4.6 passi 6–9. **Ordine dubbio di acquisto** (ingresso o DCA): se ha fillato, la posizione esiste; per il Donchian lo stop condizionale nasce col fill, altrimenti si piazza uno stop per la qty reale (§4.6 8b). Il DCA non ha stop: si aggiorna solo il budget speso dai fill reali. **Ordine dubbio di vendita** (uscita o stop): stop per `min(qty_reale, saldo disponibile)`, così non si vende due volte. Se il bid è già sotto lo stop ⇒ market sell della qty reale. Tutto con il `deadline` attivo |
| 6 | Esito | entro i 2 minuti: ordine trovato o assenza **provata** (passo 4), **e** posizioni protette e coerenti ⇒ l'halt si toglie da solo (`ORDER_RESOLVED` con `halt_cleared=true`, più `HALT_CLEARED`). In tutti gli altri casi ⇒ **halt persistente**, compreso il caso di Kraken **irraggiungibile o in errore oltre i 2 minuti**: l'assenza di risposta **non è mai** prova di assenza e l'ordine non si dà mai per "mai piazzato". L'halt resta anche attraverso i riavvii. Viene registrato `HALT_PERSISTENT` con allarme critico. In background le interrogazioni continuano ogni 60 s solo per **raccogliere evidenze** (`ORDER_EVIDENCE`, `evidence_only`: nessun cambio di stato), mostrate da `status` e `resolve`; l'halt persistente si toglie **solo** con `krakbot resolve` |
| 7 | Al riavvio | la riconciliazione del §4.5 risolve gli `unknown` del DB per `cl_ord_id` e `txid` **prima** di qualunque altro ordine (stesso criterio di prova del passo 4). La ri-protezione del §4.6 viene eseguita comunque. Un `halt_reason = unknown_order` già persistito resta attivo finché il passo 6 non è soddisfatto |
| 8 | Contabilità | tutto il PnL e il budget DCA si ricalcolano dai fill reali; un ordine dubbio poi risultato eseguito non viene mai reinviato (dopo la risoluzione il `cl_ord_id` dell'attempt successivo è nuovo) |

**Comando `krakbot resolve <cl_ord_id>`** (l'unico modo documentato per uscire da un halt persistente; nessuna modifica manuale del DB):
1. Mostra l'ordine dubbio dal DB (strategia, coppia, lato, qty, prezzo, ora di invio, `deadline`) e l'halt attivo.
2. Interroga Kraken **in quel momento**: `OpenOrders`, `ClosedOrders` (da invio − 60 s), `QueryOrders` (se c'è un `txid`), `TradesHistory`, cercando per `cl_ord_id` (che `OpenOrders`/`ClosedOrders` restituiscono [V]). Stampa cosa ha trovato.
3. Esiti:
   - **trovato** ⇒ propone l'adozione (`txid`, stato, fill reali), chiede conferma e la registra;
   - **assenza provata**, con le stesse condizioni del passo 4 (siamo oltre `deadline` + 60 s e tutte e tre le fonti hanno risposto con successo) ⇒ propone `rejected`, chiede conferma e la registra;
   - **Kraken irraggiungibile o risposta parziale** ⇒ **rifiuta di sbloccare** ed esce con codice ≠ 0. L'unica eccezione è `--operator-confirms-absent --reason "<testo>"`: dichiari di aver verificato tu sull'interfaccia Kraken che l'ordine non esiste. L'ordine diventa `rejected` con `resolved_by=operator` e motivo in un evento critico. Se in seguito arrivasse un fill su quell'ordine, la riconciliazione lo rileva (`FILL_ON_REJECTED_ORDER`) e rimette la strategia in halt (`halt_reason = recon`).
4. Dopo un'adozione o un `rejected`, esegue la ri-protezione (passo 5) e toglie l'halt **solo** se per quella strategia non restano altri ordini `intent`/`sent`/`unknown`.
5. Ogni azione finisce negli `events` (append-only) con l'evidenza raccolta: la storia dell'halt resta ricostruibile.
6. `--dry` mostra solo l'evidenza senza modificare nulla.

Implementazione (blocco 4, `src/krakbot/cli.py`): per un ordine LIVE servono le chiavi (solo permessi di query, client costruito con il trading disabilitato); per un ordine DRY_RUN il book simulato è morto col processo, quindi l'assenza si prova solo dopo `deadline` + 60 s, come per un GTD reale. `--operator-confirms-absent` richiede `--reason` e `--yes`. Codici di uscita: 0 risolto o nulla da fare, 2 rifiutato (l'halt resta), 1 errore. `krakbot resume` **non** toglie mai un halt `unknown_order` e rifiuta se esistono ordini non risolti: rimanda a `resolve`.

**Comandi della CLI** (blocco 4): `dry-run` (forza DRY_RUN), `run --live --i-understand-live` (config `mode=live` + `KRAKBOT_LIVE_ENABLED` + chiavi + gate dei 7 giorni, ricontrollato anche dentro l'app), `status`, `halt --reason`, `resume`, `resolve`, `spreads [--days]`, `liquidity-derive [--yes]` (rifiuta se i 7 giorni di riferimento non sono tutti giorni DRY_RUN puliti), `gate`, `flatten` (non vende nulla: Donchian solo DRY_RUN, DCA mai venduto), `backtest` (rimanda agli script di ricerca).

**Schema** (migrazione 0002, blocco 3): `risk_state.halt_reason` accetta anche `unknown_order` e `recon`; `orders.status = unknown` esiste già.
**Test** (blocco 4 per il DCA, poi Donchian): timeout dopo l'invio con ordine poi trovato aperto, poi trovato fillato, poi assente provato; REST giù per oltre 2 minuti, quindi halt persistente che **non** si sblocca da solo nemmeno quando l'evidenza torna disponibile, e `resolve` che rifiuta di sbloccare senza evidenza; ordine dubbio di vendita e ri-protezione senza doppia vendita. Stato: la parte DCA è coperta in `tests/unit/test_dca_engine.py`, `test_cli_app.py`, `test_live_broker_dms.py`; la vendita dubbia e la ri-protezione arrivano col Donchian (blocco 6).

## 5. Dead man's switch: deviazione motivata dal requisito
Requisito: `CancelAllOrdersAfter` a 60 s rinnovato a ogni ciclo. Fatto verificato: allo scadere cancella **tutti** gli ordini, **stop compresi**. Applicato alla lettera, ogni crash o partizione di rete oltre 60 s lascia le posizioni nude: il contrario di "crash-safe".
- **Default `DMS_MODE=entry_only`**: timer armato (timeout 60 s, refresh ogni 20 s) solo mentre ci sono ordini di ingresso aperti **e** nessuno stop a riposo. Disarmato (`timeout=0`) appena esiste uno stop, e prima delle manutenzioni annunciate sul canale `status`. Gli ingressi sono comunque GTD più `deadline`, quindi non sopravvivono al bot.
- `DMS_MODE=always` resta disponibile, così com'era richiesto, con warning all'avvio e nel README: "un crash con posizione aperta la lascia senza stop dopo 60 s".
- Il "dead man's switch" del monitor è un'altra cosa: heartbeat esterno (§7).
- Test (§8): mock che verifica arm/refresh/disarm per stato, più un test di crash simulato in entrambe le modalità.

## 6. Rischio (soglie numeriche e comportamento)
- **Sizing**: `qty = floor_lot( equity_MTM × risk_pct × size_mult / (entry − stop) )`. Con `risk_pct` = 0.5% (range ammesso 0.25–1%), cap 15% dell'equity per posizione e 30% totale, massimo 3 posizioni, una per coppia, niente piramidi né medie. Se `qty×price < max(ordermin×price, costmin) × 1.5` il trade si salta, con log del motivo.
- **Capitale minimo** all'avvio: per ogni coppia, `min_capital = 1.5 × max(ordermin×price, costmin) / min(risk_pct × 0.5 / stop_tipico, 0.15)`. Se `CAPITAL` è sotto, warning e coppia disabilitata; se lo sono tutte, exit. Con prezzi e stop mediani di oggi (stop 5.3–10.6%) il minimo per il candidato è circa 70–115 EUR per coppia; per il DCA ~851 EUR (`docs/research.md` §F). Il vincolo vero non sono i minimi ma il tier fee (`docs/research.md` §D).
- **Breaker**: 3 perdite consecutive ⇒ size ×0.5 fino al primo trade vincente. 5 perdite ⇒ nessun ingresso per 24 h, poi si riparte a ×0.5. Le posizioni aperte restano gestite.
- **Cooldown**: 2 barre del timeframe dopo uno stop sulla stessa coppia.
- **Kill giornaliero** (libro del Donchian): equity MTM < equity a inizio giornata UTC × (1 − 4%) ⇒ cancel all degli ordini del Donchian, market sell delle sue posizioni, `halted=true` (`halt_reason=daily`). Ripresa **solo** con `cli resume`. In DRY_RUN tutto è simulato: sospensione 24 h con ripresa automatica (§0.2).
- **Kill totale** (libro del Donchian): equity MTM < picco × (1 − 15%) ⇒ come sopra, con `halt_reason=total`. Ripresa solo con `cli resume --ack-drawdown`. La soglia 15% lascia margine sotto il 20% massimo per gap e slippage del flatten.
- **Ambito dei kill switch: decisione confermata (2026-11-22).** Il flatten **non vende mai i BTC accumulati dal DCA**. Vendere sul drawdown contraddice il DCA, e il rischio del DCA è già limitato dall'allocazione (0.20 × 76.6% ≈ 15% di DD di conto).
  - Al DCA si applicano solo gli halt operativi: ordine dubbio (§4.7), errori API ripetuti, riconciliazione fallita. Fermano i nuovi acquisti, non vendono.
  - Il flag `strategies.dca.kill_switch_sells_holdings` esiste, è `false` e resta spento. È fuori dall'hash del forward test.
- **Filtro di liquidità** (soglie derivate, sostituiscono quelle del campione di 30 minuti):
  1. **Raccolta.** In DRY_RUN ogni 10 s, per ogni coppia candidata (XBT/EUR più le prime 8 EUR per volume, così l'universo top-4 point-in-time ha sempre dati), si salvano in `book_samples` lo spread in bp e la profondità bid/ask entro 25 bp. Campionamento a intervallo fisso, non a evento, per non pesare di più i momenti concitati.
  2. **Rapporto giornaliero.** In `spread_daily`: n, p50, p90, p95, p99 e massimo dello spread per coppia, più p5 e p50 della profondità.
  3. **Definizione di percentile** (unica, nearest-rank): il p-esimo percentile di n campioni ordinati è il campione di posizione ⌈p/100 · n⌉.
  4. **Derivazione.** Sui 7 giorni UTC di DRY_RUN puliti (gli stessi del gate DCA), per ogni coppia:
     - `spread_bp_max` = **p95** dello spread;
     - `depth_min` = **p5** della profondità entro 25 bp sul lato da consumare;
     - una coppia con meno del 90% dei campioni attesi è **esclusa** finché non ha dati sufficienti.
  5. **Regola d'ingresso** (Donchian): ingresso solo se spread corrente ≤ `spread_bp_max` **e** profondità ≥ max(`depth_min`, 10 × nozionale).
  6. **DCA:** nessun nuovo ordine mentre lo spread XBT/EUR supera il **p99** a 7 giorni (gli ordini già esposti restano).
  7. Prima dei 7 giorni non esistono soglie: il Donchian simulato non entra e il DCA non applica la pausa.
  8. Le soglie si congelano nell'hash del forward test. Prima di congelare, il baseline di backtest (`docs/research.md` §K.4) si ricalcola con gli spread mediani misurati, che sostituiscono quelli dei 30 minuti.
- **Filtro di regime**: dalla strategia; con regime sfavorevole non si apre.

## 7. Monitoraggio
- Log JSON su stdout (journald) più file ruotato.
- Telegram: apertura, chiusura, stop, errore API, disconnessione WS oltre 30 s, kill switch, recovery, breaker.
- `/status` (CLI e comando Telegram con whitelist del chat_id): equity, posizioni, PnL, breaker, DMS, tier fee, ultimo heartbeat.
- systemd `WatchdogSec=60` con sd_notify a ogni ciclo sano.
- `HEALTHCHECK_URL` opzionale: ping ogni 60 s verso un servizio esterno che avvisa se il ping manca. È l'unico modo per sapere che il processo è morto.

## 8. Piano di test
1. **Unit** (pytest):
   - config: range e fail-fast;
   - Decimal: arrotondamento a tick/lot, ordermin/costmin;
   - nonce monotono anche dopo restart;
   - firma: test vector della doc Kraken;
   - token bucket e classificazione degli errori;
   - features anti-lookahead;
   - signals golden;
   - risk: sizing, cap, breaker, kill, cooldown;
   - state machine: transizioni lecite e illecite;
   - redazione dei log.
2. **Integrazione con Kraken mockato** (`respx` per REST, server WS locale): fill totali e parziali con stop per-fill, reprice e cancel, `Deadline elapsed` senza doppio invio, rate limit e backoff, disconnessione WS con resync, checksum errato, manutenzione e `cancel_only`.
3. **Riconciliazione**: un test per ciascun caso del §4.5 (a–f), più crash iniettati tra "scrittura intent" e "invio" e tra "invio" e "conferma".
4. **DMS**: arm/refresh/disarm per stato; scenario crash in `entry_only` (stop sopravvive) e in `always` (stop cancellato, poi recovery caso b).
5. **Backtest**: riproducibile con seed fisso (hash del report uguale tra due run), stesso motore di esecuzione della ricerca (`research/scripts/bt.py`, portato in Decimal).
6. **Smoke test live** (dopo l'OK, size minima, manuale e guidato), per chiudere le [L] della ricerca: (i) uno stop non triggerato riserva i fondi? (ii) REST `close[]` genera un secondario per ogni fill? (iii) REST restituisce `cl_ord_id` negli ordini? (iv) tier reale da `TradeVolume`.
7. **Accettazione**: 7 giorni di DRY_RUN senza crash e con stato coerente (riconciliazione giornaliera DB ↔ simulazione senza differenze). Test verdi, `ruff` e `mypy` puliti, `detect-secrets` pulito.

## 9. Struttura del repo (deliverable dopo l'OK)
```
krakbot/  pyproject.toml  .env.example  Dockerfile  docker-compose.yml  README.md
  src/krakbot/{config,kraken_rest,kraken_ws,market_data,features,signals,risk,execution,state,portfolio,monitor,cli}.py
  src/krakbot/strategies/{dca.py,candidate.py}
  migrations/0001_init.sql   deploy/krakbot.service
  tests/{unit,integration}/   docs/{research.md,strategy.md,plan.md,runbook.md}   research/ (questo studio)
```
Ordine di scrittura (aggiornato): config e schema (1), kraken_rest (2), state (3), **verticale DCA** (4–5), Donchian in DRY_RUN (6), avvio del forward test (7), deploy completo (8).

## 10. Stato di avanzamento (blocchi)
| Blocco | Contenuto | Stato | Verifica |
|---|---|---|---|
| 0 | Condizioni pre-codice | ✅ | (1) BTC/EUR non è nella schedule 0.20/0.20 ⇒ nessuna rimisura (`docs/research.md` §K.1). (2) Il tier 0.10/0.20 non esiste; Tier 7 (0.10/0.22) a 400k USD di AoP, Tier 8 (0.08/0.20) a 600k: non conviene (§K.2). (3) Sequenza DMS nel §4.6 |
| 1 | `config` (+ `.env.example`, `config.toml`), `numeric`, `domain`, `state/db` (migrazioni con checksum, WAL), schema `0001_init.sql`, `state/gates` (7 giorni DRY_RUN puliti) | ✅ | `pytest`: 79 passati; `ruff`: pulito; `mypy --strict`: pulito; wheel con le migrazioni incluse |
| 2 | `kraken_rest`: firma HMAC verificata sul test vector ufficiale; nonce in ms persistito prima dell'invio, mai decrescente; richieste private serializzate; limiter REST/engine/pubblico allo 0.8 della soglia del tier Starter; classificazione errori; retry solo se l'ordine certamente non è stato eseguito o la chiamata è idempotente, altrimenti `OutcomeUnknownError`; trading bloccato prima di ogni I/O se non abilitato; validazione locale di tick/lot/ordermin/costmin | ✅ | `pytest`: 148 passati (Kraken mockato con respx); `ruff` e `mypy --strict` puliti. Smoke test **in sola lettura** sulle API pubbliche reali (Time, SystemStatus, AssetPairs, Ticker): parsing corretto. Bug trovato e corretto: loop infinito del limiter su residui float, con test di regressione. Nessuna API key usata |
| 3 | `state`: repository (`repo.py`, `liquidity.py`, `dca.py`, `forward.py`) e migrazione 0002. Macchina a stati persistita con optimistic locking ed eventi nella stessa transazione. Ordini in write-ahead (`intent` prima dell'invio, `unknown` lasciabile solo con evidenza). Fill idempotenti su `exec_id`; `filled_qty` ricalcolato sempre dai fill; overfill rifiutato; un fill su ordine `rejected` fa escalation. Halt per strategia, rimossi solo se il motivo coincide. Percentili nearest-rank e derivazione delle soglie per coppia con copertura minima. Tranche DCA che non possono superare il budget (regola rivista nel blocco 4: il fill reale si registra sempre, vedi §0.1). Registro dei forward test (uno solo in corso; hash cambiato ⇒ chiusura) | ✅ | `pytest`: 267 passati, tra cui la migrazione 0001→0002 su DB con dati e la ricostruzione di `risk_state` senza perdite; `ruff` e `mypy --strict` puliti |
| 4 | **Verticale DCA** (anticipata: è l'unica parte che toccherà soldi e l'unica con aspettativa positiva). `kraken_ws` (book v2 con checksum CRC32, trade, heartbeat, riconnessione con backoff); `market_data` (top of book da WS, fallback REST, freschezza 10 s) e campionatore a griglia fissa di 10 s (solo da WS fresco) con percentili giornalieri; strategia DCA pura (§0.1); broker DRY_RUN (fill maker solo su trade **strettamente sotto** il prezzo, rifiuto post-only, scadenza GTD) e broker LIVE (AddOrder post-only GTD con `deadline`, lookup per `cl_ord_id`); motore DCA con halt su ordine dubbio e risoluzione solo su evidenza (§4.7); DMS `entry_only`; riconciliazione oraria; `monitor` (log JSON con redazione, Telegram con dedup, heartbeat DB/systemd/ping esterno); `app` (composizione e loop) e `cli` con `resolve` | ✅ | `pytest`: 347 passati; `ruff` (src e test) e `mypy --strict` puliti. Smoke test reale in DRY_RUN, **solo dati pubblici, nessuna chiave**: AssetPairs/Ticker/WS v2 reali, 7 campioni per ciascuna delle 8 coppie in 70 s, heartbeat, riconciliazione ok, uscita pulita su SIGINT. Difetti trovati e corretti: client REST senza URL assoluto (in produzione ogni chiamata sarebbe fallita); fee arrotondata per eccesso che faceva rifiutare un fill reale (`OverBudget`); lookup su `ClosedOrders` esposto alla paginazione da 50; campo `level` dei log sovrascrivibile da un `extra`; refresh di AssetPairs ogni 24 h mancante |
| 5 | Accettazione DCA: 7 giorni di DRY_RUN puliti sul VPS e soglie di liquidità derivate; poi smoke test live a size minima e DCA LIVE (con tuo OK) | — | — |
| 6 | Donchian (solo DRY_RUN): `features`, `signals`, `risk` (sizing dallo stop, breaker, kill), OCO emulato, DMS `entry_only`, recovery §4.5–4.6 su stop simulati, `portfolio` paper a tier 1 | — | — |
| 7 | Avvio del forward test: ricalcolo del baseline con gli spread misurati, congelamento (hash e soglie), poi via. Checkpoint a 100 trade | — | — |
| 8 | Deploy (Dockerfile, compose, systemd), README VPS, runbook, `docs/strategy.md`. Le parti necessarie al DCA arrivano già nel blocco 4/5 | — | — |

**Ramo di ricerca parallelo (sizing)**: chiuso, il pattern non regge (`docs/research.md` §K.5). Configurazione del forward test invariata; si congela all'avvio (§0.2) quando i blocchi 3–7 saranno pronti.
