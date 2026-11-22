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
