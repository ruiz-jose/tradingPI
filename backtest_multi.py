"""
Portfolio backtest — EMA Crossover 9/21 en múltiples pares.

Corre la misma estrategia de config.py sobre varios símbolos compartiendo
un único balance (comportamiento real de portfolio). El Sharpe del portfolio
se calcula sobre la curva de balance vela a vela (4H), no per-trade.

Uso:
    python backtest_multi.py
    python backtest_multi.py --symbols BTCUSDT ETHUSDT SOLUSDT
    python backtest_multi.py --months 36 --balance 1000
    python backtest_multi.py --months 36 --balance 1000 --csv portfolio.csv
"""

import asyncio
import argparse
import csv
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import aiohttp as _aiohttp
from aiohttp import ThreadedResolver as _ThreadedResolver

_orig_connector_init = _aiohttp.TCPConnector.__init__
def _patched_connector_init(self, *, resolver=None, **kwargs):
    if resolver is None:
        resolver = _ThreadedResolver()
    _orig_connector_init(self, resolver=resolver, **kwargs)
_aiohttp.TCPConnector.__init__ = _patched_connector_init

from binance import AsyncClient

from config import config
from strategy import EMAStrategy
from risk_manager import RiskManager
from backtest import Trade, SLIPPAGE, FEE, _fill_trade, _fmt, dynamic_slippage

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


# ──────────────────────────────────────────────────────────────────── #
# Estado por par                                                         #
# ──────────────────────────────────────────────────────────────────── #

@dataclass
class PairState:
    symbol:       str
    strategy:     EMAStrategy
    htf_strategy: EMAStrategy
    klines:       list           # 4H candles
    klines_htf:   list           # 1D candles
    htf_ptr:      int = 0
    position:     Optional[Trade] = None
    in_position:  bool = False
    trades:       List[Trade] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────── #
# Descarga en paralelo                                                   #
# ──────────────────────────────────────────────────────────────────── #

async def _fetch(symbol: str, interval: str, months: int) -> list:
    client = await AsyncClient.create(
        api_key=config.API_KEY or "",
        api_secret=config.API_SECRET or "",
        testnet=False,
    )
    since_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp() * 1000
    )
    all_klines, start = [], since_ms

    while True:
        batch = await client.get_klines(
            symbol=symbol, interval=interval,
            startTime=start, limit=1000,
        )
        if not batch:
            break
        all_klines.extend(batch)
        if len(batch) < 1000:
            break
        start = int(batch[-1][0]) + 1

    await client.close_connection()
    return all_klines


async def fetch_all(
    symbols: List[str], months: int
) -> Dict[str, Tuple[list, list]]:
    """Descarga 4H + 1D para todos los símbolos en paralelo."""
    coros_4h = [_fetch(sym, config.INTERVAL,     months) for sym in symbols]
    coros_1d = [_fetch(sym, config.HTF_INTERVAL, months) for sym in symbols]
    results_4h = await asyncio.gather(*coros_4h)
    results_1d = await asyncio.gather(*coros_1d)
    return {
        sym: (results_4h[i], results_1d[i])
        for i, sym in enumerate(symbols)
    }


# ──────────────────────────────────────────────────────────────────── #
# Motor de simulación                                                    #
# ──────────────────────────────────────────────────────────────────── #

