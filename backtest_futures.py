"""
Backtest de Futuros — EMA Crossover 9/21 + filtro HTF + ATR, CON SHORTS.

Extiende el motor de backtest.py para simular también posiciones cortas
(death cross + HTF bajista + filtros), cobra el funding real histórico de
Binance (en vez de una tasa asumida constante) y aplica el filtro de
funding de config.py (FUNDING_FILTER_ENABLED) para medir si evitar entradas
con funding desfavorable mejora el resultado neto.

Uso:
    python backtest_futures.py                       # 36 meses, 1000 USDT
    python backtest_futures.py --months 60 --csv shorts.csv
    python backtest_futures.py --no-funding-filter    # comparar sin el filtro
"""

import asyncio
import argparse
import bisect
import sys
from datetime import datetime, timezone, timedelta
from typing import List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from binance import AsyncClient

from config import config
from strategy import EMAStrategy
from risk_manager import RiskManager
from backtest import (
    Trade, FEE, dynamic_slippage, _fill_trade, _fmt,
    fetch_klines, compute_metrics, print_report, save_csv,
)


async def fetch_funding_history(symbol: str, months: int) -> List[tuple]:
    """Descarga el historial real de funding rate (cada 8h) de Binance Futures.
    Devuelve [(fundingTime_ms, fundingRate), ...] ordenado ascendente."""
    client = await AsyncClient.create(
        api_key=config.FUTURES_API_KEY or "",
        api_secret=config.FUTURES_API_SECRET or "",
        testnet=False,
    )
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp() * 1000)
    events, start = [], since_ms

    while True:
        batch = await client.futures_funding_rate(symbol=symbol, startTime=start, limit=1000)
        if not batch:
            break
        events.extend((int(r["fundingTime"]), float(r["fundingRate"])) for r in batch)
        if len(batch) < 1000:
            break
        start = int(batch[-1]["fundingTime"]) + 1

    await client.close_connection()
    return events


def run_backtest_futures(
    klines_1h: list,
    klines_4h: list,
    initial_balance: float,
    funding_events: Optional[List[tuple]] = None,
    funding_filter_enabled: Optional[bool] = None,
) -> tuple[List[Trade], List[float], float]:

    funding_events = funding_events or []
    funding_times  = [t for t, _ in funding_events]
    filter_enabled = config.FUNDING_FILTER_ENABLED if funding_filter_enabled is None else funding_filter_enabled

    def _current_funding_rate(ts: int) -> Optional[float]:
        """Última tasa de funding conocida hasta el instante ts (proxy del rate vigente)."""
        idx = bisect.bisect_right(funding_times, ts) - 1
        return funding_events[idx][1] if idx >= 0 else None

    def _funding_pnl(entry_time: int, exit_time: int, qty: float, entry_price: float, is_long: bool) -> float:
        """Funding neto cobrado/pagado entre entry_time y exit_time (eventos reales).
        Rate > 0 -> LONG paga a SHORT; rate < 0 -> SHORT paga a LONG."""
        lo = bisect.bisect_right(funding_times, entry_time)
        hi = bisect.bisect_right(funding_times, exit_time)
        rate_sum = sum(funding_events[i][1] for i in range(lo, hi))
        signed = entry_price * qty * rate_sum
        return -signed if is_long else signed  # cost para el que paga, ingreso para el que recibe

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
    risk_manager = RiskManager()

    balance         = initial_balance
    balance_history = [balance]
    trades: List[Trade]      = []
    current: Optional[Trade] = None
    in_position                = False
    htf_ptr                    = 0
    candles_in_position        = 0
    candles_ready               = 0
    entry_open_time: int        = 0

    def _close_trade(open_time: int, exit_price: float, reason: str) -> None:
        nonlocal balance, in_position, current
        is_long = current.side == "LONG"
        fee = exit_price * current.qty * FEE
        if is_long:
            pnl = (exit_price - current.entry_price) * current.qty - fee
        else:
            pnl = (current.entry_price - exit_price) * current.qty - fee
        funding = _funding_pnl(entry_open_time, open_time, current.qty, current.entry_price, is_long)
        pnl += funding
        current.funding_cost = round(-funding, 4)  # positivo = costo neto pagado
        balance += pnl
        _fill_trade(current, open_time, exit_price, reason, pnl)
        trades.append(current)
        in_position, current = False, None

    for k in klines_1h:
        open_time = int(k[0])
        close     = float(k[4])
        high      = float(k[2])
        low       = float(k[3])
        volume    = float(k[5])

        while htf_ptr < len(klines_4h):
            close_time_4h = int(klines_4h[htf_ptr][6])
            if close_time_4h < open_time:
                htf_strategy.update(float(klines_4h[htf_ptr][4]), closed=True)
                htf_ptr += 1
            else:
                break

        strategy.update(close, high=high, low=low, volume=volume, closed=True)

        if not strategy.is_ready:
            balance_history.append(balance)
            continue

        candles_ready += 1
        if in_position:
            candles_in_position += 1

        if in_position and current:
            is_long = current.side == "LONG"

            hit_sl = (low <= current.sl) if is_long else (high >= current.sl)
            hit_tp = (high >= current.tp) if is_long else (low <= current.tp)

            if hit_sl or hit_tp:
                if hit_sl and hit_tp:
                    exit_price, reason = current.sl, "SL"
                elif hit_tp:
                    exit_price, reason = current.tp, "TP"
                else:
                    exit_price, reason = current.sl, "SL"
                _close_trade(open_time, exit_price, reason)
                balance_history.append(balance)
                continue

            if config.TRAILING_STOP and strategy.current_atr:
                trail_mult = risk_manager.get_trailing_multiplier(strategy.current_adx)
                if is_long:
                    new_sl = round(close - strategy.current_atr * trail_mult, 2)
                    if new_sl > current.sl:
                        current.sl = new_sl
                else:
                    new_sl = round(close + strategy.current_atr * trail_mult, 2)
                    if new_sl < current.sl:
                        current.sl = new_sl

        signal = strategy.get_signal()
        slip = dynamic_slippage(strategy.current_atr, strategy.avg_atr)

        if in_position and current:
            is_long = current.side == "LONG"
            close_signal = (signal == "SELL" and is_long) or (signal == "BUY" and not is_long)
            if close_signal:
                exit_price = close * (1 - slip) if is_long else close * (1 + slip)
                _close_trade(open_time, exit_price, "SIGNAL")

        elif not in_position:
            funding_rate = _current_funding_rate(open_time)

            htf_ok_long = htf_strategy.is_bullish or config.ALLOW_BUY_IN_BEARISH_HTF
            long_funding_ok = not (
                filter_enabled and funding_rate is not None
                and funding_rate > config.FUNDING_RATE_MAX_FOR_LONG
            )
            short_funding_ok = not (
                filter_enabled and funding_rate is not None
                and funding_rate < config.FUNDING_RATE_MIN_FOR_SHORT
            )

            if signal == "BUY" and htf_ok_long and strategy.can_enter_long and long_funding_ok:
                atr   = strategy.current_atr
                entry = close * (1 + slip)
                qty   = risk_manager.calculate_position_size(
                    balance, entry, atr, strategy.vol_multiplier, strategy.current_adx
                )
                if qty > 0:
                    balance -= entry * qty * FEE
                    entry_open_time = open_time
                    current = Trade(
                        entry_time  = _fmt(open_time),
                        entry_price = round(entry, 2),
                        qty         = qty,
                        initial_qty = qty,
                        sl          = risk_manager.get_stop_loss(entry, "BUY", atr, strategy.current_adx),
                        tp          = risk_manager.get_take_profit(entry, "BUY", atr),
                        atr         = round(atr, 2) if atr else 0,
                        side        = "LONG",
                    )
                    in_position = True

            elif signal == "SELL" and htf_strategy.is_bearish and strategy.can_enter_short and short_funding_ok:
                atr   = strategy.current_atr
                entry = close * (1 - slip)
                qty   = risk_manager.calculate_position_size(
                    balance, entry, atr, strategy.vol_multiplier, strategy.current_adx
                )
                if qty > 0:
                    balance -= entry * qty * FEE
                    entry_open_time = open_time
                    current = Trade(
                        entry_time  = _fmt(open_time),
                        entry_price = round(entry, 2),
                        qty         = qty,
                        initial_qty = qty,
                        sl          = risk_manager.get_stop_loss(entry, "SELL", atr, strategy.current_adx),
                        tp          = risk_manager.get_take_profit(entry, "SELL", atr),
                        atr         = round(atr, 2) if atr else 0,
                        side        = "SHORT",
                    )
                    in_position = True

        balance_history.append(balance)

    if in_position and current:
        slip = dynamic_slippage(strategy.current_atr, strategy.avg_atr)
        last_open_time = int(klines_1h[-1][0])
        last_close = float(klines_1h[-1][4])
        is_long = current.side == "LONG"
        exit_price = last_close * (1 - slip) if is_long else last_close * (1 + slip)
        _close_trade(last_open_time, exit_price, "FIN")

    time_in_market_pct = (candles_in_position / candles_ready * 100) if candles_ready else 0.0
    return trades, balance_history, time_in_market_pct


