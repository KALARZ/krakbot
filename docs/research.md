# Dossier di ricerca — Fase 0 (Kraken Spot, long-only, senza leva)

Data: 2026-11-22 (UTC). Autore: Claude (quant dev). Lo studio è riproducibile: script in `research/scripts/`, output in `research/out/` (comandi in §J).

## Sintesi (leggere per prima)
- **Verdetto: NO-GO per il trading attivo.** Nessuna delle 5 famiglie compatibili con spot long-only (time-series momentum, breakout Donchian, breakout da compressione di volatilità, continuazione dopo impulso, mean-reversion su iperestensioni) supera i gate fuori campione. Test usato: walk-forward pre-registrato, 12 semestri 2021–2026, universo point-in-time con monete poi delistate, purge ed embargo di 72 h, regole di rischio complete, fee Kraken reali.
- Risultati OOS: PF netto **0.78–0.95**, Sharpe **−0.48…−0.09**, finestre positive **20–42%**; in 4 famiglie su 5 il kill switch del 15% si attiva. Senza kill switch: PF 0.78–0.89, DD fino al 31% (§E.8).
- **Causa principale: i costi.** Al tier 1 di oggi (0.40% maker / 0.80% taker, verificato) il round-trip costa 0.9–1.3%. Con holding ≤48 h l'expectancy lorda per trade delle famiglie migliori vale **1.0–1.3× il round-trip**, contro un requisito di 3×.
- L'edge di letteratura (momentum su orizzonti di giorni-settimane) è presente nei dati ma debole (t ≤ 2.7, §C.4) e concentrato nel 2021.
- I migliori risultati per singola coppia dello screening iniziale (PF fino a 2.0) erano **selezione a posteriori**: con gli stessi parametri sulle 10 coppie si scende a PF 0.91–1.19 (audit, §H).
- Alternativa onesta (§G): non fare trading attivo. Se si vuole esposizione cripto, DCA su BTC/EUR con ordini maker e allocazione massima del 20% del capitale, che tiene il drawdown di conto storico sotto il 20%. È esposizione beta, non un edge.

## A. Verifica della piattaforma Kraken Spot

Fonte primaria: documentazione ufficiale scaricata il 2026-11-22 da `docs.kraken.com` (versione markdown delle pagine, OpenAPI `spot-rest.yaml` v1.1.0), salvata in `research/docs_raw/`. Fee da `www.kraken.com/pro/fees`. Dati live da `api.kraken.com/0/public/*`.
Legenda: **[V]** = verificato sulla doc o sui dati live · **[A]** = assunzione non verificabile da questo ambiente (motivo indicato) · **[L]** = da verificare con lo smoke test live a size minima prima del go-live.

### A.1 Endpoint
| Area | Endpoint | Stato |
|---|---|---|
| Market data | `GET /0/public/Time`, `SystemStatus`, `AssetPairs`, `Ticker`, `OHLC`, `Depth`, `Trades`, `Spread` | [V] doc + chiamate live |
| Account | `POST /0/private/Balance`, `BalanceEx` (campo `hold_trade`), `OpenOrders`, `ClosedOrders`, `QueryOrders`, `TradesHistory`, `TradeVolume` | [V] doc |
| Trading | `AddOrder`, `AmendOrder` (atomico, mantiene gli id), `EditOrder` (legacy cancel-new), `CancelOrder`, `CancelAll`, `CancelAllOrdersAfter`, `AddOrderBatch`, `CancelOrderBatch`, `GetWebSocketsToken` | [V] doc |
| WebSocket v2 | `wss://ws.kraken.com/v2`: canali `book` (CRC32 sui top-10), `trade`, `ohlc`, `instrument`, `status`, `heartbeat`; privati `executions` (stati ordine + fill, sostituisce i v1 `openOrders`/`ownTrades`), `balances`; metodi `add_order`, `amend_order`, `cancel_order`, `cancel_all`, `cancel_after`, `batch_add` | [V] doc. **Non testato**: il proxy di questo ambiente non supporta l'upgrade WebSocket |
| Auth | `API-Sign` = HMAC-SHA512(URI path + SHA256(nonce + POST data)), con secret decodificato da base64. Nonce intero a 64 bit sempre crescente | [V] doc |

Nota: la richiesta originale cita `ownTrades`/`openOrders` (nomi WS v1). In v2 lo stesso servizio è il canale `executions`; il bot userà v2 (raccomandato dalla doc).

### A.2 Tipi d'ordine e parametri
- `ordertype` ∈ {market, limit, iceberg, stop-loss, take-profit, stop-loss-limit, take-profit-limit, trailing-stop, trailing-stop-limit, settle-position} [V].
- `oflags`: `post` (post-only, solo limit), `fcib`/`fciq` (valuta della fee), `viqc` (solo buy market), `nompp` deprecato [V].
- `timeinforce` GTC/IOC/GTD/FOK. Con GTD: `expiretm` assoluto o `+n` secondi, minimo 5 s, massimo 1 mese [V].
- `deadline`: RFC3339, da now+2 s a now+60 s; oltre, il matching engine rifiuta l'ordine [V].
- `validate=true` valida senza eseguire [V]. Non è documentato se controlli i fondi [A].
- `cl_ord_id` (UUID o testo ≤18 caratteri): **unicità imposta da Kraken sugli ordini aperti** [V]. È mutuamente esclusivo con `userref` (intero non univoco) [V]. Decisione: si usa `cl_ord_id` deterministico per entry (vedi `docs/plan.md`) al posto di `userref`. È l'unico identificatore lato client la cui unicità è garantita dal server.
- `trigger` ∈ {last, index}, default last; con feed index assente si usa last [V].
- Stop-loss = market al trigger, quindi fee taker. Lo slippage può essere "significativo" (support art. 7699391647892) [V].
- `AmendOrder`: modifica prezzo limite, prezzo trigger, quantità; mantiene `txid` e `cl_ord_id`. Con `post_only` la modifica viene rifiutata se l'ordine incrocerebbe il book [V]. Cambiando il prezzo si perde la priorità in coda [V].

### A.3 `close` condizionale e OCO
- `close[ordertype]` ∈ {limit, iceberg, stop-loss, take-profit, stop-loss-limit, take-profit-limit, trailing-stop, trailing-stop-limit} [V]. Il secondario nasce all'esecuzione del primario, con quantità uguale e direzione opposta; da lì è un **ordine indipendente** [V].
- Il secondario **non può essere insieme TP e SL**: non è OCO (support art. 360038640052) [V].
- WS v2 `conditional`: **ogni fill del primario genera un nuovo ordine secondario** con la quantità eseguita [V] (`docs_raw/api-reference_spot-websocket-v2_add_order.md:208`). Che REST `close[]` si comporti allo stesso modo (stesso engine) è [A] → [L].
- **OCO**: esiste nell'interfaccia Kraken Pro (pagina order-types), ma **non è nell'enum `ordertype` dell'API** [V]. Va quindi emulato lato client.
- Fondi impegnati: `available = balance + credit − credit_used − hold_trade`, e `hold_trade` include gli ordini spot non margin [V]. Che uno **stop-loss non ancora triggerato** riservi la quantità base non è documentato esplicitamente [A]. Assunzione conservativa: sì. In tal caso **SL e TP sull'intera quantità non possono stare entrambi a riposo** [L].

### A.4 `CancelAllOrdersAfter` (dead man's switch)
[V] Allo scadere del timer **cancella tutti gli ordini del client, stop di protezione compresi**, poi il timer resta disabilitato finché non se ne imposta uno nuovo. Uso raccomandato: chiamata ogni 15–30 s con timeout 60 s, da disattivare prima delle manutenzioni. Se il timer è attivo durante un downtime del motore, al ritorno tutti gli ordini vengono cancellati.
**Conseguenza progettuale (critica):** con il DMS a 60 s richiesto, un crash del processo con posizione aperta cancella lo stop sul server dopo 60 s e lascia la posizione scoperta. La risoluzione è in `docs/plan.md` §5.

### A.5 Fee (tier verificati il 2026-11-22)
| Tier | Volume spot 30 g (USD) | oppure asset su piattaforma (USD) | Maker | Taker |
|---|---|---|---|---|
| 1 | ≥0 | — | **0.40%** | **0.80%** |
| 2 | ≥2.5k | — | 0.30% | 0.60% |
| 3 | ≥10k | ≥20k | 0.22% | 0.38% |
| 4 | ≥25k | ≥50k | 0.20% | 0.35% |
| 5 | ≥50k | ≥100k | 0.15% | 0.30% |
| 6 | ≥100k | ≥200k | 0.12% | 0.25% |
| 7 | ≥250k | ≥400k | 0.10% | 0.22% |
| 8 | ≥500k | ≥600k | 0.08% | 0.20% |
| 9 | ≥1M | ≥1M | 0.06% | 0.18% |

- Vale il **criterio più favorevole** tra volume spot 30 g, volume futures e asset su piattaforma; i criteri non si sommano [V].
- Le coppie stablecoin/FX hanno una tabella simmetrica a parte (0.20%/0.20% al tier 1, 0.16% da 50k USD di volume, …) [V]. Si applica solo se:
  - **la base è una stablecoin** (USDT/USD);
  - **entrambe le valute sono fiat** (EUR/USD);
  - **entrambe sono stablecoin** (USDG/USDC);
  - **la coppia è pegged** (WBTC/BTC).

  Con una crypto in base e una stablecoin o fiat in quote vale la schedule spot normale (support art. 360039299431: *"If the stablecoin is the quote currency (e.g. BTC/USDT): The spot crypto fee schedule applies"*) [V]. **BTC/EUR, ETH/EUR, SOL/EUR, … sono quindi sulla schedule 0.40/0.80** [V]. I trade su coppie stablecoin/FX non contano per il volume a 30 giorni [V].
- **Modifica del 9 luglio 2026** (support art. "Cross-Platform Fee Tiers Explained") [V]:
  - il tier è il migliore tra volume spot a 30 giorni e Assets on Platform (AoP);
  - l'AoP è il valore USD **corrente**, non una media: wallet crypto/fiat/tokenized più Opt-In Rewards. Se il mercato scende o si preleva, il tier cambia subito;
  - nella regione servita i futures non contano;
  - la tabella dell'articolo coincide con quella di kraken.com/pro/fees qui sotto. Sopra il tier 12 ci sono i tier Pro 1–5, oltre i 50M USD di volume.
- Lo sconto di −0.02% vale solo per una lista di coppie che **non include** BTC, ETH, SOL, XRP, ADA, LINK, LTC, DOGE, AVAX, DOT in EUR o USD [V].
- Rispetto al vecchio 0.25/0.40 il tier 1 è **raddoppiato**. È il fatto che pesa di più su tutto lo studio.
- `AssetPairs` oggi restituisce `fees`/`fees_maker` **vuoti** [V, live]. Le fee reali del conto vanno lette da `TradeVolume` (`fees`, `fees_maker` per coppia e `inputs`: volume 30 g e asset) [V doc]. Il bot lo chiama all'avvio e ogni ora; il tier effettivo del conto è [A] → [L].

### A.6 Limiti di rate
- REST: contatore per API key. Starter max 15 con decay −0.33/s, Intermediate 20 / −0.5/s, Pro 20 / −1/s. Le chiamate ledger/trade history costano +2 [V].
- Il motore di trading ha un contatore **per coppia**, condiviso tra REST, WS e FIX. Soglia Starter 60, Intermediate 125, Pro 180; decay per secondo 1 / 2.34 / 3.75 [V].
  - Add +1. Amend +1 fisso, più +3/+2/+1 se l'ordine ha meno di 5/10/15 s.
  - Cancel: +8 sotto 5 s, +6 sotto 10 s, +5 sotto 15 s, +4 sotto 45 s, +2 sotto 90 s, +1 sotto 300 s.
  - Massimo ordini aperti per coppia: 60 / 80 / 225 [V].
- Errori: `EAPI:Rate limit exceeded`, `EOrder:Rate limit exceeded`, `EService:Throttled:[ts]` [V].
- Il bot fa al massimo qualche decina di transazioni al giorno: margine > 10×.

### A.7 Specifiche delle coppie (AssetPairs live, 2026-11-22)
| Coppia | key | pair_decimals | lot_decimals | tick_size | ordermin | costmin |
|---|---|---|---|---|---|---|
| XBT/EUR | XXBTZEUR | 1 | 8 | 0.1 | 0.00006 | 0.45 EUR |
| ETH/EUR | XETHZEUR | 2 | 8 | 0.01 | 0.002 | 0.45 |
| SOL/EUR | SOLEUR | 2 | 8 | 0.01 | 0.05 | 0.45 |
| XRP/EUR | XXRPZEUR | 5 | 8 | 0.00001 | 4 | 0.45 |
| ADA/EUR | ADAEUR | 6 | 8 | 0.000001 | 20 | 0.45 |
| LINK/EUR | LINKEUR | 5 | 8 | 0.00001 | 0.35 | 0.45 |
| LTC/EUR | XLTCZEUR | 2 | 8 | 0.01 | 0.08 | 0.45 |
| XDG/EUR | XDGEUR | 7 | 8 | 0.0000001 | 50 | 0.45 |
| AVAX/EUR | AVAXEUR | 3 | 8 | 0.001 | 0.5 | 0.45 |
| DOT/EUR | DOTEUR | 4 | 8 | 0.0001 | 3.9 | 0.45 |
| XBT/USD | XXBTZUSD | 1 | 8 | 0.1 | 0.00006 | 0.5 USD |

(Valori di oggi; il bot li rilegge da `AssetPairs` a ogni avvio e non usa mai costanti.)

