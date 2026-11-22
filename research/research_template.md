# Dossier di ricerca — Fase 0 (Kraken Spot, long-only, senza leva)

Data: 2026-11-22 (UTC). Autore: Claude (quant dev). Lo studio è riproducibile: script in `research/scripts/`, output in `research/out/` (comandi in §J).

## Sintesi (leggere per prima)
- **Verdetto: NO-GO per il trading attivo.** Nessuna delle 5 famiglie compatibili con spot long-only (time-series momentum, breakout Donchian, breakout da compressione di volatilità, continuazione dopo impulso, mean-reversion su iperestensioni) supera i gate fuori campione. Test usato: walk-forward pre-registrato, 12 semestri 2021–2026, universo point-in-time con monete poi delistate, purge ed embargo di 72 h, regole di rischio complete, fee Kraken reali.
- Risultati OOS: PF netto **0.78–0.95**, Sharpe **−0.48…−0.09**, finestre positive **20–42%**; in 4 famiglie su 5 il kill switch del 15% si attiva. Senza kill switch: PF 0.78–0.89, DD fino al 31% (§E.8).
- **Causa principale: i costi.** Al tier 1 di oggi (0.40% maker / 0.80% taker, verificato) il round-trip costa 0.9–1.3%. Con holding ≤48 h l'expectancy lorda per trade delle famiglie migliori vale **1.0–1.3× il round-trip**, contro un requisito di 3×.
- L'edge di letteratura (momentum su orizzonti di giorni-settimane) è presente nei dati ma debole (t ≤ 2.7, §C.4) e concentrato nel 2021.
- I migliori risultati per singola coppia dello screening iniziale (PF fino a 2.0) erano **selezione a posteriori**: con gli stessi parametri sulle 10 coppie si scende a PF 0.91–1.19 (audit, §H).
- Alternativa onesta (§G): non fare trading attivo. Se si vuole esposizione cripto, DCA su BTC/EUR con ordini maker e allocazione massima del 20% del capitale, che tiene il drawdown di conto storico sotto il 20%. È esposizione beta, non un edge.

{{A_B}}

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

{{PROXY_TABLE}}

**C.0b: microstructure 5m (ultime 720 candele 5m Kraken)**

{{PROXY5_TABLE}}

**C.0c: fedeltà dell'esecuzione (stessi segnali; proxy Binance vs barre native Kraken da Trades, riscalate al livello USDT)**

{{FIDELITY_TABLE}}

**C.0d: book Kraken (16 snapshot per coppia, 2026-11-22 09:12–09:43 UTC)**

{{BOOK_TABLE}}

**C.0e: regimi coperti dal campione (BTC, per semestre)**: rialzo 2019H1 +186%, 2020H2 +213%, 2023H1 +83%, 2024H1–H2 +42%/+49%; ribasso 2019H2 −32%, 2022H1 −58% (DD 60%), 2022H2 −14%, 2026H1 −34%; laterale/volatile 2021H1 +20% con DD 50%, 2025H1 +13% con DD 28%, 2026H2 +0.3% a oggi. Il campione OOS (2021–2026) contiene rialzo, ribasso e fasi laterali.

{{STATS_TABLES}}

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

{{EDGE_TABLE}}

Conclusioni del budget:
1. **5m–1h esclusi**: il movimento medio a quegli orizzonti (≤0.65%) non arriva a 3× il RT nemmeno al tier 5 (1.20%).
2. Sul timeframe 4h, il **movimento** su 24–48 h (1.71–5.07%) supera 3× il RT maker→taker a 48 h già al tier 1 per le alt (non ETH, che passa dal tier 2); BTC passa solo al tier 3.
3. Ma il criterio che conta è l'**expectancy lorda** per trade ≥ 3× RT (definizione B dell'audit). Nel walk-forward vale **0.03–1.26×** (§E.1). Nessuna famiglia passa.
4. **Tier del conto**: dipende dal volume 30 g o dagli asset su Kraken. Una strategia con circa 140 trade/anno al 10–15% del capitale genera ~3× il capitale di volume mensile: tier 2–3 sopra circa 3k USD di capitale, tier 3 garantito con ≥20k USD di asset. Il backtest simula il tier dinamico (capitale di riferimento 10k USD) e riporta anche tier 1/2/3 fissi (§E.2).

## E. Selezione dei candidati

### E.0 Screening in-sample (8 coppie, 2019–2026; serve solo a scartare)
{{SCREEN_TABLE}}

Lettura: **a 1h nessuna famiglia ha expectancy netta mediana positiva a nessun tier** (lordo mediano +0.04…+0.20% contro RT 0.56–0.70%), quindi 1h è scartato. A 4h le mediane sono ≤0 al tier 3. Lo screening ha prodotto ~1.920 configurazioni distinte: **nessun numero dello screening è usato per scegliere**. La scelta è delegata al walk-forward pre-registrato.

### E.1–E.8 Walk-forward pre-registrato (OOS)
Protocollo (fissato in `research/scripts/wf2.py` prima di vedere i risultati):
- timeframe 4h;
- universo point-in-time: top-8 per volume a 90 giorni fra 20 simboli, inclusi i delistati;
- griglie piccole e fissate a priori;
- selezione per finestra sul **pool** di coppie (mai per coppia singola), con obiettivo la t-stat netta per trade a tier 2, smussata sui vicini di griglia;
- 12 semestri 2021H1–2026H2, train rolling 24 mesi, **purge ed embargo 72 h**;
- portafoglio con le regole di rischio del bot (`bt.portfolio2`): sizing dallo stop, 3 posizioni, 30% di esposizione, breaker, cooldown, kill switch con flatten e HALT, tier dinamico, minimi per coppia, impatto sul book.

