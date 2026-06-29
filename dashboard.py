"""
Dashboard web del bot — proceso independiente, solo lectura.

No importa ni toca el loop de trading de bot.py: lee trades.db (mismo archivo que
escribe TradeLogger) para el historial, y consulta el API de Binance Futures
(misma cuenta/keys de config.py) para balance y posiciones en vivo. Puede
correr en paralelo al bot sin riesgo de interferir con las órdenes reales.
"""
import sqlite3
import time
from pathlib import Path

from flask import Flask, jsonify, render_template
from binance import Client
from binance.exceptions import BinanceAPIException

from config import config

app = Flask(__name__)

DB_PATH = "trades.db"
LOG_PATH = "bot.log"

_client: Client | None = None
_client_error: str | None = None


def get_client() -> Client | None:
    """Cliente Binance Futures perezoso y cacheado — solo lectura (balance/posiciones)."""
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client
    try:
        client = Client(config.FUTURES_API_KEY, config.FUTURES_API_SECRET)
        if config.FUTURES_TESTNET:
            client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        client.futures_account_balance()  # valida credenciales
        _client = client
    except Exception as exc:
        _client_error = str(exc)
    return _client


def fetch_trades() -> list[dict]:
    if not Path(DB_PATH).exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades ORDER BY timestamp ASC").fetchall()
    return [dict(r) for r in rows]


def compute_stats(trades: list[dict]) -> dict:
    """Reconstruye PnL por operación emparejando OPEN -> (SCALE_OUT)* -> CLOSE/CLOSED_BY_SL/CLOSED_BY_TP
    por símbolo. Solo hay una posición abierta a la vez por símbolo, así que no hace falta
    desambiguar por order_id."""
    open_trade: dict[str, dict] = {}
    closed: list[dict] = []

    for t in trades:
        symbol, action, side = t["symbol"], t["action"], t["side"]
        if action == "OPEN":
            open_trade[symbol] = {"side": side, "price": t["price"], "pnl_acc": 0.0, "ts": t["timestamp"]}
            continue

        ot = open_trade.get(symbol)
        if not ot:
            continue
        is_long = ot["side"] == "LONG"
        leg_pnl = (t["price"] - ot["price"]) * t["quantity"] if is_long else (ot["price"] - t["price"]) * t["quantity"]

        if action == "SCALE_OUT":
            ot["pnl_acc"] += leg_pnl
        elif action in ("CLOSE", "CLOSED_BY_SL", "CLOSED_BY_TP"):
            total_pnl = ot["pnl_acc"] + leg_pnl
            closed.append({
                "symbol": symbol, "side": ot["side"], "entry": ot["price"], "exit": t["price"],
                "pnl": total_pnl, "closed_by": action, "timestamp": t["timestamp"],
            })
            del open_trade[symbol]

    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] <= 0]
    gross_win = sum(c["pnl"] for c in wins)
    gross_loss = abs(sum(c["pnl"] for c in losses))

    consecutive_losses = 0
    for c in reversed(closed):
        if c["pnl"] <= 0:
            consecutive_losses += 1
        else:
            break

    by_symbol: dict[str, dict] = {}
    for c in closed:
        s = by_symbol.setdefault(c["symbol"], {"trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        s["wins"] += 1 if c["pnl"] > 0 else 0
        s["pnl"] += c["pnl"]
    for s in by_symbol.values():
        s["win_rate"] = round(100 * s["wins"] / s["trades"], 1) if s["trades"] else 0.0

    equity = []
    cum = 0.0
    for c in closed:
        cum += c["pnl"]
        equity.append({"timestamp": c["timestamp"], "cumulative_pnl": round(cum, 2)})

    return {
        "total_trades": len(closed),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if gross_win == 0 else float("inf")),
        "total_pnl": round(sum(c["pnl"] for c in closed), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "consecutive_losses": consecutive_losses,
        "open_count": len(open_trade),
        "by_symbol": by_symbol,
        "equity_curve": equity,
        "recent_closed": list(reversed(closed))[:20],
    }


def fetch_live_overview() -> dict:
    client = get_client()
    if client is None:
        return {"connected": False, "error": _client_error or "Credenciales no configuradas"}

    try:
        balances = client.futures_account_balance()
        balance = next((float(b["balance"]) for b in balances if b["asset"] == "USDT"), 0.0)

        positions = []
        for p in client.futures_position_information():
            amt = float(p["positionAmt"])
            if amt == 0:
                continue
            positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "qty": abs(amt),
                "entry_price": float(p["entryPrice"]),
                "mark_price": float(p["markPrice"]),
                "unrealized_pnl": float(p["unRealizedProfit"]),
                "leverage": int(p["leverage"]),
            })

        return {
            "connected": True,
            "mode": "TESTNET" if config.FUTURES_TESTNET else "LIVE",
            "balance": balance,
            "unrealized_pnl": round(sum(p["unrealized_pnl"] for p in positions), 2),
            "open_positions": positions,
            "max_concurrent_positions": config.MAX_CONCURRENT_POSITIONS,
            "symbols": config.SYMBOLS,
            "interval": config.INTERVAL,
            "leverage": config.LEVERAGE,
        }
    except BinanceAPIException as exc:
        return {"connected": False, "error": f"Binance API: {exc.message}"}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


def tail_log(n: int = 80) -> list[str]:
    path = Path(LOG_PATH)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return [l.rstrip("\n") for l in lines[-n:]]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    return jsonify(fetch_live_overview())


@app.route("/api/stats")
def api_stats():
    return jsonify(compute_stats(fetch_trades()))


@app.route("/api/trades")
def api_trades():
    trades = fetch_trades()
    return jsonify(list(reversed(trades))[:50])


@app.route("/api/log")
def api_log():
    return jsonify({"lines": tail_log(80), "server_time": time.time()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