def run_portfolio_backtest(
    pairs_data: Dict[str, Tuple[list, list]],
    initial_balance: float,
    portfolio_risk_cap: float = 0.0,
) -> Tuple[Dict[str, List[Trade]], List[float]]:
    """portfolio_risk_cap > 0 activa el cap de riesgo simultáneo entre símbolos
    (ver risk_manager.calculate_position_size_capped) — 0 mantiene el comportamiento
    actual (cada símbolo se sizea de forma independiente, sin tope conjunto)."""

    risk_manager = RiskManager()
    if portfolio_risk_cap > 0:
        config.PORTFOLIO_RISK_CAP = portfolio_risk_cap

    pairs: List[PairState] = []
    for symbol, (klines_4h, klines_1d) in pairs_data.items():
        strategy = EMAStrategy(
            config.EMA_FAST, config.EMA_SLOW, config.ATR_PERIOD,
            rsi_min=config.RSI_BUY_MIN, rsi_max=config.RSI_BUY_MAX,
            adx_period=config.ADX_PERIOD, adx_min=config.ADX_MIN,
            atr_vol_period=config.ATR_VOL_PERIOD,
            atr_vol_min_ratio=config.ATR_VOL_MIN_RATIO,
            atr_vol_max_ratio=config.ATR_VOL_MAX_RATIO,
            er_period=config.REGIME_ER_PERIOD,
            er_min=config.REGIME_ER_MIN,
            rsi_sell_min=config.RSI_SELL_MIN,
            rsi_sell_max=config.RSI_SELL_MAX,
            mr_rsi_oversold=config.MR_RSI_OVERSOLD,
            mr_rsi_overbought=config.MR_RSI_OVERBOUGHT,
        )
        htf_strategy = EMAStrategy(config.EMA_FAST, config.EMA_SLOW)
        pairs.append(PairState(
            symbol=symbol,
            strategy=strategy,
            htf_strategy=htf_strategy,
            klines=klines_4h,
            klines_htf=klines_1d,
        ))

    min_len = min(len(p.klines) for p in pairs)
    balance = initial_balance
    balance_history = [balance]

    for i in range(min_len):
        for pair in pairs:
            k = pair.klines[i]
            open_time = int(k[0])
            close     = float(k[4])
            high      = float(k[2])
            low       = float(k[3])
            volume    = float(k[5])

            # Actualizar HTF con velas 1D cerradas antes de esta vela 4H
            while pair.htf_ptr < len(pair.klines_htf):
                close_time_1d = int(pair.klines_htf[pair.htf_ptr][6])
                if close_time_1d < open_time:
                    pair.htf_strategy.update(
                        float(pair.klines_htf[pair.htf_ptr][4]), closed=True
                    )
                    pair.htf_ptr += 1
                else:
                    break

            pair.strategy.update(close, high=high, low=low, volume=volume, closed=True)

            if not pair.strategy.is_ready:
                continue

            closed_this_candle = False

            # ── Gestión de posición existente ──────────────────────────
            if pair.in_position and pair.position:
                current = pair.position

                # Scale-out (desactivado si SCALE_OUT_R = 0)
                if (config.SCALE_OUT_R > 0
                        and not current.scale_out_done
                        and current.atr > 0 and current.qty > 0):
                    so_mult = risk_manager.get_trailing_multiplier(pair.strategy.current_adx)
                    so_price = (current.entry_price
                                + config.SCALE_OUT_R * so_mult * current.atr)
                    if high >= so_price:
                        partial_qty = round(current.qty * config.SCALE_OUT_RATIO, 5)
                        if partial_qty >= config.MIN_QUANTITY:
                            fee_so = so_price * partial_qty * FEE
                            pnl_so = (so_price - current.entry_price) * partial_qty - fee_so
                            balance               += pnl_so
                            current.scale_out_pnl  = round(pnl_so, 4)
                            current.qty            = round(current.qty - partial_qty, 5)
                            current.sl             = current.entry_price
                            current.scale_out_done = True

                hit_sl = low  <= current.sl
                # TP fijo como techo de seguridad, incluso con trailing activo (antes era código muerto)
                hit_tp = high >= current.tp

                if hit_sl or hit_tp:
                    if hit_sl and hit_tp:
                        exit_price, reason = current.sl, "SL"
                    elif hit_tp:
                        exit_price, reason = current.tp, "TP"
                    else:
                        exit_price, reason = current.sl, "SL"

                    fee = exit_price * current.qty * FEE
                    pnl = (exit_price - current.entry_price) * current.qty - fee
                    balance += pnl
                    _fill_trade(current, open_time, exit_price, reason, pnl)
                    pair.trades.append(current)
                    pair.in_position = False
                    pair.position    = None
                    closed_this_candle = True

                elif config.TRAILING_STOP and pair.strategy.current_atr:
                    trail_mult = risk_manager.get_trailing_multiplier(pair.strategy.current_adx)
                    new_sl = round(
                        close - pair.strategy.current_atr * trail_mult, 2
                    )
                    if new_sl > current.sl:
                        current.sl = new_sl

            # ── Señales (solo si no cerró posición esta vela) ──────────
            if not closed_this_candle:
                signal = pair.strategy.get_signal()
                slip = dynamic_slippage(pair.strategy.current_atr, pair.strategy.avg_atr)

                if signal == "SELL" and pair.in_position and pair.position:
                    exit_price = close * (1 - slip)
                    fee = exit_price * pair.position.qty * FEE
                    pnl = (exit_price - pair.position.entry_price) * pair.position.qty - fee
                    balance += pnl
                    _fill_trade(pair.position, open_time, exit_price, "SIGNAL", pnl)
                    pair.trades.append(pair.position)
                    pair.in_position = False
                    pair.position    = None

                elif (signal == "BUY"
                      and not pair.in_position
                      and (pair.htf_strategy.is_bullish or config.ALLOW_BUY_IN_BEARISH_HTF)
                      and pair.strategy.can_enter_long):
                    atr   = pair.strategy.current_atr
                    entry = close * (1 + slip)
                    if portfolio_risk_cap > 0:
                        open_risk_usdt = sum(
                            p.position.risk_usdt for p in pairs if p.in_position and p.position
                        )
                        qty, risk_usdt = risk_manager.calculate_position_size_capped(
                            balance, open_risk_usdt, entry, atr,
                            pair.strategy.vol_multiplier, pair.strategy.current_adx,
                        )
                    else:
                        qty = risk_manager.calculate_position_size(
                            balance, entry, atr, pair.strategy.vol_multiplier, pair.strategy.current_adx
                        )
                        risk_usdt = balance * config.RISK_PER_TRADE * pair.strategy.vol_multiplier
                    if qty > 0:
                        balance -= entry * qty * FEE
                        pair.position = Trade(
                            entry_time  = _fmt(open_time),
                            entry_price = round(entry, 2),
                            qty         = qty,
                            initial_qty = qty,
                            sl          = risk_manager.get_stop_loss(entry, "BUY", atr, pair.strategy.current_adx),
                            tp          = risk_manager.get_take_profit(entry, "BUY", atr),
                            atr         = round(atr, 2) if atr else 0,
                            risk_usdt   = risk_usdt,
                        )
                        pair.in_position = True

        balance_history.append(balance)

    # Cerrar posiciones abiertas al final del periodo
    for pair in pairs:
        if pair.in_position and pair.position:
            last_close = float(pair.klines[min_len - 1][4]) * (1 - SLIPPAGE)
            fee = last_close * pair.position.qty * FEE
            pnl = (last_close - pair.position.entry_price) * pair.position.qty - fee
            balance += pnl
            _fill_trade(
                pair.position,
                int(pair.klines[min_len - 1][0]),
                last_close, "FIN", pnl,
            )
            pair.trades.append(pair.position)

    return {p.symbol: p.trades for p in pairs}, balance_history