{{WF2_TABLES}}

### E.9 Matrice di selezione e scarti
| Candidato | Edge stimato (OOS) | Robustezza | Sensitivity ±20% | Costo operativo | Complessità | Esito |
|---|---|---|---|---|---|---|
{{MATRIX_ROWS}}
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
{{AUDIT_ROWS}}

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

{{DCA_TIER_TABLE}}

Lettura:
- Anche nel caso più favorevole il DCA risparmia al massimo **0.30% × 0.20·C = 0.06% di C** in tutto il programma.
- Parcheggiare capitale extra su Kraken per arrivare al Tier 7 espone 265–365 mila EUR al rischio di controparte dell'exchange per risparmiare da 0.60 a 60 EUR. Conviene solo se la probabilità annua di default dell'exchange è sotto lo 0.0002–0.02%: un valore che nessuno può garantire.
- **Raccomandazione:** non tenere su Kraken più del necessario (la cassa del DCA e i BTC acquistati) per inseguire il tier. Il tier "gratuito" che deriva dal capitale già depositato (es. Tier 3 da ~18.3k EUR) va bene, ma non giustifica il deposito.

**Il Donchian a tier bassi** (stessa selezione pre-registrata, fee fisse per tier; serve a capire se un AoP alto cambierebbe il verdetto):

{{TIERS_TABLE}}

Anche a Tier 7–9 il PF OOS resta **< 0.96**, lo Sharpe resta negativo e il kill switch scatta comunque. La fee non è l'unico problema. Ho verificato sui 310 trade OOS di base (`out/wf2_donchian_trades.json`, terzili di distanza dello stop):

| Terzile | stop mediano | nozionale mediano | rend. per trade | win rate | PF |
|---|---|---|---|---|---|
| stretto | 5.1% | 934 USD | −0.36% | 38% | 0.91 |
| medio | 7.4% | 671 USD | −0.91% | 42% | 0.69 |
| largo | 11.0% | 458 USD | **+1.74%** | 46% | **1.29** |

Su quel campione il sizing dallo stop dà il nozionale maggiore ai trade che perdono: la media pesata per nozionale è −0.15%, contro +0.16% non pesata. **L'osservazione non si generalizza**: il ramo di ricerca dedicato (§K.5) mostra che non esiste una relazione monotona tra distanza dello stop e rendimento. Un tier basso, da solo, **non** sblocca il candidato.

**K.3 Sequenza di riavvio con posizione aperta e stop cancellati dal DMS:** scritta in `docs/plan.md` §4.6.

**K.4 Baseline del forward test con il vincolo "2–4 coppie".** Il §E usava un universo top-8, che viola il vincolo dell'utente. Il forward test userà quindi il top-4: stesso walk-forward pre-registrato, solo K=4 (`scripts/wf2_k.py`, `out/wf2_donchian_K4.json`). Il walk-forward sceglie gli **stessi parametri in 12/12 finestre** (N=50, k_sl=2.5, k_tr=3.5, SMA200: quelli del `config.toml`).

{{K4_TABLE}}

Con K=4 il risultato è meno negativo che con K=8, ma **lontano dai gate** (PF > 1.3, Sharpe > 1): niente LIVE, e questi numeri sono il riferimento contro cui leggere il forward test.



**K.5 Ramo di ricerca sul sizing: rendimento per bucket di distanza dello stop.**
- Domanda: il rendimento cresce in modo **monotono** con la distanza dello stop? Se sì, un filtro o un sizing per bucket potrebbe correggere il problema del §K.2.
- Nota di metodo: lo stop è `entry − 2.5·ATR`, quindi la distanza **in ATR è costante** e un filtro "distanza minima in ATR" non selezionerebbe nulla. Varia la distanza in % del prezzo (2.5·ATR/prezzo), cioè la volatilità: i bucket sono di volatilità, analizzati sia tra coppie sia dentro la stessa coppia.
- Rendimenti netti a **fee tier 1** più 5 bp di slippage per lato. Quintili di distanza dello stop (`scripts/sizing_buckets.py`, `out/sizing_buckets.json`).

{{SIZING_TABLES}}

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
Tutti i passaggi sono deterministici (nessun campionamento casuale).

**Dati grezzi non conservati (decisione del 2026-11-22).** I 368 MB di `research/data/` e le copie delle doc Kraken in `research/docs_raw/` esistevano solo nel container di lavoro e non sono stati salvati. Restano in git gli script, i risultati in `research/out/` e questo dossier. Riscaricabili: lo storico Binance (archivi pubblici mensili e giornalieri, immutabili). **Non** riscaricabili identici: le 720 candele native Kraken (finestra mobile), il campione di book Kraken del 2026-11-22, i trade Kraken paginati di quella finestra e il testo delle doc Kraken come era quel giorno. Rieseguire gli script oggi dà numeri su dati nuovi, non una verifica bit per bit di quelli citati qui.