### A.8 Errori e politica di retry (dalla guida errori)
- `EAPI:Invalid nonce` → nuovo nonce, mai riusarlo.
- `EService:Unavailable` → backoff esponenziale da 1 s a 60 s.
- **`EService:Deadline elapsed` → controllare `OpenOrders` prima di ritentare: l'ordine potrebbe essere stato piazzato** [V].
- `EOrder:Insufficient funds` → non ritentare.
- `EGeneral:Internal error` → backoff.
- `EService:Market in cancel_only/post_only mode` → attendere.
- Il canale WS `status` riporta `online/cancel_only/maintenance/post_only` e gli avvisi di manutenzione [V].

### A.9 Non verificabile da qui
- Comportamento WebSocket (nessun upgrade attraverso il proxy dell'ambiente).
- Endpoint privati (nessuna API key, per scelta di sicurezza): tier reale, funds hold sugli stop non triggerati, `close[]` per-fill via REST, `validate` sui fondi.
- Spread e book Binance (host non consentito).
- Storico dei book Kraken (non esiste un endpoint storico).

## B. Letteratura

Riferimenti verificati via ricerca bibliografica (titolo, autori, rivista); i contenuti riportati sono quelli degli abstract o delle sintesi pubbliche, salvo dove indicato.

| Famiglia | Evidenza pubblicata | Plausibilità economica | Dove si rompe |
|---|---|---|---|
| **Time-series momentum** | Moskowitz, Ooi, Pedersen (2012, *JFE* 104(2)): TSMOM su 58 futures, persistenza 1–12 mesi con inversione parziale oltre. Liu & Tsyvinski (2021, *RFS* 34(6)): forte TSMOM in cripto a frequenza giornaliera e settimanale (BTC, ETH, XRP), legato all'attenzione degli investitori. Shen, Urquhart, Wang (2022, *Financial Review* 57(2)): momentum intraday su BTC (la prima mezz'ora predice l'ultima), attribuito alla fornitura di liquidità | Under-reaction e diffusione lenta dell'informazione; flussi pro-ciclici di retail e leva | Inversioni violente (2019H2, maggio 2021, 2022). Gli orizzonti efficaci in letteratura sono **giorni-settimane**: un tetto di 48 h ne taglia la parte redditizia |
| **Breakout / trend-following con canali** | Hurst, Ooi, Pedersen (2017, *JPM*): un secolo di trend-following. Zarattini, Pagani, Barbon (2025, SFI RP 25-80): ensemble di Donchian su BTC e altcoin, survivorship-free; i trend **lunghi** restano robusti, quelli **brevi** si sono indeboliti nell'era recente. Hudson & Urquhart (2021, *Ann. Oper. Res.* 297): circa 15.000 regole tecniche con potere predittivo in cripto anche dopo i test di data-snooping | Come sopra, più il convex payoff dei trend | Mercati laterali: whipsaw e fee su molti falsi breakout. Su orizzonti brevi le fee mangiano l'edge |
| **Mean-reversion su iperestensioni** | Caporale & Plastun (2019, *J. Econ. Studies* 46(5)): le overreaction giornaliere **non** danno profitti sfruttabili (contrarian non profittevole; inertia non diversa dal caso), forse per i costi. Letteratura intraday (*Ledger*, "On the Intraday Behavior of Bitcoin"): autocorrelazione negativa a 1–4 h; evidenza di reversion asimmetrica (più forte dopo i ribassi) | Fornitura di liquidità a chi vende in panico; bid-ask bounce | Crash informativi (LUNA, FTX); regimi ribassisti; costi. A frequenze alte l'effetto è soprattutto microstruttura |
| **Carry** | Assente in spot senza leva: niente funding né basis sfruttabili senza derivati. Gli unici "carry" spot sono staking/rewards, che non sono una strategia di trading | — | Fuori perimetro |
| **Pattern di liquidità / stagionalità** | Baur, Cahill, Godfrey, Liu (2019, *FRL* 31): anomalie orarie nei rendimenti presenti ma **non persistenti**; persistente invece l'attività (più bassa la sera e nel weekend, più alta con l'apertura USA) | Flussi istituzionali e retail in orari prevedibili | Effetti nell'ordine dei bp, inferiori ai costi |

Rischi metodologici: Bailey & López de Prado (*deflated Sharpe*) e Harvey, Liu & Zhu (2016, soglia t ≈ 3 per i fattori nuovi) motivano il controllo del multiple testing applicato in §C ed §E.

## C. Misurazione empirica

### C.0 Dati e loro limiti
- **Storico lungo**: candele 5m Binance spot (USDT) dal 2019-01 al 2026-11-21, 20 simboli; 10 sono le large cap oggi su Kraken, 10 sono large cap poi uscite o ridimensionate (LUNA fino al crollo del 2022-05-12, MATIC fino alla migrazione del 2024-09, EOS fino al 2025-05, BCH, TRX, XLM, ETC, ATOM, UNI, FIL). Motivo del proxy: Kraken `/0/public/OHLC` restituisce **solo le ultime 720 candele** [V doc], cioè 2.5 giorni a 5m e 120 giorni a 4h.
- **Validazione del proxy** sulle candele native Kraken (720 per timeframe), tabella sotto. A 1h–1d la correlazione dei rendimenti è 0.999–1.000 sulle coppie USD e 0.969–0.996 su quelle EUR (rumore FX e book più sottili): **il proxy è adeguato per i segnali**.
  Non lo è per l'esecuzione sui book EUR sottili. Il range 5m nativo Kraken vale il 46–54% di quello Binance su LINK/ADA/DOGE EUR (31% AVAX/EUR; DOT/EUR fa 2 trade per 5m). Il range a 1h vale 0.80–0.99.
- **Test di fedeltà dell'esecuzione** (tabella C.0c, XDG/EUR e LINK/EUR, 92 giorni, barre 5m native ricostruite da `/0/public/Trades`):
  - il **fill rate maker nativo è 5–6 punti più basso** del proxy (94.2% contro 99.0%; 93.6% contro 99.2%);
  - sul rendimento lordo dei trade fillati in entrambi l'effetto ha **segno misto e piccolo**: −8.6 bp su XDG, +6.5 bp su LINK (mediane −2.6 / 0.0 bp), contro un round-trip di 66–79 bp.
  
  L'errore del proxy non spiega il fallimento del §E e non lo ribalta.