# ──────────────────────────────────────────────────────────────────── #
# Métricas                                                              #
# ──────────────────────────────────────────────────────────────────── #

def _symbol_stats(trades: List[Trade], initial_balance: float, months: int) -> dict:
    """Métricas por par, calculadas sobre los trades (sin curva de balance)."""
    if not trades:
        return {}
    winners     = [t for t in trades if t.pnl_usdt > 0]
    losers      = [t for t in trades if t.pnl_usdt <= 0]
    gross_profit = sum(t.pnl_usdt for t in winners)
    gross_loss   = abs(sum(t.pnl_usdt for t in losers))
    total_pnl    = sum(t.pnl_usdt for t in trades)

    ret_series = [t.pnl_usdt / initial_balance for t in trades]
    mean_r   = sum(ret_series) / len(ret_series)
    variance = sum((r - mean_r) ** 2 for r in ret_series) / max(len(ret_series) - 1, 1)
    std_r    = math.sqrt(variance) if variance > 0 else 0
    tpy      = len(trades) / months * 12
    sharpe   = (mean_r / std_r) * math.sqrt(tpy) if std_r > 0 else 0.0

    by_reason = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    return {
        "total":         len(trades),
        "winners":       len(winners),
        "win_rate":      len(winners) / len(trades) * 100,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "sharpe":        sharpe,
        "total_pnl":     total_pnl,
        "expectancy":    total_pnl / len(trades),
        "by_reason":     by_reason,
    }