async def main(months: int, initial_balance: float, csv_path, funding_filter: Optional[bool]):
    print(f"\nDescargando velas 1H ({config.SYMBOL}, {months} meses)...")
    klines_1h = await fetch_klines(config.INTERVAL, months)
    print(f"  -> {len(klines_1h):,} velas")

    print("Descargando velas 4H (filtro HTF)...")
    klines_4h = await fetch_klines(config.HTF_INTERVAL, months)
    print(f"  -> {len(klines_4h):,} velas")

    print("Descargando historial real de funding rate...")
    funding_events = await fetch_funding_history(config.SYMBOL, months)
    print(f"  -> {len(funding_events):,} eventos de funding")

    filter_on = config.FUNDING_FILTER_ENABLED if funding_filter is None else funding_filter
    print(f"\nEjecutando simulación (long + short, funding real, filtro {'ON' if filter_on else 'OFF'})...\n")
    trades, balance_history, time_in_market_pct = run_backtest_futures(
        klines_1h, klines_4h, initial_balance, funding_events, filter_on
    )

    longs  = sum(1 for t in trades if t.side == "LONG")
    shorts = sum(1 for t in trades if t.side == "SHORT")
    total_funding = sum(t.funding_cost for t in trades)
    print(f"  Operaciones LONG: {longs}  |  Operaciones SHORT: {shorts}")
    print(f"  Costo neto de funding (real, histórico): {total_funding:.2f} USDT\n")

    metrics = compute_metrics(trades, balance_history, initial_balance, months, time_in_market_pct)
    print_report(metrics, initial_balance, months)

    if csv_path and trades:
        save_csv(trades, csv_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Futuros con shorts y funding real")
    parser.add_argument("--months",  type=int,   default=36,     help="Meses de historial (default: 36)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    parser.add_argument("--csv",     type=str,   default=None,   help="Exportar operaciones a CSV")
    parser.add_argument("--no-funding-filter", action="store_true", help="Desactiva el filtro de funding para comparar")
    args = parser.parse_args()

    funding_filter = False if args.no_funding_filter else None
    asyncio.run(main(args.months, args.balance, args.csv, funding_filter))