- **Book Kraken**: 16 snapshot per coppia **in 30 minuti di domenica mattina** (il campionatore si è bloccato per throttling dell'IP condiviso). È un'evidenza debole. La misura vera (≥7 giorni) è un criterio di accettazione del DRY_RUN.
- Binance spread/book: non misurati (host non consentito dalla policy di rete dell'ambiente).

**C.0a: validazione del proxy a 1h/4h/1d (Kraken nativo vs Binance)**

| Kraken | Binance | TF | n | corr rend. | beta | range H-L Kraken/Binance (mediana) | vol Kraken/Binance (quota volume) |
|---|---|---|---|---|---|---|---|
| XBTUSD | BTCUSDT | 1h | 710 | 0.999 | 1.00 | 0.98 | 0.175 |
| XBTUSD | BTCUSDT | 4h | 717 | 1.000 | 1.00 | 0.99 | 0.155 |
| XBTUSD | BTCUSDT | 1d | 719 | 1.000 | 1.00 | 0.99 | 0.094 |
| XBTEUR | BTCUSDT | 1h | 710 | 0.992 | 1.00 | 0.97 | 0.031 |
| XBTEUR | BTCUSDT | 4h | 717 | 0.990 | 0.99 | 0.98 | 0.024 |
| XBTEUR | BTCUSDT | 1d | 719 | 0.985 | 0.97 | 0.97 | 0.022 |
| ETHUSD | ETHUSDT | 1h | 710 | 1.000 | 1.00 | 0.99 | 0.115 |
| ETHUSD | ETHUSDT | 4h | 717 | 1.000 | 1.00 | 0.99 | 0.105 |
| ETHUSD | ETHUSDT | 1d | 719 | 1.000 | 1.00 | 0.99 | 0.059 |
| ETHEUR | ETHUSDT | 1h | 710 | 0.994 | 1.00 | 0.96 | 0.026 |
| ETHEUR | ETHUSDT | 4h | 717 | 0.995 | 0.99 | 0.98 | 0.024 |
| ETHEUR | ETHUSDT | 1d | 719 | 0.994 | 0.99 | 0.96 | 0.015 |
| SOLUSD | SOLUSDT | 1h | 710 | 0.999 | 1.00 | 0.98 | 0.126 |
| SOLUSD | SOLUSDT | 4h | 717 | 1.000 | 1.00 | 1.00 | 0.146 |
| SOLUSD | SOLUSDT | 1d | 719 | 1.000 | 1.00 | 0.99 | 0.083 |
| SOLEUR | SOLUSDT | 1h | 710 | 0.993 | 1.00 | 0.95 | 0.037 |
| SOLEUR | SOLUSDT | 4h | 717 | 0.995 | 0.99 | 0.98 | 0.036 |
| SOLEUR | SOLUSDT | 1d | 719 | 0.995 | 0.99 | 0.97 | 0.017 |
| XRPUSD | XRPUSDT | 1h | 710 | 0.999 | 1.00 | 0.98 | 0.297 |
| XRPUSD | XRPUSDT | 4h | 717 | 1.000 | 1.00 | 0.99 | 0.285 |
| XRPUSD | XRPUSDT | 1d | 719 | 1.000 | 1.00 | 1.00 | 0.143 |
| XRPEUR | XRPUSDT | 1h | 710 | 0.990 | 1.00 | 0.94 | 0.067 |
| XRPEUR | XRPUSDT | 4h | 717 | 0.996 | 0.99 | 0.98 | 0.061 |
| XRPEUR | XRPUSDT | 1d | 719 | 0.995 | 0.99 | 0.97 | 0.027 |
| ADAEUR | ADAUSDT | 1h | 710 | 0.982 | 1.00 | 0.85 | 0.085 |
| ADAEUR | ADAUSDT | 4h | 717 | 0.995 | 1.00 | 0.96 | 0.076 |
| ADAEUR | ADAUSDT | 1d | 719 | 0.996 | 0.98 | 0.98 | 0.031 |
| LINKEUR | LINKUSDT | 1h | 710 | 0.984 | 1.00 | 0.89 | 0.080 |
| LINKEUR | LINKUSDT | 4h | 717 | 0.994 | 0.99 | 0.94 | 0.065 |
| LINKEUR | LINKUSDT | 1d | 719 | 0.995 | 0.99 | 0.95 | 0.017 |
| LTCEUR | LTCUSDT | 1h | 710 | 0.990 | 0.99 | 0.92 | 0.061 |
| LTCEUR | LTCUSDT | 4h | 717 | 0.995 | 0.99 | 0.95 | 0.062 |
| LTCEUR | LTCUSDT | 1d | 719 | 0.993 | 0.98 | 0.96 | 0.032 |
| XDGEUR | DOGEUSDT | 1h | 710 | 0.983 | 1.01 | 0.84 | 0.022 |
| XDGEUR | DOGEUSDT | 4h | 717 | 0.994 | 0.99 | 0.90 | 0.018 |
| XDGEUR | DOGEUSDT | 1d | 719 | 0.996 | 0.99 | 0.97 | 0.008 |
| AVAXEUR | AVAXUSDT | 1h | 710 | 0.973 | 0.98 | 0.80 | 0.055 |
| AVAXEUR | AVAXUSDT | 4h | 717 | 0.993 | 0.99 | 0.91 | 0.036 |
| AVAXEUR | AVAXUSDT | 1d | 719 | 0.995 | 0.99 | 0.96 | 0.011 |
| DOTEUR | DOTUSDT | 1h | 710 | 0.969 | 0.98 | 0.80 | 0.081 |
| DOTEUR | DOTUSDT | 4h | 717 | 0.993 | 1.00 | 0.91 | 0.060 |
| DOTEUR | DOTUSDT | 1d | 719 | 0.993 | 0.98 | 0.96 | 0.027 |

**C.0b: microstructure 5m (ultime 720 candele 5m Kraken)**

| Kraken | n | corr 5m | ρ1 Kraken | ρ1 Binance | sd Kraken/Binance | range H-L Kraken/Binance | trade Kraken per 5m (mediana) |
|---|---|---|---|---|---|---|---|
| XBTUSD | 609 | 0.997 | -0.042 | -0.040 | 1.00 | 0.95 | 548 |
| XBTEUR | 609 | 0.991 | -0.031 | -0.040 | 0.99 | 0.93 | 104 |
| ETHEUR | 609 | 0.978 | -0.048 | -0.047 | 1.00 | 0.84 | 34 |
| SOLEUR | 609 | 0.975 | -0.079 | -0.083 | 0.99 | 0.81 | 31 |
| SOLUSD | 609 | 0.993 | -0.077 | -0.083 | 0.99 | 0.95 | 127 |
| XRPEUR | 609 | 0.934 | -0.062 | -0.038 | 1.04 | 0.71 | 21 |
| XDGEUR | 609 | 0.905 | +0.005 | -0.026 | 1.01 | 0.53 | 7 |
| ADAEUR | 609 | 0.911 | +0.007 | -0.032 | 1.00 | 0.54 | 10 |
| LINKEUR | 609 | 0.896 | -0.054 | -0.067 | 1.01 | 0.46 | 6 |
| AVAXEUR | 609 | 0.845 | -0.035 | -0.059 | 1.03 | 0.31 | 4 |
| LTCEUR | 609 | 0.911 | -0.055 | -0.068 | 1.02 | 0.66 | 12 |
| DOTEUR | 609 | 0.766 | +0.050 | -0.044 | 0.97 | 0.00 | 2 |

**C.0c: fedeltà dell'esecuzione (stessi segnali; proxy Binance vs barre native Kraken da Trades, riscalate al livello USDT)**

| Coppia Kraken | giorni | segnali | fill proxy | fill nativo | fillati in entrambi | lordo medio proxy | lordo medio nativo | Δ lordo (bp) media / mediana | uscite maker proxy / nativo | barre 5m con trade |
|---|---|---|---|---|---|---|---|---|---|---|
| LINKEUR | 92 | 236 | 99.2% | 93.6% | 219 | -2.19% | -2.12% | +6.5 / +0.0 | 37% / 38% | 91% |
| XDGEUR | 92 | 207 | 99.0% | 94.2% | 194 | -1.57% | -1.65% | -8.6 / -2.6 | 39% / 36% | 89% |

Segnali = tutti i segnali distinti delle 5 famiglie (griglie del §E) negli ultimi ~90 giorni. Lordo per trade prima delle fee. Le coppie mancanti (ETH/SOL/XBT EUR ecc.) non sono state scaricate: il download da `/0/public/Trades` richiede ~2 s per chiamata e migliaia di chiamate per coppia liquida.

**C.0d: book Kraken (16 snapshot per coppia, 2026-11-22 09:12–09:43 UTC)**

| Pair | snapshots | spread mediano (bp) | spread p90 (bp) | depth bid ≤10bp | ≤25bp | ≤50bp (quote, mediana) | impatto vendita mkt 1k / 5k / 20k (bp da mid, mediana) |
|---|---|---|---|---|---|---|---|
| XBTEUR | 16 | 0.14 | 3.50 | 815,871 | 5,069,831 | 6,136,392 | 0.1 / 0.1 / 0.3 |
| XBTUSD | 16 | 0.02 | 0.02 | 3,757,815 | 10,941,998 | 13,140,238 | 0.0 / 0.0 / 0.1 |
| ETHEUR | 16 | 0.75 | 2.27 | 479,030 | 3,571,222 | 4,611,547 | 0.4 / 0.4 / 0.5 |
| ETHUSD | 16 | 0.06 | 0.06 | 1,321,612 | 4,968,372 | 8,109,443 | 0.0 / 0.0 / 0.9 |
| SOLEUR | 16 | 1.57 | 1.57 | 106,650 | 273,083 | 1,231,801 | 0.8 / 0.8 / 2.3 |
| SOLUSD | 16 | 1.43 | 2.86 | 475,270 | 2,646,353 | 3,435,934 | 1.1 / 1.2 / 1.8 |
| XRPEUR | 16 | 2.84 | 3.79 | 89,951 | 220,050 | 852,591 | 1.5 / 1.9 / 3.4 |
| XRPUSD | 16 | 0.81 | 1.51 | 360,898 | 1,377,952 | 3,307,449 | 0.5 / 0.5 / 1.5 |
| ADAEUR | 16 | 3.80 | 4.78 | 41,989 | 138,300 | 253,566 | 2.9 / 3.8 / 6.0 |
| LINKEUR | 16 | 0.77 | 1.80 | 66,154 | 159,195 | 303,956 | 0.4 / 1.4 / 3.1 |
| LTCEUR | 16 | 5.55 | 5.57 | 40,845 | 120,913 | 245,603 | 2.8 / 3.5 / 5.7 |
| XDGEUR | 16 | 0.29 | 5.14 | 19,575 | 115,281 | 170,843 | 0.8 / 2.9 / 6.1 |
| AVAXEUR | 16 | 3.32 | 4.96 | 33,123 | 111,789 | 205,645 | 1.7 / 1.7 / 4.7 |
| DOTEUR | 16 | 5.85 | 8.39 | 4,595 | 19,974 | 53,718 | 3.4 / 6.8 / 14.7 |
| XBTUSDC | 16 | 0.99 | 2.61 | 242,091 | 642,701 | 2,279,539 | 0.5 / 0.5 / 0.9 |
| ETHUSDC | 16 | 2.16 | 4.81 | 247,543 | 523,667 | 2,090,993 | 1.1 / 1.1 / 2.7 |

**C.0e: regimi coperti dal campione (BTC, per semestre)**: rialzo 2019H1 +186%, 2020H2 +213%, 2023H1 +83%, 2024H1–H2 +42%/+49%; ribasso 2019H2 −32%, 2022H1 −58% (DD 60%), 2022H2 −14%, 2026H1 −34%; laterale/volatile 2021H1 +20% con DD 50%, 2025H1 +13% con DD 28%, 2026H2 +0.3% a oggi. Il campione OOS (2021–2026) contiene rialzo, ribasso e fasi laterali.

### C.1 Autocorrelazione lag-1 (t robusto) e variance ratio VR(8) (z* robusto), campione intero
| Coppia | ρ1 5m | ρ1 15m | ρ1 1h | ρ1 4h | ρ1 1d | VR8 5m | VR8 1h | VR8 4h | VR8 1d |
|---|---|---|---|---|---|---|---|---|---|
| BTC | -0.030 (-4.0) | -0.009 (-0.8) | -0.017 (-1.5) | -0.021 (-1.6) | -0.068 (-2.0) | 0.91 (-4.3) | 0.94 (-1.9) | 1.00 (-0.1) | 0.98 (-0.3) |
| ETH | -0.025 (-2.9) | +0.002 (+0.3) | -0.005 (-0.6) | -0.021 (-1.5) | -0.082 (-2.4) | 0.94 (-2.9) | 0.95 (-1.8) | 1.00 (-0.1) | 0.96 (-0.5) |
| SOL | -0.020 (-3.6) | -0.002 (-0.2) | -0.024 (-2.3) | -0.042 (-2.6) | -0.049 (-1.2) | 0.93 (-4.5) | 0.91 (-3.2) | 0.94 (-1.4) | 1.01 (+0.1) |
| XRP | -0.025 (-2.7) | -0.016 (-1.3) | -0.029 (-2.5) | +0.009 (+0.4) | -0.067 (-1.8) | 0.92 (-3.5) | 0.92 (-2.6) | 1.04 (+0.7) | 0.94 (-0.6) |
| ADA | -0.028 (-1.5) | -0.018 (-1.4) | -0.029 (-2.5) | -0.009 (-0.6) | -0.069 (-2.0) | 0.89 (-2.7) | 0.92 (-3.0) | 0.96 (-1.0) | 0.95 (-0.6) |
| LINK | -0.029 (-4.1) | -0.025 (-2.2) | -0.023 (-2.3) | -0.025 (-2.0) | -0.075 (-2.5) | 0.90 (-5.0) | 0.90 (-3.8) | 0.94 (-1.6) | 0.92 (-0.9) |
| LTC | -0.025 (-2.3) | -0.021 (-1.9) | -0.038 (-3.0) | -0.018 (-1.5) | -0.058 (-1.9) | 0.91 (-3.7) | 0.90 (-3.6) | 0.97 (-0.8) | 0.88 (-1.5) |
| DOGE | -0.048 (-3.1) | -0.009 (-0.7) | -0.029 (-1.1) | +0.005 (+0.1) | -0.008 (-0.1) | 0.87 (-3.6) | 0.92 (-1.2) | 1.17 (+1.2) | 1.06 (+0.3) |
| AVAX | -0.098 (-1.5) | -0.008 (-0.9) | -0.033 (-2.5) | -0.037 (-2.4) | +0.016 (+0.5) | 0.83 (-1.4) | 0.89 (-3.6) | 0.97 (-0.8) | 1.17 (+1.8) |
| DOT | -0.029 (-1.3) | -0.019 (-0.8) | -0.028 (-2.3) | -0.027 (-1.9) | -0.037 (-1.1) | 0.87 (-2.3) | 0.89 (-3.6) | 0.93 (-1.7) | 0.94 (-0.7) |

### C.2 ρ1 per sottoperiodo (t robusto)
| Coppia | TF | 2019-2021 | 2022-2023 | 2024-2026 |
|---|---|---|---|---|
| BTC | 5m | -0.037 (-3.1) | -0.028 (-3.5) | -0.010 (-1.4) |
| BTC | 1h | -0.022 (-1.2) | -0.011 (-0.6) | -0.011 (-0.9) |
| BTC | 4h | -0.037 (-1.8) | +0.016 (+0.7) | -0.012 (-0.7) |
| ETH | 5m | -0.037 (-2.6) | -0.014 (-1.9) | -0.002 (-0.4) |
| ETH | 1h | -0.014 (-0.9) | +0.001 (+0.1) | +0.007 (+0.6) |
| ETH | 4h | -0.042 (-1.8) | -0.011 (-0.5) | +0.013 (+0.7) |
| SOL | 5m | -0.010 (-1.2) | -0.040 (-3.6) | -0.015 (-1.6) |
| SOL | 1h | -0.031 (-1.8) | -0.013 (-0.7) | -0.022 (-1.8) |
| SOL | 4h | -0.070 (-2.7) | -0.012 (-0.4) | -0.017 (-0.9) |
| XRP | 5m | -0.032 (-2.5) | -0.032 (-1.5) | -0.003 (-0.2) |
| XRP | 1h | -0.043 (-2.6) | -0.013 (-0.5) | -0.011 (-0.6) |
| XRP | 4h | +0.005 (+0.2) | +0.048 (+0.8) | -0.013 (-0.5) |
| ADA | 5m | -0.040 (-2.7) | -0.018 (-2.6) | -0.013 (-0.2) |
| ADA | 1h | -0.054 (-3.5) | -0.021 (-1.1) | +0.009 (+0.4) |
| ADA | 4h | -0.028 (-1.5) | +0.002 (+0.1) | +0.013 (+0.4) |
| LINK | 5m | -0.041 (-4.3) | -0.021 (-3.2) | -0.004 (-0.2) |
| LINK | 1h | -0.032 (-2.1) | -0.014 (-0.8) | -0.005 (-0.4) |
| LINK | 4h | -0.034 (-1.8) | -0.009 (-0.5) | -0.014 (-0.7) |
| LTC | 5m | -0.027 (-2.5) | -0.030 (-2.1) | -0.017 (-0.5) |
| LTC | 1h | -0.053 (-2.5) | -0.023 (-1.3) | -0.018 (-1.3) |
| LTC | 4h | -0.013 (-0.7) | -0.023 (-1.1) | -0.024 (-1.1) |
| DOGE | 5m | -0.065 (-3.7) | -0.019 (-2.0) | -0.018 (-0.4) |
| DOGE | 1h | -0.037 (-0.9) | -0.027 (-1.3) | -0.010 (-0.7) |
| DOGE | 4h | +0.017 (+0.2) | -0.014 (-0.5) | -0.014 (-0.6) |
| AVAX | 5m | -0.021 (-1.8) | -0.023 (-2.2) | -0.241 (-1.3) |
| AVAX | 1h | -0.052 (-2.6) | -0.030 (-0.9) | +0.001 (+0.1) |
| AVAX | 4h | -0.077 (-2.8) | +0.027 (+1.2) | -0.026 (-1.4) |
| DOT | 5m | -0.035 (-1.3) | -0.038 (-5.2) | -0.015 (-0.3) |
| DOT | 1h | -0.041 (-2.0) | -0.029 (-0.9) | -0.009 (-0.7) |
| DOT | 4h | -0.053 (-2.1) | -0.005 (-0.2) | -0.007 (-0.3) |

### C.3 Esponente di Hurst (DFA) su rendimenti e |rendimenti|
| Coppia | H 5m | H 1h | H 4h | H 1d | H|r| 5m | H|r| 1h | H|r| 4h |
|---|---|---|---|---|---|---|---|
| BTC | 0.502 | 0.507 | 0.525 | 0.524 | 0.90 | 0.88 | 0.83 |
| ETH | 0.503 | 0.511 | 0.520 | 0.516 | 0.90 | 0.87 | 0.81 |
| SOL | 0.508 | 0.526 | 0.544 | 0.609 | 0.90 | 0.86 | 0.80 |
| XRP | 0.492 | 0.502 | 0.516 | 0.487 | 0.91 | 0.88 | 0.81 |
| ADA | 0.491 | 0.501 | 0.520 | 0.561 | 0.89 | 0.87 | 0.80 |
| LINK | 0.485 | 0.491 | 0.502 | 0.494 | 0.91 | 0.87 | 0.82 |
| LTC | 0.483 | 0.488 | 0.485 | 0.488 | 0.89 | 0.86 | 0.81 |
| DOGE | 0.515 | 0.525 | 0.520 | 0.595 | 0.95 | 0.90 | 0.84 |
| AVAX | 0.511 | 0.535 | 0.573 | 0.626 | 0.88 | 0.88 | 0.85 |
| DOT | 0.490 | 0.508 | 0.521 | 0.533 | 0.89 | 0.87 | 0.82 |

### C.4 Time-series momentum (test non sovrapposti su chiusure 1h): t-stat della strategia sign(rend. passato L) × rend. futuro H
| Coppia | L12_H12 | L12_H24 | L12_H48 | L24_H12 | L24_H24 | L24_H48 | L72_H12 | L72_H24 | L72_H48 | L168_H12 | L168_H24 | L168_H48 | L336_H12 | L336_H24 | L336_H48 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTC | +2.0 | -0.5 | +0.6 | -1.1 | -0.3 | +0.5 | +1.7 | +1.6 | +1.6 | +1.7 | +1.6 | +0.9 | +1.4 | +1.7 | +1.5 |
| ETH | +1.7 | -0.1 | -0.8 | -1.2 | -0.2 | +1.4 | +1.4 | +0.9 | +0.7 | +2.0 | +1.8 | +1.7 | +1.5 | +1.3 | +1.2 |
| SOL | +1.8 | -0.9 | -1.2 | -1.7 | -0.5 | -0.2 | +1.2 | +1.0 | +1.8 | +1.9 | +2.0 | +2.1 | +1.1 | +1.2 | +0.4 |
| XRP | +2.6 | -0.1 | +1.1 | +1.0 | -0.1 | +1.8 | +0.6 | +0.0 | +1.9 | +1.3 | +0.8 | +0.8 | +2.3 | +1.8 | +1.6 |
| ADA | +2.8 | -0.4 | +0.4 | -0.2 | +0.2 | -0.6 | +1.2 | +0.5 | -0.3 | +1.6 | +1.6 | +0.3 | +1.5 | +1.4 | +1.4 |
| LINK | +2.0 | -0.5 | +0.1 | -1.6 | -1.8 | +0.5 | +1.8 | +0.9 | -0.2 | +2.0 | +2.7 | +1.3 | +1.3 | +0.8 | +0.8 |
| LTC | +1.2 | -1.9 | -1.6 | -1.4 | -0.1 | -0.1 | -0.2 | +0.2 | -0.5 | +0.8 | +0.1 | -0.2 | +0.4 | +0.5 | +0.4 |
| DOGE | +2.5 | +0.6 | -0.1 | +1.7 | +0.6 | +0.1 | +1.9 | +1.9 | +1.6 | +2.5 | +2.4 | +0.7 | +2.6 | +2.2 | +2.4 |
| AVAX | +1.2 | -0.1 | -0.9 | -0.3 | +1.1 | +1.9 | +1.5 | +1.4 | +1.2 | +1.9 | +2.3 | +2.1 | +1.6 | +1.8 | +1.3 |
| DOT | +0.9 | +0.4 | -0.3 | -0.1 | -0.0 | -0.1 | +0.7 | +0.3 | +0.3 | +0.5 | +0.5 | -0.6 | +1.1 | +1.3 | +0.4 |

### C.5 TSMOM long-only: rendimento medio futuro (bp) quando il passato è positivo vs negativo, L=168h (7g), per H
| Coppia | H=12h up/down | H=24h up/down | H=48h up/down | t L168H48 per periodo 19-21 / 22-23 / 24-26 |
|---|---|---|---|---|
| BTC | +8 / -2 | +16 / -2 | +24 / +4 | +0.3 / +0.3 / +1.2 |
| ETH | +10 / -6 | +18 / -10 | +35 / -15 | +1.2 / +0.7 / +1.0 |
| SOL | +16 / -7 | +32 / -15 | +69 / -30 | +0.8 / +1.7 / +1.1 |
| XRP | +9 / -4 | +13 / -3 | +27 / -2 | +1.1 / -0.5 / +0.4 |
| ADA | +9 / -7 | +19 / -13 | +11 / -2 | +1.0 / -0.2 / -0.3 |
| LINK | +15 / -7 | +38 / -20 | +40 / -15 | +1.6 / +0.8 / -0.3 |
| LTC | +3 / -4 | -1 / -2 | -3 / +1 | +0.1 / -0.3 / -0.3 |
| DOGE | +24 / -9 | +47 / -17 | +49 / +3 | -0.1 / +0.3 / +1.4 |
| AVAX | +12 / -13 | +28 / -29 | +48 / -53 | +2.1 / +1.4 / -0.1 |
| DOT | -6 / -10 | -10 / -21 | -47 / -19 | -1.2 / +1.0 / -0.5 |

### C.6 Persistenza dei regimi
| Coppia | 4h close>SMA50: P(stay), durata media (barre) | 4h close>SMA200: P(stay), durata | 1d >SMA50: P(stay), durata (giorni) | Vol 24h > mediana 90g: P(stay giornaliera), durata (gg) |
|---|---|---|---|---|
| BTC | 0.932, 14.8 | 0.972, 36.0 | 0.949, 19.4 | 0.662, 3.0 |
| ETH | 0.931, 14.5 | 0.977, 43.3 | 0.950, 19.7 | 0.666, 3.0 |
| SOL | 0.932, 14.6 | 0.968, 31.0 | 0.944, 17.7 | 0.681, 3.1 |
| XRP | 0.931, 14.5 | 0.970, 33.3 | 0.929, 14.0 | 0.695, 3.3 |
| ADA | 0.930, 14.2 | 0.969, 32.3 | 0.943, 17.5 | 0.683, 3.2 |
| LINK | 0.932, 14.7 | 0.964, 27.5 | 0.938, 16.1 | 0.690, 3.2 |
| LTC | 0.922, 12.8 | 0.967, 30.1 | 0.923, 13.0 | 0.684, 3.2 |
| DOGE | 0.926, 13.5 | 0.974, 38.1 | 0.949, 19.6 | 0.683, 3.2 |
| AVAX | 0.930, 14.3 | 0.968, 31.2 | 0.932, 14.6 | 0.682, 3.1 |
| DOT | 0.935, 15.5 | 0.972, 36.2 | 0.943, 17.3 | 0.697, 3.3 |

### C.7 Movimenti estremi a 4h (z-score su vol 30g): rendimento successivo medio in bp (t), H=12h e H=24h
| Coppia | z≤-3 H12 | z≤-2 H12 | z≥+2 H12 | z≥+3 H12 | z≤-3 H24 | z≤-2 H24 | z≥+2 H24 | uncond H24 |
|---|---|---|---|---|---|---|---|---|
| BTC | -18 (-0.6, n=179) | -10 (-0.6, n=491) | +50 (+4.1, n=481) | +63 (+2.7, n=164) | +47 (+1.2, n=178) | +17 (+0.8, n=490) | +39 (+2.3, n=480) | +10.0 |
| ETH | +32 (+0.8, n=195) | -21 (-1.0, n=500) | +56 (+3.6, n=481) | +61 (+1.9, n=148) | +91 (+2.0, n=195) | +24 (+0.9, n=499) | +75 (+3.4, n=480) | +9.6 |
| SOL | -34 (-0.5, n=111) | +59 (+1.8, n=359) | +23 (+0.8, n=373) | +67 (+1.3, n=116) | -11 (-0.1, n=110) | +91 (+1.9, n=358) | +47 (+1.2, n=372) | +13.3 |
| XRP | +60 (+1.1, n=154) | +15 (+0.5, n=442) | +55 (+2.0, n=492) | +14 (+0.2, n=182) | +141 (+2.4, n=154) | +85 (+2.6, n=441) | +83 (+2.3, n=492) | +3.8 |
| ADA | +68 (+1.2, n=145) | +48 (+1.9, n=440) | +39 (+1.7, n=469) | +68 (+1.5, n=148) | +170 (+2.3, n=144) | +109 (+3.0, n=439) | +26 (+0.8, n=469) | +4.6 |
| LINK | +61 (+1.0, n=128) | +24 (+0.8, n=422) | +6 (+0.3, n=452) | -64 (-1.5, n=131) | +188 (+2.8, n=127) | +88 (+2.2, n=421) | -7 (-0.2, n=451) | +10.5 |
| LTC | +21 (+0.5, n=155) | +2 (+0.1, n=443) | +29 (+1.3, n=409) | +0 (+0.0, n=132) | +41 (+0.7, n=154) | +46 (+1.4, n=442) | -1 (-0.0, n=409) | +0.8 |
| DOGE | +131 (+2.0, n=136) | +52 (+1.8, n=408) | +144 (+2.7, n=405) | +222 (+1.9, n=161) | +242 (+3.3, n=135) | +119 (+3.3, n=407) | +231 (+2.9, n=405) | +10.9 |
| AVAX | +58 (+0.9, n=105) | +54 (+1.8, n=350) | +30 (+1.2, n=355) | +10 (+0.2, n=133) | +159 (+1.7, n=104) | +84 (+1.8, n=349) | +61 (+1.6, n=354) | +1.8 |
| DOT | +56 (+1.0, n=112) | +32 (+1.1, n=336) | +40 (+1.6, n=351) | +112 (+2.5, n=112) | +140 (+2.1, n=112) | +83 (+2.3, n=336) | +8 (+0.2, n=351) | -9.2 |

### C.8 Stagionalità oraria (UTC): ore con |t|>3 sul rendimento medio 1h
- BTC: 21h +4.2bp (t=+3.4)
- ETH: 21h +4.8bp (t=+3.1)
- SOL: nessuna
- XRP: 06h -5.1bp (t=-3.0)
- ADA: nessuna
- LINK: nessuna
- LTC: 22h +4.9bp (t=+3.0)
- DOGE: 21h +7.4bp (t=+3.1)
- AVAX: 05h +8.2bp (t=+3.5)
- DOT: nessuna

### C.9 Ampiezza dei movimenti vs costi: |Δlog prezzo| medio (%) e ATR14 mediano (%) — ultimi 2 anni
| Coppia | E|r| 1h | 4h | 12h | 24h | 48h | ATR% 15m | 1h | 4h | 1d |
|---|---|---|---|---|---|---|---|---|---|
| BTC | 0.31 | 0.63 | 1.16 | 1.71 | 2.47 | 0.27 | 0.58 | 1.20 | 3.25 |
| ETH | 0.45 | 0.93 | 1.74 | 2.59 | 3.85 | 0.42 | 0.90 | 1.90 | 5.11 |
| SOL | 0.55 | 1.12 | 1.98 | 2.90 | 4.17 | 0.50 | 1.06 | 2.22 | 5.83 |
| XRP | 0.55 | 1.11 | 2.01 | 2.99 | 4.34 | 0.46 | 0.97 | 2.02 | 5.51 |
| ADA | 0.65 | 1.30 | 2.38 | 3.44 | 5.02 | 0.58 | 1.22 | 2.51 | 6.84 |
| LINK | 0.62 | 1.24 | 2.22 | 3.12 | 4.24 | 0.58 | 1.19 | 2.43 | 6.41 |
| LTC | 0.54 | 1.07 | 1.92 | 2.71 | 3.91 | 0.47 | 0.98 | 2.00 | 5.39 |
| DOGE | 0.61 | 1.24 | 2.18 | 3.13 | 4.61 | 0.54 | 1.15 | 2.42 | 6.84 |
| AVAX | 0.62 | 1.26 | 2.28 | 3.31 | 4.73 | 0.57 | 1.19 | 2.51 | 6.79 |
| DOT | 0.64 | 1.27 | 2.29 | 3.26 | 5.07 | 0.57 | 1.20 | 2.50 | 6.78 |

### C.10 Cosa dicono i numeri
1. **Quasi random walk a 1h–1d.**
   - Hurst DFA dei rendimenti 0.49–0.54 a 1h–4h (AVAX 0.57 a 4h); a 1d fino a 0.63 (SOL 0.61, DOGE 0.60, AVAX 0.63).
   - ρ1 a 1h tra −0.005 e −0.038; a 4h tra −0.042 e +0.009, con |t| ≤ 2.6 (SOL −2.6, AVAX −2.4).
   - VR(8) a 4h 0.93–1.17, nessuna z oltre ±1.7.
2. **Mean-reversion solo microstrutturale.** ρ1 a 5m tra −0.020 e −0.098 (t fino a −4.1) e VR(8) a 5m 0.83–0.94, ma è bid-ask bounce: si attenua nel 2024–26 (BTC −0.011, t −1.5) e vale pochi bp contro un round-trip di 54–130 bp. Non è sfruttabile.
3. **Momentum debole a 3–14 giorni.** Per L=72–336 h la t-stat della regola sign × rendimento futuro è quasi sempre positiva ma ≤2.7, e nessuna cella supera 3 (§C.4). Long-only: con passato positivo il rendimento a 48 h vale +24…+69 bp, con passato negativo −53…+4 bp (§C.5). L'ordine di grandezza (decine di bp) è inferiore al round-trip.
4. **Continuazione dopo forti rialzi a 4h** (z ≥ +2): BTC +50 bp a 12 h (t 4.1), ETH +56 bp (t 3.6). Dopo forti ribassi (z ≤ −3), a 24 h la maggior parte delle alt rimbalza (+91…+242 bp, t 1.7–3.3, n = 104–195), ma non SOL (−11 bp) né LTC (+41 bp, t 0.7). Sono effetti nell'ordine del round-trip, non di 3×.
5. **Regimi persistenti**: sopra/sotto la SMA200 a 4h P(stay) è 0.964–0.977 (durata media 27–43 barre, cioè 4.5–7 giorni). Il volatility clustering è forte (H(|r|) 0.80–0.95): **utile per sizing e filtri, non come fonte di rendimento**.
6. **Stagionalità oraria**: l'unica stabile è alle 21 UTC (BTC +4.2 bp, ETH +4.8 bp), due ordini di grandezza sotto i costi.

## D. Budget di edge
Round-trip (RT) = fee maker d'ingresso + fee d'uscita (maker se limit, taker se stop o fallback) + 2 × 5 bp di slippage (assunzione dell'utente), più l'impatto sul book per le uscite taker.

| Tier | RT maker→maker | RT maker→taker | 3× RT (min…max) |
|---|---|---|---|
| 1 (0.40/0.80) | 0.90% | 1.30% | 2.70…3.90% |
| 2 (0.30/0.60) | 0.70% | 1.00% | 2.10…3.00% |
| 3 (0.22/0.38) | 0.54% | 0.70% | 1.62…2.10% |
| 4 (0.20/0.35) | 0.50% | 0.65% | 1.50…1.95% |
| 5 (0.15/0.30) | 0.40% | 0.55% | 1.20…1.65% |

Movimento medio |Δ log prezzo| per orizzonte (ultimi 2 anni) e confronto con 3×RT maker→taker (✓ = supera a quel tier):

| Coppia | 1h | 4h | 12h | 24h | 48h | 48h ≥3×RT a T1 / T2 / T3 |
|---|---|---|---|---|---|---|
| BTC | 0.31% | 0.63% | 1.16% | 1.71% | 2.47% | ✗ / ✗ / ✓ |
| ETH | 0.45% | 0.93% | 1.74% | 2.59% | 3.85% | ✗ / ✓ / ✓ |
| SOL | 0.55% | 1.12% | 1.98% | 2.90% | 4.17% | ✓ / ✓ / ✓ |
| XRP | 0.55% | 1.11% | 2.01% | 2.99% | 4.34% | ✓ / ✓ / ✓ |
| ADA | 0.65% | 1.30% | 2.38% | 3.44% | 5.02% | ✓ / ✓ / ✓ |
| LINK | 0.62% | 1.24% | 2.22% | 3.12% | 4.24% | ✓ / ✓ / ✓ |
| LTC | 0.54% | 1.07% | 1.92% | 2.71% | 3.91% | ✓ / ✓ / ✓ |
| DOGE | 0.61% | 1.24% | 2.18% | 3.13% | 4.61% | ✓ / ✓ / ✓ |
| AVAX | 0.62% | 1.26% | 2.28% | 3.31% | 4.73% | ✓ / ✓ / ✓ |
| DOT | 0.64% | 1.27% | 2.29% | 3.26% | 5.07% | ✓ / ✓ / ✓ |

Conclusioni del budget:
1. **5m–1h esclusi**: il movimento medio a quegli orizzonti (≤0.65%) non arriva a 3× il RT nemmeno al tier 5 (1.20%).
2. Sul timeframe 4h, il **movimento** su 24–48 h (1.71–5.07%) supera 3× il RT maker→taker a 48 h già al tier 1 per le alt (non ETH, che passa dal tier 2); BTC passa solo al tier 3.
3. Ma il criterio che conta è l'**expectancy lorda** per trade ≥ 3× RT (definizione B dell'audit). Nel walk-forward vale **0.03–1.26×** (§E.1). Nessuna famiglia passa.
4. **Tier del conto**: dipende dal volume 30 g o dagli asset su Kraken. Una strategia con circa 140 trade/anno al 10–15% del capitale genera ~3× il capitale di volume mensile: tier 2–3 sopra circa 3k USD di capitale, tier 3 garantito con ≥20k USD di asset. Il backtest simula il tier dinamico (capitale di riferimento 10k USD) e riporta anche tier 1/2/3 fissi (§E.2).

## E. Selezione dei candidati

### E.0 Screening in-sample (8 coppie, 2019–2026; serve solo a scartare)
| Famiglia | TF | config | trade mediani | fill | lordo mediano | |lordo| mediano | netto T1 | netto T2 | netto T3 | % config >0 a T3 |
|---|---|---|---|---|---|---|---|---|---|---|
| donchian | 1h | 128 | 791 | 97% | +0.04% | 2.22% | -1.24% | -0.95% | -0.66% | 0% |
| donchian | 4h | 128 | 236 | 95% | +0.47% | 4.30% | -0.67% | -0.41% | -0.16% | 39% |
| squeeze | 1h | 128 | 506 | 96% | +0.10% | 1.99% | -1.19% | -0.89% | -0.60% | 1% |
| squeeze | 4h | 128 | 142 | 96% | +0.38% | 3.81% | -0.80% | -0.54% | -0.27% | 27% |
| tsmom | 1h | 288 | 265 | 97% | +0.20% | 3.54% | -0.98% | -0.71% | -0.44% | 20% |
| tsmom | 4h | 288 | 170 | 97% | +0.44% | 4.85% | -0.59% | -0.36% | -0.15% | 41% |
| impulse | 1h | 128 | 494 | 97% | +0.13% | 3.18% | -1.16% | -0.87% | -0.57% | 2% |
| impulse | 4h | 128 | 206 | 96% | +0.64% | 5.01% | -0.55% | -0.29% | -0.02% | 47% |
| mr_dip | 1h | 384 | 242 | 98% | +0.14% | 2.78% | -0.85% | -0.62% | -0.43% | 22% |
| mr_dip | 4h | 384 | 130 | 99% | +0.53% | 4.42% | -0.44% | -0.22% | -0.04% | 47% |

Lettura: **a 1h nessuna famiglia ha expectancy netta mediana positiva a nessun tier** (lordo mediano +0.04…+0.20% contro RT 0.56–0.70%), quindi 1h è scartato. A 4h le mediane sono ≤0 al tier 3. Lo screening ha prodotto ~1.920 configurazioni distinte: **nessun numero dello screening è usato per scegliere**. La scelta è delegata al walk-forward pre-registrato.

### E.1–E.8 Walk-forward pre-registrato (OOS)
Protocollo (fissato in `research/scripts/wf2.py` prima di vedere i risultati):
- timeframe 4h;
- universo point-in-time: top-8 per volume a 90 giorni fra 20 simboli, inclusi i delistati;
- griglie piccole e fissate a priori;
- selezione per finestra sul **pool** di coppie (mai per coppia singola), con obiettivo la t-stat netta per trade a tier 2, smussata sui vicini di griglia;
- 12 semestri 2021H1–2026H2, train rolling 24 mesi, **purge ed embargo 72 h**;
- portafoglio con le regole di rischio del bot (`bt.portfolio2`): sizing dallo stop, 3 posizioni, 30% di esposizione, breaker, cooldown, kill switch con flatten e HALT, tier dinamico, minimi per coppia, impatto sul book.

### E.1 Walk-forward OOS (pre-registrato), regole di rischio complete, tier fee dinamico, capitale 10k USD
| Famiglia | trade OOS | win | exp. netta/trade | PF netto (USD) | Sharpe | Sortino | max DD | CAGR | % finestre >0 | exp. lorda | RT medio | lordo/RT (B) | |lordo|/RT (A) | hold medio | uscite maker | kill switch |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| donchian | 310 | 41.6% | +0.16% | 0.95 | -0.09 | -0.13 | 15.0% | -0.5% | 20.0% | +0.83% | 0.66% | 1.26× | 9.4× | 40h | 63.2% | total_dd @ 2023-03-17 |
| squeeze | 224 | 37.9% | -0.69% | 0.81 | -0.36 | -0.48 | 15.0% | -1.5% | 25.0% | +0.02% | 0.70% | 0.03× | 7.2× | 33h | 40.2% | total_dd @ 2022-10-07 |
| tsmom | 413 | 40.9% | -0.21% | 0.85 | -0.38 | -0.53 | 15.1% | -1.8% | 27.3% | +0.49% | 0.71% | 0.70× | 8.0× | 44h | 81.8% | total_dd @ 2026-04-18 |
| impulse | 292 | 33.2% | -0.25% | 0.85 | -0.30 | -0.42 | 15.0% | -1.1% | 20.0% | +0.53% | 0.79% | 0.67× | 7.3× | 28h | 24.0% | total_dd @ 2023-06-09 |
| mr_dip | 401 | 58.9% | +0.01% | 0.78 | -0.48 | -0.63 | 11.7% | -1.5% | 41.7% | +0.67% | 0.67% | 1.01× | 5.3× | 22h | 84.0% | — |

### E.2 Stress sui costi (OOS): expectancy netta per trade, PF, Sharpe
| Famiglia | base (tier dinamico) | Tier 1 fisso | Tier 2 | Tier 3 | fee ×2 | slippage ×2 | fee ×2 + slip ×2 | fee×2+slip×2 a Tier 1 |
|---|---|---|---|---|---|---|---|---|
| donchian | +0.16% / PF 0.95 / S -0.09 | +0.10% / PF 0.89 / S -0.20 | +0.19% / PF 0.93 / S -0.13 | +0.15% / PF 0.95 / S -0.09 | +0.10% / PF 0.88 / S -0.21 | +0.13% / PF 0.94 / S -0.11 | +0.05% / PF 0.87 / S -0.23 | -0.42% / PF 0.72 / S -0.51 |
| squeeze | -0.69% / PF 0.81 / S -0.36 | -0.90% / PF 0.77 / S -0.43 | -0.81% / PF 0.79 / S -0.40 | -0.63% / PF 0.82 / S -0.34 | -1.19% / PF 0.73 / S -0.49 | -0.79% / PF 0.80 / S -0.38 | -1.17% / PF 0.74 / S -0.45 | -1.64% / PF 0.67 / S -0.51 |
| tsmom | -0.21% / PF 0.85 / S -0.38 | -0.31% / PF 0.82 / S -0.41 | -0.24% / PF 0.85 / S -0.37 | -0.14% / PF 0.87 / S -0.32 | -0.95% / PF 0.67 / S -0.63 | -0.57% / PF 0.71 / S -0.55 | -1.08% / PF 0.66 / S -0.65 | -1.38% / PF 0.63 / S -0.68 |
| impulse | -0.25% / PF 0.85 / S -0.30 | -0.49% / PF 0.79 / S -0.40 | -0.39% / PF 0.81 / S -0.38 | -0.09% / PF 0.87 / S -0.26 | -0.81% / PF 0.76 / S -0.46 | -0.37% / PF 0.82 / S -0.36 | -0.77% / PF 0.75 / S -0.47 | -0.99% / PF 0.70 / S -0.58 |
| mr_dip | +0.01% / PF 0.78 / S -0.48 | -0.14% / PF 0.62 / S -0.85 | -0.08% / PF 0.71 / S -0.68 | +0.11% / PF 0.82 / S -0.38 | -0.16% / PF 0.58 / S -0.90 | -0.10% / PF 0.71 / S -0.67 | -0.28% / PF 0.55 / S -0.99 | -0.68% / PF 0.38 / S -1.25 |

### E.3 Sensitivity ±20% sui parametri selezionati (OOS): exp. netta/trade, PF
- **donchian**: Nx0.8 → -0.02% PF 0.89; Nx1.2 → +0.20% PF 0.94; k_slx0.8 → +0.37% PF 0.94; k_slx1.2 → -0.04% PF 0.88; k_trx0.8 → -0.14% PF 0.84; k_trx1.2 → +0.07% PF 0.94; regimex0.8 → +0.03% PF 0.91; regimex1.2 → +0.03% PF 0.90
- **squeeze**: pctx0.8 → -0.86% PF 0.75; pctx1.2 → -0.63% PF 0.82; k_slx0.8 → -0.65% PF 0.76; k_slx1.2 → -0.54% PF 0.83; k_trx0.8 → -0.57% PF 0.79; k_trx1.2 → -0.32% PF 0.90; regimex0.8 → -0.73% PF 0.80; regimex1.2 → -0.75% PF 0.79
- **tsmom**: Lx0.8 → -0.76% PF 0.77; Lx1.2 → -0.46% PF 0.82; thrx0.8 → -0.42% PF 0.90; thrx1.2 → -0.10% PF 0.91; k_slx0.8 → -0.47% PF 0.77; k_slx1.2 → -0.34% PF 0.82
- **impulse**: nx0.8 → -0.25% PF 0.85; nx1.2 → -0.25% PF 0.85; z0x0.8 → +0.19% PF 0.97; z0x1.2 → -0.52% PF 0.78; k_slx0.8 → -0.17% PF 0.89; k_slx1.2 → -0.27% PF 0.80; k_trx0.8 → -0.17% PF 0.90; k_trx1.2 → +0.03% PF 0.90
- **mr_dip**: nx0.8 → -0.02% PF 0.78; nx1.2 → -0.03% PF 0.78; z0x0.8 → +0.00% PF 0.73; z0x1.2 → +0.02% PF 0.81; k_slx0.8 → -0.06% PF 0.77; k_slx1.2 → +0.08% PF 0.72; exit_max0.8 → +0.00% PF 0.77; exit_max1.2 → +0.04% PF 0.81; regimex0.8 → +0.10% PF 0.82; regimex1.2 → +0.05% PF 0.80

### E.4 Concentrazione: leave-one-pair-out (range) e rimozione del mese migliore
| Famiglia | LOPO exp. min…max | LOPO PF min…max | mese migliore | quota PnL | senza mese migliore: exp / PF / Sharpe |
|---|---|---|---|---|---|
| donchian | -0.27…+0.22% | 0.82…0.96 | 2021-04 | n/a (PnL totale ≤ 0) | -0.41% / 0.84 / -0.33 |
| squeeze | -0.92…-0.43% | 0.76…0.85 | 2021-04 | n/a (PnL totale ≤ 0) | -0.94% / 0.71 / -0.51 |
| tsmom | -0.46…-0.10% | 0.81…0.90 | 2021-01 | n/a (PnL totale ≤ 0) | -0.54% / 0.79 / -0.54 |
| impulse | -0.65…-0.03% | 0.76…0.89 | 2021-04 | n/a (PnL totale ≤ 0) | -0.95% / 0.71 / -0.61 |
| mr_dip | -0.11…+0.24% | 0.70…0.87 | 2023-11 | n/a (PnL totale ≤ 0) | -0.06% / 0.73 / -0.61 |

### E.5 Sottoperiodi OOS (exp. netta/trade, PF, n)
| Famiglia | 2021 | 2022–2023 | 2024–2026 |
|---|---|---|---|
| donchian | +0.95% / PF 1.11 / n=185 | -1.01% / PF 0.67 / n=125 | — (HALT) |
| squeeze | -0.22% / PF 1.05 / n=115 | -1.18% / PF 0.57 / n=109 | — (HALT) |
| tsmom | +0.44% / PF 1.02 / n=113 | -0.47% / PF 0.83 / n=131 | -0.44% / PF 0.75 / n=169 |
| impulse | +1.46% / PF 1.18 / n=106 | -1.23% / PF 0.62 / n=186 | — (HALT) |
| mr_dip | +2.27% / PF 1.91 / n=29 | +0.10% / PF 1.00 / n=128 | -0.31% / PF 0.63 / n=244 |

### E.6 Rendimento per finestra semestrale (portafoglio OOS, %)
| Famiglia | 2021-01 | 2021-07 | 2022-01 | 2022-07 | 2023-01 | 2023-07 | 2024-01 | 2024-07 | 2025-01 | 2025-07 | 2026-01 | 2026-07 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| donchian | +10.1 (96) | -5.8 (89) | -1.3 (37) | -3.2 (51) | -2.1 (37) | HALT | HALT | HALT | HALT | HALT | HALT | HALT |
| squeeze | +4.2 (49) | -2.8 (66) | -5.5 (67) | -4.5 (42) | HALT | HALT | HALT | HALT | HALT | HALT | HALT | HALT |
| tsmom | +2.5 (61) | -2.0 (52) | -3.4 (25) | -1.9 (18) | -1.3 (39) | +2.8 (49) | +1.5 (41) | -1.1 (56) | -2.2 (26) | -2.1 (33) | -3.0 (13) | HALT |
| impulse | +9.7 (52) | -6.1 (54) | -5.5 (93) | -2.6 (40) | -1.0 (53) | HALT | HALT | HALT | HALT | HALT | HALT | HALT |
| mr_dip | +1.4 (19) | +0.1 (10) | -2.0 (26) | -0.9 (8) | +0.4 (7) | +2.4 (87) | -0.5 (52) | -1.9 (28) | +2.2 (18) | -2.3 (56) | -7.1 (50) | -0.5 (40) |

### E.7 Parametri scelti dal walk-forward per finestra
- **donchian**: 2021-01 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2021-07 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2022-01 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2022-07 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2023-01 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2023-07 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2024-01 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2024-07 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2025-01 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2025-07 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2026-01 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2026-07 {"N": 50, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}
- **squeeze**: 2021-01 {"pct": 0.1, "k_sl": 1.5, "k_tr": 3.5, "regime": 200}; 2021-07 {"pct": 0.25, "k_sl": 2.5, "k_tr": 2.0, "regime": 200}; 2022-01 {"pct": 0.25, "k_sl": 2.5, "k_tr": 3.5, "regime": 0}; 2022-07 {"pct": 0.25, "k_sl": 2.5, "k_tr": 3.5, "regime": 0}; 2023-01 {"pct": 0.25, "k_sl": 2.5, "k_tr": 3.5, "regime": 0}; 2023-07 {"pct": 0.25, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2024-01 {"pct": 0.25, "k_sl": 1.5, "k_tr": 3.5, "regime": 200}; 2024-07 {"pct": 0.25, "k_sl": 1.5, "k_tr": 3.5, "regime": 200}; 2025-01 {"pct": 0.1, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2025-07 {"pct": 0.1, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2026-01 {"pct": 0.1, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}; 2026-07 {"pct": 0.1, "k_sl": 2.5, "k_tr": 3.5, "regime": 200}
- **tsmom**: 2021-01 {"L": 42, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2021-07 {"L": 42, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2022-01 {"L": 42, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2022-07 {"L": 84, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2023-01 {"L": 84, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2023-07 {"L": 84, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2024-01 {"L": 42, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2024-07 {"L": 42, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2025-01 {"L": 42, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2025-07 {"L": 42, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2026-01 {"L": 84, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}; 2026-07 {"L": 84, "thr": 1.5, "k_sl": 3.0, "k_tr": 0.0}
- **impulse**: 2021-01 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 3.5}; 2021-07 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 2.0}; 2022-01 {"n": 1, "z0": 2.0, "k_sl": 2.5, "k_tr": 2.0}; 2022-07 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 2.0}; 2023-01 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 2.0}; 2023-07 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 3.5}; 2024-01 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 3.5}; 2024-07 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 3.5}; 2025-01 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 3.5}; 2025-07 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 3.5}; 2026-01 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 3.5}; 2026-07 {"n": 1, "z0": 3.0, "k_sl": 2.5, "k_tr": 3.5}
- **mr_dip**: 2021-01 {"n": 3, "z0": 3.0, "k_sl": 3.0, "exit_ma": 10, "regime": 200}; 2021-07 {"n": 1, "z0": 3.0, "k_sl": 3.0, "exit_ma": 10, "regime": 200}; 2022-01 {"n": 1, "z0": 2.0, "k_sl": 3.0, "exit_ma": 10, "regime": 200}; 2022-07 {"n": 3, "z0": 3.0, "k_sl": 3.0, "exit_ma": 10, "regime": 200}; 2023-01 {"n": 3, "z0": 3.0, "k_sl": 3.0, "exit_ma": 10, "regime": 200}; 2023-07 {"n": 1, "z0": 2.0, "k_sl": 2.0, "exit_ma": 10, "regime": 200}; 2024-01 {"n": 1, "z0": 2.0, "k_sl": 3.0, "exit_ma": 10, "regime": 200}; 2024-07 {"n": 1, "z0": 3.0, "k_sl": 3.0, "exit_ma": 10, "regime": 200}; 2025-01 {"n": 1, "z0": 3.0, "k_sl": 3.0, "exit_ma": 10, "regime": 200}; 2025-07 {"n": 1, "z0": 3.0, "k_sl": 3.0, "exit_ma": 10, "regime": 0}; 2026-01 {"n": 1, "z0": 3.0, "k_sl": 3.0, "exit_ma": 10, "regime": 0}; 2026-07 {"n": 1, "z0": 3.0, "k_sl": 3.0, "exit_ma": 20, "regime": 0}

### E.8 Diagnostica (non decisionale): stesso OOS **senza kill switch**
| Famiglia | n | exp. netta | PF | Sharpe | max DD | CAGR | % finestre >0 | idem a Tier 3 fisso: exp / PF / Sharpe / DD |
|---|---|---|---|---|---|---|---|---|
| donchian | 797 | -0.15% | 0.87 | -0.45 | 28.4% | -3.2% | 25.0% | -0.12% / 0.89 / -0.39 / 26.7% |
| squeeze | 625 | -0.61% | 0.78 | -0.77 | 31.3% | -4.7% | 33.3% | -0.55% / 0.80 / -0.68 / 29.6% |
| tsmom | 441 | -0.28% | 0.82 | -0.48 | 17.7% | -2.3% | 25.0% | -0.14% / 0.87 / -0.32 / 14.2% |
| impulse | 610 | -0.30% | 0.84 | -0.49 | 23.2% | -2.8% | 25.0% | -0.16% / 0.88 / -0.33 / 19.6% |
| mr_dip | 401 | +0.01% | 0.78 | -0.48 | 11.7% | -1.5% | 41.7% | +0.11% / 0.82 / -0.38 / 10.6% |

### E.9 Matrice di selezione e scarti
| Candidato | Edge stimato (OOS) | Robustezza | Sensitivity ±20% | Costo operativo | Complessità | Esito |
|---|---|---|---|---|---|---|
| donchian 4h | exp +0.16%/trade, PF 0.95, Sharpe -0.09 (senza kill: PF 0.87, DD 28%) | finestre >0 20%, LOPO -0.27…+0.22% | -0.14…+0.37% | RT 0.66%, uscite maker 63%, ~135 trade/anno | bassa | **Scartato**: PF < 1.3, Sharpe < 1, lordo/RT 1.26× < 3× |
| squeeze 4h | exp -0.69%/trade, PF 0.81, Sharpe -0.36 (senza kill: PF 0.78, DD 31%) | finestre >0 25%, LOPO -0.92…-0.43% | -0.86…-0.32% | RT 0.70%, uscite maker 40%, ~106 trade/anno | media | **Scartato**: PF < 1.3, Sharpe < 1, lordo/RT 0.03× < 3× |
| tsmom 4h | exp -0.21%/trade, PF 0.85, Sharpe -0.38 (senza kill: PF 0.82, DD 18%) | finestre >0 27%, LOPO -0.46…-0.10% | -0.76…-0.10% | RT 0.71%, uscite maker 82%, ~75 trade/anno | bassa | **Scartato**: PF < 1.3, Sharpe < 1, lordo/RT 0.70× < 3× |
| impulse 4h | exp -0.25%/trade, PF 0.85, Sharpe -0.30 (senza kill: PF 0.84, DD 23%) | finestre >0 20%, LOPO -0.65…-0.03% | -0.52…+0.19% | RT 0.79%, uscite maker 24%, ~104 trade/anno | bassa | **Scartato**: PF < 1.3, Sharpe < 1, lordo/RT 0.67× < 3× |
| mr_dip 4h | exp +0.01%/trade, PF 0.78, Sharpe -0.48 (senza kill: PF 0.78, DD 12%) | finestre >0 42%, LOPO -0.11…+0.24% | -0.06…+0.10% | RT 0.67%, uscite maker 84%, ~68 trade/anno | media | **Scartato**: PF < 1.3, Sharpe < 1, lordo/RT 1.01× < 3× |
| Tutte le famiglie a 1h | lordo mediano +0.04…+0.20% contro RT 0.56–0.70% | — | — | RT > lordo | — | **Scartate**: budget di edge (§E.0) |
| 5m / 15m (qualsiasi stile) | movimento medio a 5m–1h ≤0.65% < 3×RT minimo (1.2% a tier 5) | — | — | — | — | **Scartate**: budget di edge (§D) |
| Stagionalità oraria | +4…+5 bp contro RT 54–130 bp | non persistente (Baur et al. 2019) | — | — | bassa | **Scartata** |
| Carry | non esiste in spot senza leva/derivati | — | — | — | — | **Fuori perimetro** |
| Migliori config per coppia dello screening (SOL tsmom, DOGE mr_dip, DOGE/XRP donchian, ADA mr_dip, LINK 1h) | PF in-sample 1.34–2.04 | sul pool PF 0.91–1.19; mese migliore fino al 65% del PnL | cali del 60–90% su un parametro | — | — | **Scartate**: selezione a posteriori (audit #3/#7) |
| Ensemble delle 5 famiglie | non testato | — | — | — | media | **Non perseguito**: tutte le componenti hanno PF OOS < 1; la combinazione può ridurre la varianza, non creare expectancy positiva |

## F. Decisioni (dato il NO-GO)
| Decisione | Valore | Motivazione e numero |
|---|---|---|
| Trading attivo in LIVE | **Disabilitato** | OOS PF 0.78–0.95, Sharpe < 0 per 5/5 famiglie (§E.1); fee ×2 tutte negative tranne donchian +0.10% con PF 0.88 (§E.2) |
| Modalità LIVE ammessa | solo `dca` | unica opzione senza bisogno di edge; il rischio è dichiarato |
| Coppia DCA | BTC/EUR (`XXBTZEUR`) | spread mediano 0.14 bp, profondità bid ≤10 bp ~816k EUR (§C.0d), corr 5m con il proxy 0.991. Valuta EUR: si assume che il capitale sia in EUR (niente conversione FX, SEPA) |
| Allocazione DCA | 20% di `CAPITAL` | max DD di BTC nel campione 76.6% ⇒ DD di conto ≈ 0.20 × 76.6% = **15.3% < 20%** |
| Cadenza e ordine | 52 tranche settimanali (lunedì 10:00 UTC); limit post-only GTD 300 s al best bid, ripiazzato alla scadenza per il residuo, inseguimento massimo +2% sul mid d'apertura, mai market. A fine finestra (24 h) il non fillato va a riporto, con tetto di 2× tranche a settimana | maker 0.40% contro taker 0.80% al tier 1. Regole complete: `docs/plan.md` §0.1 |
| Capitale minimo (DCA) | `CAPITAL` ≥ **~851 EUR** con 52 tranche; sotto la soglia di 1.5 × min_cost il numero di tranche si riduce in automatico | tranche = 0.20·C/52 ≥ max(ordermin × prezzo, costmin) = max(0.00006 × 54,557; 0.45) = 3.27 EUR; i minimi sono letti a runtime da `AssetPairs` |
| Rendimento storico DCA (non è un edge) | avvio 2019: ×3.02 sull'investito, perdita peggiore sull'investito 27%; avvio 2021: ×1.39, perdita peggiore 55% (sul conto, al 20% di allocazione: 5.5% / 11%) | `research/out/dca.json` |
| Forward test (solo DRY_RUN) | `donchian` 4h, N=50, k_sl=2.5, k_tr=3.5, regime SMA200, universo PIT **top-4** EUR (vincolo 2–4 coppie; baseline in §K.4); rischio 0.5%/trade, max 3 posizioni, 30% di esposizione; capitale simulato 80% di `CAPITAL` | scelto **identico in 12/12 finestre** sia con top-8 sia con top-4; coerente con la letteratura sui trend. Nessuna correzione di sizing (§K.5). **OOS PF 0.95 (top-8) / 0.98 (top-4) ⇒ niente LIVE** |
| Forward test: lettura e abbandono | tutte le metriche a **fee tier 1** (0.40/0.80), più 5 bp per lato. **Checkpoint unico a 100 trade: se PF < 1.0, chiuso**. Kill totale simulato = chiuso. Configurazione congelata con hash | pre-registrato (`docs/plan.md` §0.2). A tier 1 il baseline top-4 ha PF 0.87 (§K.4): l'abbandono è l'esito più probabile, atteso in ~13–14 mesi (~89 trade/anno) |
| Sblocco LIVE del candidato | dopo ≥300 trade, a tier 1: PF > 1.3, Sharpe > 1, DD < 20%, ≥80% dei mesi positivi, positivo con fee ×2; più modifica del codice e OK esplicito | stessi gate dell'utente; circa 3.4 anni al ritmo del backtest |
| Timeframe | 4h | 5m–1h esclusi dal budget di edge (§D) |
| Filtro di liquidità (candidato) | spread ≤ 3× la mediana a 7 giorni e profondità entro 25 bp ≥ 10× il nozionale | spread mediani 0.1–5.9 bp (§C.0d) |
| Rischio del conto | breaker 3/5 perdite; kill giornaliero 4% e totale 15% sull'equity MTM, con flatten e HALT | 15% lascia margine sotto il 20% per gap e slippage del flatten (§E.8: DD senza kill fino al 31%) |
| DMS | `entry_only` (deviazione motivata) | il DMS cancella anche gli stop [V] (`docs/plan.md` §5) |
| Alert | apertura, chiusura, stop, errore API, WS giù > 30 s, kill switch, recovery, breaker, heartbeat esterno | requisito utente |

## G. Go / no-go
**NO-GO per il trading attivo.** Motivi, in ordine:
1. PF OOS < 1 in 5/5 famiglie.
2. Expectancy lorda ≤ 1.26× il round-trip, contro 3× richiesto.
3. Edge concentrato nel 2021 (sottoperiodi §E.5: 2022–26 negativi per tutte, salvo mr_dip 2022–23 con PF 1.00).
4. Il proxy sovrastima di 5–6 punti i fill maker sui book EUR sottili (§C.0c): un motivo in più di prudenza, non la causa del fallimento.

Alternative oneste:
1. **Non fare trading attivo** (cash in EUR). È l'unica opzione che rispetta insieme "expectancy positiva" e "DD ≤ 20%" sulla base di questi dati.
2. **DCA BTC/EUR al 20% del capitale**, maker-only, come nel §F. È esposizione al mercato, non un edge; il drawdown di conto storico stimato è ~15%.
3. Il bot completo viene comunque costruito e tenuto in **DRY_RUN come forward test** del candidato, con LIVE bloccato dai gate. È utile solo se si accetta che il verdetto possa cambiare dopo anni di dati, e non prima.

Che cosa potrebbe ribaltare il verdetto (ipotesi, **non testate qui**): holding più lunghi di 48 h (oggi fuori dai vincoli), un tier fee molto più basso (ma anche a tier 3 fisso i risultati restano negativi: §E.2), lo short (escluso dai vincoli).

## H. Risposta all'audit (stato dei rimedi)
| # audit | Voce | Stato | Evidenza |
|---|---|---|---|
| 0 | Documenti assenti | Risolto | questo file e `docs/plan.md` |
| 1 | Spread proxy vs Kraken | Parziale | Kraken: 16 snapshot in 30 min (C.0d); Binance non misurabile (host bloccato). Misura ≥7 gg spostata nell'accettazione DRY_RUN |
| 1c/2 | Microstructure, 720 candele native | Quantificato | range 5m 0.31–0.95× (C.0b); test di fedeltà su barre native da Trades a 92 giorni (C.0c): LINKEUR: fill 93.6% contro 99.2%, Δ lordo +6.5 bp; XDGEUR: fill 94.2% contro 99.0%, Δ lordo -8.6 bp. Con 720 candele 4h ci sono ≤10 segnali per strategia: test statisticamente impossibile, quindi sostituito dal test di fedeltà |
| 3 | Survivorship / selezione a posteriori | Risolto | universo point-in-time con LUNA, MATIC, EOS, ecc. (C.0); selezione solo sul pool nel walk-forward |
| 4 | Regimi | Documentato | C.0e |
| 5 | Leakage | Risolto | `scripts/test_lookahead.py`: segnali identici su dati troncati per 5/5 famiglie; barre incomplete mascherate |
| 6 | Embargo walk-forward | Risolto | purge+embargo 72 h, 12 finestre, eseguito (`wf2.py`) |
| 7 | Soglie sul campione completo | Risolto | griglie pre-registrate; selezione solo in-sample per finestra (E.7) |
| 8 | Fee ×2, slippage ×2 | Risolto | E.2: con fee ×2 solo donchian resta positivo per trade (+0.10%) ma con PF 0.88 |
| 9 | Budget di edge 3× | Risolto | §D ed E.1: definizione B 0.03–1.26× ⇒ nessuno passa |
| 10 | Impatto sul book | Parziale | collegato (`wf2.build_costs`, ×2 su stop e kill), ma stimato da 30 minuti di book |
| 11 | ≥100 trade OOS per candidato | Risolto | donchian 310, squeeze 224, tsmom 413, impulse 292, mr_dip 401 (≥300 solo donchian, tsmom, mr_dip) |
| 12 | Sensitivity ±20% | Risolto | E.3 |
| 13 | Concentrazione coppia/mese | Risolto | E.4 (LOPO, mese migliore), E.5 (sottoperiodi) |
| 14 | PF/Sharpe/DD OOS | Risolto: **tutti FAIL** | E.1 |
| 15 | ordermin/costmin/decimali | Risolto | minimi per coppia in `portfolio2`, floor a 8 decimali; capitale minimo DCA in §F |
| 16 | Tier fee | Risolto | E.2 a tier 1/2/3 fissi più tier dinamico |
| 17 | OCO, riconciliazione, DMS | Specificato | `docs/plan.md` §4–5 |
| 18 | Sizing dallo stop | Specificato e simulato | `docs/plan.md` §6, `bt.portfolio2` |
| 19 | Kill switch e breaker | Specificato e simulato | flatten più HALT in `portfolio2`, soglie in `docs/plan.md` §6 |
| 20 | Morte con posizione aperta | Specificato | `docs/plan.md` §4.5 caso (b) e §5 |
| 21 | Sezione scarti | Risolto | §E.9 |
| 22 | Assunzioni fragili | Risolto | §I |
| 23 | Claim senza backtest | Nessuno | ogni numero è generato da `research/out/*.json` (`build_docs.py`) |

## I. Assunzioni dichiarate e punti non verificati
1. I prezzi Binance USDT sono un proxy per i segnali Kraken (verificato a 1h–1d). Per l'esecuzione sui book EUR sottili il fill rate è ottimista di 5–6 punti, con effetto sul rendimento lordo di segno misto e sotto i 10 bp per trade (C.0b/C.0c), misurato solo su XDG/EUR e LINK/EUR; nessuna correzione applicata al §E.
2. Spread e impatto del book misurati su 30 minuti di domenica (C.0d) e applicati come costanti a tutto lo storico; per i simboli senza book Kraken misurato si usa lo spread peggiore (6 bp) e l'impatto della coppia peggiore.
3. Fill maker: pieno solo se il prezzo passa sotto il limite di almeno 1 bp. Niente coda né fill parziali; uno slippage di 5 bp si applica anche ai fill maker come costo di selezione avversa.
4. Stop: esecuzione a min(stop, open della barra) − spread/2 − 5 bp − 2× impatto. Nei crash veri lo slippage può essere maggiore.
5. Le fee del 2026 sono applicate a tutto lo storico; il tier è dinamico su un capitale di riferimento di 10k USD.
6. Si assume che uno stop non triggerato riservi i fondi [A]; che REST `close[]` crei un secondario per ogni fill [A]; che REST restituisca `cl_ord_id` [A]. Tutte da verificare nello smoke test live (`docs/plan.md` §8.6).
7. Downtime, `cancel_only` e manutenzioni Kraken non sono simulati; i gap dei dati Binance sono trattati come assenza di trading (barre incomplete mascherate per i segnali).
8. Universo point-in-time costruito sui volumi Binance, non Kraken.
9. Non verificato da questo ambiente: WebSocket (proxy), endpoint privati (nessuna key, per scelta), spread Binance (host non consentito), storico dei book Kraken (non esiste).

## K. Verifiche richieste all'approvazione (2026-11-22)

**K.1 BTC/EUR e coppie EUR nella schedule 0.20%/0.20%?** **No** [V]. La schedule simmetrica 0.20/0.20 copre solo coppie FX, coppie con stablecoin in base, coppie stablecoin/stablecoin e token pegged (§A.5, articolo 360039299431). Il "50k" è la soglia del tier 2 di quella stessa schedule (0.16% da 50k USD di volume), non un criterio sulla valuta base. Le coppie crypto/EUR seguono la schedule spot (0.40/0.80 al tier 1). Il Donchian quindi **non va rimisurato**: le fee usate nel §E sono quelle corrette.

**K.2 Saldo su piattaforma per il tier "0.10/0.20" dopo il 9 luglio 2026.** Nella tabella in vigore **un tier 0.10/0.20 non esiste**. I due più vicini:
- **Tier 7: 0.10% maker / 0.22% taker**, con AoP ≥ **400.000 USD ≈ 365.530 EUR** (oppure ≥250k USD di volume a 30 giorni);
- **Tier 8: 0.08% / 0.20%**, con AoP ≥ **600.000 USD ≈ 548.296 EUR** (oppure ≥500k USD di volume).

Conversione al cambio implicito Kraken XBTUSD/XBTEUR = 1.0943.

Convenienza sul capitale che il bot movimenta davvero, cioè il solo DCA in LIVE: acquisti per il 20% di C, una sola volta, maker.

| Capitale C (EUR) | tier se tutto C è su Kraken | maker | volume DCA (0.20·C) | fee DCA a T1 | fee DCA al tier di C | risparmio | extra da parcheggiare per il T7 | risparmio a T7 | prob. annua di default di pareggio sull'extra |
|---|---|---|---|---|---|---|---|---|---|
| 1,000 | T1 | 0.40% | 200 | 0.80 | 0.80 | 0.00 | 364,530 | 0.60 | 0.0002% |
| 5,000 | T1 | 0.40% | 1,000 | 4.00 | 4.00 | 0.00 | 360,530 | 3.00 | 0.0008% |
| 20,000 | T3 | 0.22% | 4,000 | 16.00 | 8.80 | 7.20 | 345,530 | 12.00 | 0.0035% |
| 50,000 | T4 | 0.20% | 10,000 | 40.00 | 20.00 | 20.00 | 315,530 | 30.00 | 0.0095% |
| 100,000 | T5 | 0.15% | 20,000 | 80.00 | 30.00 | 50.00 | 265,530 | 60.00 | 0.0226% |

Lettura:
- Anche nel caso più favorevole il DCA risparmia al massimo **0.30% × 0.20·C = 0.06% di C** in tutto il programma.
- Parcheggiare capitale extra su Kraken per arrivare al Tier 7 espone 265–365 mila EUR al rischio di controparte dell'exchange per risparmiare da 0.60 a 60 EUR. Conviene solo se la probabilità annua di default dell'exchange è sotto lo 0.0002–0.02%: un valore che nessuno può garantire.
- **Raccomandazione:** non tenere su Kraken più del necessario (la cassa del DCA e i BTC acquistati) per inseguire il tier. Il tier "gratuito" che deriva dal capitale già depositato (es. Tier 3 da ~18.3k EUR) va bene, ma non giustifica il deposito.

**Il Donchian a tier bassi** (stessa selezione pre-registrata, fee fisse per tier; serve a capire se un AoP alto cambierebbe il verdetto):

| Tier (maker/taker) | trade OOS | exp. netta/trade | PF netto | Sharpe | max DD | CAGR | % finestre >0 | lordo/RT | kill switch |
|---|---|---|---|---|---|---|---|---|---|
| T1 (0.40/0.80) | 241 | +0.01% | 0.867 | -0.25 | 15.3% | -1.10% | 25% | 1.07× | total_dd @ 2022-08 |
| T2 (0.30/0.60) | 275 | +0.10% | 0.901 | -0.18 | 15.1% | -0.88% | 20% | 1.15× | total_dd @ 2023-01 |
| T3 (0.22/0.38) | 329 | +0.08% | 0.929 | -0.13 | 15.2% | -0.72% | 20% | 1.15× | total_dd @ 2023-04 |
| T4 (0.20/0.35) | 330 | +0.10% | 0.931 | -0.13 | 15.2% | -0.71% | 20% | 1.20× | total_dd @ 2023-04 |
| T5 (0.15/0.30) | 342 | +0.17% | 0.945 | -0.10 | 15.0% | -0.58% | 20% | 1.39× | total_dd @ 2023-06 |
| T6 (0.12/0.25) | 351 | +0.22% | 0.949 | -0.09 | 15.1% | -0.56% | 17% | 1.56× | total_dd @ 2023-07 |
| T7 (0.10/0.22) | 353 | +0.23% | 0.954 | -0.08 | 15.1% | -0.51% | 17% | 1.66× | total_dd @ 2023-07 |
| T8 (0.08/0.20) | 357 | +0.20% | 0.957 | -0.08 | 15.0% | -0.47% | 17% | 1.69× | total_dd @ 2023-07 |
| T9 (0.06/0.18) | 364 | +0.21% | 0.959 | -0.07 | 15.1% | -0.45% | 17% | 1.82× | total_dd @ 2023-09 |

Anche a Tier 7–9 il PF OOS resta **< 0.96**, lo Sharpe resta negativo e il kill switch scatta comunque. La fee non è l'unico problema. Ho verificato sui 310 trade OOS di base (`out/wf2_donchian_trades.json`, terzili di distanza dello stop):

| Terzile | stop mediano | nozionale mediano | rend. per trade | win rate | PF |
|---|---|---|---|---|---|
| stretto | 5.1% | 934 USD | −0.36% | 38% | 0.91 |
| medio | 7.4% | 671 USD | −0.91% | 42% | 0.69 |
| largo | 11.0% | 458 USD | **+1.74%** | 46% | **1.29** |

Su quel campione il sizing dallo stop dà il nozionale maggiore ai trade che perdono: la media pesata per nozionale è −0.15%, contro +0.16% non pesata. **L'osservazione non si generalizza**: il ramo di ricerca dedicato (§K.5) mostra che non esiste una relazione monotona tra distanza dello stop e rendimento. Un tier basso, da solo, **non** sblocca il candidato.

**K.3 Sequenza di riavvio con posizione aperta e stop cancellati dal DMS:** scritta in `docs/plan.md` §4.6.

**K.4 Baseline del forward test con il vincolo "2–4 coppie".** Il §E usava un universo top-8, che viola il vincolo dell'utente. Il forward test userà quindi il top-4: stesso walk-forward pre-registrato, solo K=4 (`scripts/wf2_k.py`, `out/wf2_donchian_K4.json`). Il walk-forward sceglie gli **stessi parametri in 12/12 finestre** (N=50, k_sl=2.5, k_tr=3.5, SMA200: quelli del `config.toml`).

| Scenario | trade OOS | exp. netta/trade | PF netto | Sharpe | max DD | CAGR | % finestre >0 | lordo/RT | kill switch |
|---|---|---|---|---|---|---|---|---|---|
| tier dinamico (10k USD) | 524 | -0.12% | 0.981 | -0.03 | 12.0% | -0.30% | 50% | 0.83× | — |
| Tier 1 fisso | 486 | -0.40% | 0.872 | -0.37 | 15.1% | -1.97% | 45% | 0.62× | total_dd @ 2026-05 |
| Tier 3 fisso | 524 | -0.03% | 1.015 | +0.07 | 10.7% | +0.24% | 50% | 0.96× | — |
| fee ×2 | 195 | -1.05% | 0.699 | -0.61 | 15.1% | -2.08% | 20% | 0.23× | total_dd @ 2023-03 |
| senza kill switch | 524 | -0.12% | 0.981 | -0.03 | 12.0% | -0.30% | 50% | 0.83× | — |

Con K=4 il risultato è meno negativo che con K=8, ma **lontano dai gate** (PF > 1.3, Sharpe > 1): niente LIVE, e questi numeri sono il riferimento contro cui leggere il forward test.



**K.5 Ramo di ricerca sul sizing: rendimento per bucket di distanza dello stop.**
- Domanda: il rendimento cresce in modo **monotono** con la distanza dello stop? Se sì, un filtro o un sizing per bucket potrebbe correggere il problema del §K.2.
- Nota di metodo: lo stop è `entry − 2.5·ATR`, quindi la distanza **in ATR è costante** e un filtro "distanza minima in ATR" non selezionerebbe nulla. Varia la distanza in % del prezzo (2.5·ATR/prezzo), cioè la volatilità: i bucket sono di volatilità, analizzati sia tra coppie sia dentro la stessa coppia.
- Rendimenti netti a **fee tier 1** più 5 bp di slippage per lato. Quintili di distanza dello stop (`scripts/sizing_buckets.py`, `out/sizing_buckets.json`).


**Top-4, trade eseguiti dal portafoglio OOS** — N=524; Spearman(stop%, rend. netto) = -0.105 (p=0.0163); sequenza crescente in 2/4 passi (ρ sui bucket +0.10); ultimo − primo bucket = +0.28 pp (Welch t=+0.23, p=0.821)

| Bucket | N | stop % (range) | stop % mediano | expectancy netta T1 | t | PF (T1) | win rate | expectancy lorda |
|---|---|---|---|---|---|---|---|---|
| 1 | 105 | 1.3–3.4 | 2.8 | +0.01% | +0.03 | 1.01 | 38% | +1.04% |
| 2 | 105 | 3.4–4.6 | 4.0 | -0.38% | -0.81 | 0.81 | 38% | +0.66% |
| 3 | 104 | 4.6–5.8 | 5.2 | -1.22% | -2.57 | 0.55 | 32% | -0.18% |
| 4 | 105 | 5.8–7.3 | 6.5 | -1.04% | -1.54 | 0.66 | 37% | -0.01% |
| 5 | 105 | 7.3–22.8 | 9.4 | +0.29% | +0.25 | 1.07 | 42% | +1.35% |


**Top-4, tutti i segnali OOS (prima dei vincoli di portafoglio)** — N=928; Spearman(stop%, rend. netto) = -0.101 (p=0.00202); sequenza crescente in 1/4 passi (ρ sui bucket -0.40); ultimo − primo bucket = -0.40 pp (Welch t=-0.46, p=0.643)

| Bucket | N | stop % (range) | stop % mediano | expectancy netta T1 | t | PF (T1) | win rate | expectancy lorda |
|---|---|---|---|---|---|---|---|---|
| 1 | 186 | 1.3–3.6 | 2.9 | +0.27% | +0.76 | 1.17 | 41% | +1.31% |
| 2 | 185 | 3.6–4.6 | 4.0 | -0.42% | -1.13 | 0.79 | 35% | +0.62% |
| 3 | 186 | 4.6–5.9 | 5.1 | -0.67% | -1.77 | 0.73 | 38% | +0.37% |
| 4 | 185 | 5.9–7.3 | 6.5 | -1.11% | -2.28 | 0.64 | 42% | -0.06% |
| 5 | 186 | 7.3–22.8 | 9.5 | -0.13% | -0.17 | 0.97 | 44% | +0.91% |


**Top-8, trade eseguiti (run del §E, per confronto)** — N=310; Spearman(stop%, rend. netto) = +0.005 (p=0.923); sequenza crescente in 3/4 passi (ρ sui bucket +0.50); ultimo − primo bucket = +4.47 pp (Welch t=+1.97, p=0.0529)

| Bucket | N | stop % (range) | stop % mediano | expectancy netta T1 | t | PF (T1) | win rate | expectancy lorda |
|---|---|---|---|---|---|---|---|---|
| 1 | 62 | 1.3–5.4 | 4.5 | -1.28% | -2.86 | 0.42 | 31% | -0.26% |
| 2 | 62 | 5.4–6.7 | 6.1 | -0.03% | -0.03 | 0.99 | 45% | +1.01% |
| 3 | 62 | 6.7–8.3 | 7.4 | -2.35% | -2.57 | 0.44 | 31% | -1.28% |
| 4 | 62 | 8.3–10.4 | 9.3 | -0.62% | -0.48 | 0.84 | 40% | +0.40% |
| 5 | 62 | 10.4–23.2 | 12.6 | +3.19% | +1.44 | 1.87 | 52% | +4.27% |

*Stabilità per sottoperiodo (top-4, tutti i segnali OOS; terzili di stop % ricalcolati nel sottoperiodo)*

**2021** — N=212; Spearman(stop%, rend. netto) = -0.088 (p=0.202); sequenza crescente in 1/2 passi (ρ sui bucket -0.50); ultimo − primo bucket = -0.34 pp (Welch t=-0.23, p=0.821)

| Bucket | N | stop % (range) | stop % mediano | expectancy netta T1 | t | PF (T1) | win rate | expectancy lorda |
|---|---|---|---|---|---|---|---|---|
| 1 | 71 | 3.6–6.1 | 5.2 | -0.30% | -0.49 | 0.87 | 45% | +0.76% |
| 2 | 70 | 6.1–8.4 | 6.8 | -0.64% | -0.70 | 0.80 | 44% | +0.42% |
| 3 | 71 | 8.4–22.8 | 11.2 | -0.64% | -0.46 | 0.86 | 41% | +0.41% |

**2022-2023** — N=256; Spearman(stop%, rend. netto) = -0.106 (p=0.0898); sequenza crescente in 1/2 passi (ρ sui bucket -0.50); ultimo − primo bucket = -0.65 pp (Welch t=-0.62, p=0.535)

| Bucket | N | stop % (range) | stop % mediano | expectancy netta T1 | t | PF (T1) | win rate | expectancy lorda |
|---|---|---|---|---|---|---|---|---|
| 1 | 85 | 1.3–4.2 | 3.3 | +0.39% | +0.84 | 1.25 | 42% | +1.41% |
| 2 | 85 | 4.2–6.6 | 5.3 | -0.74% | -1.45 | 0.67 | 38% | +0.27% |
| 3 | 86 | 6.6–13.9 | 8.5 | -0.25% | -0.27 | 0.93 | 43% | +0.77% |

**2024-2026** — N=460; Spearman(stop%, rend. netto) = -0.135 (p=0.00377); sequenza crescente in 0/2 passi (ρ sui bucket -1.00); ultimo − primo bucket = -1.40 pp (Welch t=-1.85, p=0.0648)

| Bucket | N | stop % (range) | stop % mediano | expectancy netta T1 | t | PF (T1) | win rate | expectancy lorda |
|---|---|---|---|---|---|---|---|---|
| 1 | 153 | 1.7–3.8 | 3.0 | +0.14% | +0.33 | 1.09 | 39% | +1.19% |
| 2 | 153 | 3.8–5.1 | 4.4 | -0.31% | -0.75 | 0.86 | 38% | +0.75% |
| 3 | 154 | 5.1–14.5 | 6.3 | -1.26% | -2.03 | 0.63 | 36% | -0.21% |

*Scomposizione: tra coppie vs dentro la stessa coppia (top-4, tutti i segnali OOS)*

| Coppia | N | stop % mediano | expectancy netta T1 | PF | terzile basso / medio / alto (expectancy, N) |
|---|---|---|---|---|---|
| BTC | 256 | 3.6 | -0.72% | 0.66 | -0.24% (85) / -0.40% (85) / -1.50% (86) |
| DOGE | 29 | 8.0 | -1.54% | 0.65 | -2.66% (10) / -6.35% (9) / +3.91% (10) |
| ETH | 256 | 5.0 | -0.34% | 0.85 | -0.04% (85) / -0.04% (85) / -0.94% (86) |
| SOL | 203 | 6.3 | -0.70% | 0.76 | +0.32% (68) / -1.29% (67) / -1.13% (68) |
| XRP | 149 | 5.5 | +1.11% | 1.37 | +0.69% (50) / -1.33% (49) / +3.91% (50) |

Effetto **dentro la coppia** (terzili per coppia, aggregati): expectancy T1 basso/medio/alto = +0.02% (N=298, PF 1.01) / -0.83% (N=295, PF 0.68) / -0.17% (N=300, PF 0.95); Spearman(terzile, rend.) = -0.063 (p=0.0593).
Effetto **tra coppie** (stop % mediano della coppia vs expectancy della coppia): Spearman = -0.300 su 5 coppie (p=0.624).

**Esito: il pattern non regge.**
- Sul top-4 la correlazione a livello di trade è **negativa** (Spearman −0.10, p = 0.002 sui 928 segnali OOS). La sequenza dei bucket non è monotona: 1 passo crescente su 4 sui segnali, 2 su 4 sui trade eseguiti.
- Nel top-8 il "gli stop larghi vincono" dipendeva da un solo bucket estremo (+3.19%, t = 1.44, p = 0.053 sulla differenza), non da una relazione monotona (Spearman +0.005).
- In nessun sottoperiodo la relazione è crescente; nel 2024–26 è anzi decrescente (Spearman −0.135, p = 0.004).
- **Nessuna correzione proposta.** La configurazione del forward test resta N=50, k_sl=2.5, k_tr=3.5, SMA200, top-4. Un filtro scelto ora sarebbe calibrato sullo stesso campione OOS che l'ha suggerito.
- **Ipotesi secondaria pre-registrata, solo per il forward test.** H1: "rendimento netto a tier 1 maggiore nei trade con stop % più stretto". Specifica esatta in `docs/plan.md` §0.2 e nel codice (`src/krakbot/forward_test.py`):
  - campione: i primi 100 trade chiusi;
  - split per rango di `stop_pct`: posizioni 1–50 contro 51–100;
  - statistica: t di Welch con p a una coda;
  - soglia: **SUPPORTED se p < 0.05**.

  **Nessun effetto sul test in corso.** Se SUPPORTED, diventa ammissibile, con OK esplicito, un nuovo forward test della variante "stop_pct ≤ valore del 50° trade per rango", con hash nuovo e da zero trade.

## J. Riproducibilità
```bash
cd research
python3 -I scripts/fetch_binance.py                 # storico 5m (monthly) – e fetch_binance_daily.py per novembre
python3 -I scripts/kraken_collect.py ohlc           # 720 candele native per timeframe
python3 -I scripts/stats.py && python3 -I scripts/report_stats.py
python3 -I scripts/validate_proxy.py && python3 -I scripts/proxy_5m.py
python3 -I scripts/test_lookahead.py                # deve stampare "ok" per tutte le famiglie
python3 -I scripts/screen.py <coppie> screen_8.json # screening (solo per scartare)
python3 -I scripts/wf2.py                           # walk-forward pre-registrato (deterministico)
python3 -I scripts/wf2_diag.py donchian,squeeze,tsmom,impulse,mr_dip
python3 -I scripts/kraken_trades.py <coppie> 92 && python3 -I scripts/fidelity.py
python3 -I scripts/dca.py && python3 -I scripts/report_wf2.py && python3 -I scripts/build_docs.py
```
Tutti i passaggi sono deterministici (nessun campionamento casuale); i dati scaricati sono in `research/data/`.