def _portfolio_metrics(
    balance_history: list, initial_balance: float, months: int
) -> dict:
    """Métricas del portfolio calculadas sobre la curva de balance 4H."""
    final_balance = balance_history[-1]
    total_return  = (final_balance - initial_balance) / initial_balance * 100
    annual_return = total_return / months * 12

    peak, max_dd = balance_history[0], 0.0
    for b in balance_history:
        peak   = max(peak, b)
        max_dd = max(max_dd, (peak - b) / peak * 100)

    # Sharpe sobre retornos candle-a-candle (6 velas 4H/día, 365 días/año)
    returns = [
        (balance_history[i] - balance_history[i - 1]) / balance_history[i - 1]
        for i in range(1, len(balance_history))
        if balance_history[i - 1] > 0
    ]
    sharpe = 0.0
    if len(returns) > 1:
        mean_r   = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / max(len(returns) - 1, 1)
        std_r    = math.sqrt(variance) if variance > 0 else 0
        if std_r > 0:
            candles_per_year = 6 * 365
            sharpe = (mean_r / std_r) * math.sqrt(candles_per_year)

    calmar = annual_return / max_dd if max_dd > 0 else float("inf")

    return {
        "final_balance": final_balance,
        "total_return":  total_return,
        "annual_return": annual_return,
        "max_drawdown":  max_dd,
        "sharpe":        sharpe,
        "calmar":        calmar,
    }


# ──────────────────────────────────────────────────────────────────── #
# Informe                                                               #
# ──────────────────────────────────────────────────────────────────── #

def print_portfolio_report(
    trades_by_symbol: Dict[str, List[Trade]],
    balance_history: list,
    initial_balance: float,
    months: int,
):
    sep = "─" * 60

    print()
    print("=" * 62)
    print("  PORTFOLIO BACKTEST — EMA 9/21 Multi-Par")
    print(f"  Símbolos: {', '.join(trades_by_symbol.keys())}")
    print(f"  Periodo: {months} meses  |  Intervalo: {config.INTERVAL}")
    print(f"  Slippage: {SLIPPAGE*100:.1f}%  |  Comisión: {FEE*100:.1f}% por lado")
    print("=" * 62)

    # ── Por símbolo ──────────────────────────────────────────────────
    for symbol, trades in trades_by_symbol.items():
        print(f"\n  ── {symbol} " + "─" * (54 - len(symbol)))
        if not trades:
            print("     Sin operaciones en el periodo.")
            continue
        m = _symbol_stats(trades, initial_balance, months)
        br = m["by_reason"]
        print(f"  Trades  : {m['total']:>4}  |  "
              f"Ganadores: {m['winners']:>3} ({m['win_rate']:.1f}%)")
        print(f"  PF      : {m['profit_factor']:>6.2f}  |  "
              f"Sharpe (per-trade): {m['sharpe']:.2f}")
        print(f"  PnL     : {m['total_pnl']:>+8.2f} USDT  |  "
              f"Expectativa: {m['expectancy']:>+.2f} USDT/op")
        print(f"  Salidas : TP={br.get('TP',0)}  SL={br.get('SL',0)}  "
              f"SIGNAL={br.get('SIGNAL',0)}  FIN={br.get('FIN',0)}")

    # ── Portfolio combinado ──────────────────────────────────────────
    pm = _portfolio_metrics(balance_history, initial_balance, months)
    total_trades = sum(len(t) for t in trades_by_symbol.values())

    print(f"\n  {sep}")
    print("  PORTFOLIO COMBINADO (balance compartido)")
    print(f"  {sep}")
    print(f"  Balance inicial   : {initial_balance:>10,.2f} USDT")
    print(f"  Balance final     : {pm['final_balance']:>10,.2f} USDT")
    print(f"  Retorno total     : {pm['total_return']:>+9.2f} %")
    print(f"  Retorno anual.    : {pm['annual_return']:>+9.2f} %")
    print(f"  Max Drawdown      : {pm['max_drawdown']:>9.2f} %")
    print(f"  Sharpe portfolio  : {pm['sharpe']:>9.2f}  (curva balance 4H)")
    print(f"  Calmar portfolio  : {pm['calmar']:>9.2f}")
    print(f"  Total trades      : {total_trades}")

    c1 = pm["sharpe"]       >= 1.0
    c2 = pm["max_drawdown"] <  20.0
    c3 = pm["calmar"]       >= 1.0
    c4 = pm["total_return"] >  0.0
    score = sum([c1, c2, c3, c4])
    veredicto = {4: "EXCELENTE", 3: "BUENA", 2: "ACEPTABLE", 1: "MEJORABLE", 0: "NO VIABLE"}

    print()
    print("=" * 62)
    print("  CRITERIOS PORTFOLIO:")
    print(f"  Sharpe ≥ 1.0     : {'OK' if c1 else 'FALTA':4}  ({pm['sharpe']:.2f})")
    print(f"  Max DD < 20%     : {'OK' if c2 else 'FALTA':4}  ({pm['max_drawdown']:.1f}%)")
    print(f"  Calmar ≥ 1.0     : {'OK' if c3 else 'FALTA':4}  ({pm['calmar']:.2f})")
    print(f"  Retorno > 0      : {'OK' if c4 else 'FALTA':4}  ({pm['total_return']:+.2f}%)")
    print(f"  VEREDICTO: {veredicto.get(score, 'N/A')}  ({score}/4 criterios)")
    print("=" * 62)
    print()


