"""Shared harness: a full DCA engine on a temp DB with the paper broker, a manual clock and an in-memory book."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from krakbot.config import Settings, load_settings
from krakbot.domain import Mode
from krakbot.execution.dca_engine import DcaEngine
from krakbot.execution.paper import PaperBroker
from krakbot.kraken_rest.models import Instrument
from krakbot.kraken_ws import OrderBook
from krakbot.market_data import MarketData
from krakbot.monitor.alerts import MemoryAlerts
from krakbot.state import connect, migrate
from krakbot.state.dca import DcaRepo
from krakbot.state.liquidity import LiquidityRepo
from krakbot.state.repo import Store
from krakbot.strategies.dca import DcaPlan, plan

INST = Instrument(rest_key="XXBTZEUR", altname="XBTEUR", wsname="XBT/EUR", base="BTC", quote="EUR", pair_decimals=1,
                  lot_decimals=8, cost_decimals=5, tick_size=Decimal("0.1"), ordermin=Decimal("0.00006"),
                  costmin=Decimal("0.45"), status="online", symbol="BTC/EUR")
MONDAY_10 = int(datetime(2026, 11, 23, 10, 0, tzinfo=UTC).timestamp() * 1000)  # a DCA window start
WEEK_MS = 7 * 86_400_000


@dataclass
class Clock:
    t: int

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds * 1000)


@dataclass
class Harness:
    settings: Settings
    clock: Clock
    store: Store
    dca: DcaRepo
    liq: LiquidityRepo
    md: MarketData
    broker: PaperBroker
    alerts: MemoryAlerts
    engine: DcaEngine
    plan: DcaPlan
    bid: Decimal = Decimal("55566.8")
    ask: Decimal = Decimal("55597.9")
    _trade_seq: list[int] = field(default_factory=lambda: [0])

    def book(self, bid: str | Decimal | None = None, ask: str | Decimal | None = None) -> None:
        if bid is not None:
            self.bid = Decimal(bid)
        if ask is not None:
            self.ask = Decimal(ask)
        b = OrderBook("BTC/EUR", 10)
        b.snapshot([{"price": self.bid, "qty": Decimal("1.5")}], [{"price": self.ask, "qty": Decimal("1.5")}])
        self.md.on_book("BTC/EUR", b)

    def trade(self, price: str | Decimal, qty: str | Decimal) -> None:
        ts = datetime.fromtimestamp(self.clock.t / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
        self.md.on_trades("BTC/EUR", [{"price": Decimal(price), "qty": Decimal(qty), "timestamp": ts}])

    async def tick(self, seconds: float = 1.0, *, keep_book: bool = True) -> None:
        self.clock.advance(seconds)
        if keep_book:
            self.book()
        await self.engine.tick()

    def orders(self) -> list:
        rows = self.store.conn.execute("SELECT cl_ord_id FROM orders ORDER BY created_at, rowid").fetchall()
        return [self.store.orders.by_cl_ord_id(r[0]) for r in rows]

    def events(self, kind: str) -> list:
        return self.store.events.by_kind(kind)

    def texts(self) -> list[str]:
        return [t for _, t, _ in self.alerts.sent]

    def close(self) -> None:
        self.store.conn.close()


def make(tmp_path: Path, *, start_ms: int = MONDAY_10 + 30_000, capital: str = "5000",
         config_patch: tuple[str, str] | None = None) -> Harness:
    root = Path(__file__).resolve().parents[2]
    cfg = (root / "config.toml").read_text()
    if config_patch is not None:
        assert config_patch[0] in cfg
        cfg = cfg.replace(config_patch[0], config_patch[1], 1)
    (tmp_path / "config.toml").write_text(cfg)
    env = tmp_path / ".env"
    env.write_text(f"CAPITAL={capital}\n")
    env.chmod(0o600)
    settings = load_settings(tmp_path / "config.toml", env)
    clock = Clock(start_ms)
    conn = connect(settings.db_path)  # same file the CLI opens
    migrate(conn)
    store = Store(conn, clock)
    dca = DcaRepo(store)
    liq = LiquidityRepo(conn, clock)
    md = MarketData({"XBT/EUR": INST}, depth_bps=25, now_ms=clock)
    broker = PaperBroker(md, {"XBT/EUR": INST}, fee_rate=Decimal("0.004"), now_ms=clock)
    alerts = MemoryAlerts(clock)
    cfgd = settings.app.strategies.dca
    p = plan(capital=settings.secrets.CAPITAL, allocation=cfgd.allocation, tranches=cfgd.tranches,
             min_cost=INST.min_cost(Decimal("55582.35")), safety=cfgd.min_tranche_safety_factor)
    engine = DcaEngine(settings=settings, store=store, dca=dca, liq=liq, md=md, broker=broker, inst=INST,
                       mode=Mode.DRY_RUN, plan=p, fee_rate=lambda: Decimal("0.004"), alerts=alerts, now_ms=clock)
    h = Harness(settings, clock, store, dca, liq, md, broker, alerts, engine, p)
    h.book()
    return h
