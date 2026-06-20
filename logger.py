import sqlite3
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class TradeLogger:
    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT    NOT NULL,
                    action      TEXT    NOT NULL,
                    side        TEXT,
                    symbol      TEXT    NOT NULL,
                    price       REAL,
                    quantity    REAL,
                    stop_loss   REAL,
                    take_profit REAL,
                    order_id    TEXT
                )
            """)

    def log_trade(self, trade: dict) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trades
                    (timestamp, action, side, symbol, price, quantity, stop_loss, take_profit, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    trade.get("action"),
                    trade.get("side"),
                    trade.get("symbol"),
                    trade.get("price"),
                    trade.get("quantity"),
                    trade.get("stop_loss"),
                    trade.get("take_profit"),
                    str(trade.get("order_id", "")),
                ),
            )
        log.info("Trade guardado: %s", trade)

    def get_trades(self, limit: int = 50) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