# ──────────────────────────────────────────────────────────────────── #
# CSV                                                                    #
# ──────────────────────────────────────────────────────────────────── #

def save_csv(trades_by_symbol: Dict[str, List[Trade]], path: str):
    fields = ["symbol", "entry_time", "entry_price", "qty", "sl", "tp", "atr",
              "exit_time", "exit_price", "exit_reason", "pnl_usdt", "pnl_pct"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for symbol, trades in trades_by_symbol.items():
            for t in trades:
                row = {k: getattr(t, k) for k in fields if k != "symbol"}
                row["symbol"] = symbol
                w.writerow(row)
    print(f"  Log de operaciones guardado en: {path}")


# ──────────────────────────────────────────────────────────────────── #
# Punto de entrada                                                       #
# ──────────────────────────────────────────────────────────────────── #

async def main(
    symbols: List[str], months: int, initial_balance: float, csv_path: Optional[str],
    portfolio_risk_cap: float = 0.0,
):
    print(f"\nDescargando datos para {len(symbols)} pares ({months} meses)...")
    pairs_data = await fetch_all(symbols, months)

    for sym, (k4h, k1d) in pairs_data.items():
        print(f"  {sym}: {len(k4h):,} velas 4H  |  {len(k1d):,} velas 1D")

    if portfolio_risk_cap > 0:
        print(f"\nCap de riesgo de portafolio activo: {portfolio_risk_cap*100:.1f}% del balance")

    print("\nEjecutando simulación...\n")
    trades_by_symbol, balance_history = run_portfolio_backtest(
        pairs_data, initial_balance, portfolio_risk_cap
    )

    print_portfolio_report(trades_by_symbol, balance_history, initial_balance, months)

    if csv_path:
        save_csv(trades_by_symbol, csv_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portfolio Backtest — EMA Multi-Par")
    parser.add_argument(
        "--symbols", nargs="+", default=DEFAULT_SYMBOLS,
        help=f"Símbolos a operar (default: {' '.join(DEFAULT_SYMBOLS)})",
    )
    parser.add_argument("--months",  type=int,   default=36,    help="Meses de historial (default: 36)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    parser.add_argument("--csv",     type=str,   default=None,  help="Exportar operaciones a CSV")
    parser.add_argument(
        "--portfolio-risk-cap", type=float, default=0.0,
        help="Cap de riesgo simultáneo entre símbolos, ej. 0.01 = 1%% del balance (0 = desactivado, default)",
    )
    args = parser.parse_args()

    from typing import Optional  # needed inside __main__ scope
    asyncio.run(main(args.symbols, args.months, args.balance, args.csv, args.portfolio_risk_cap))
