"""
Backtest Híbrido — EMA Crossover (tendencia) + Mean-Reversion (rango).

Motivación: el backtest/walk-forward de la estrategia de tendencia (backtest.py) mostró
Sharpe débil y baja frecuencia de operación — capturaba bien las tendencias grandes pero
no operaba (ni ganaba ni perdía) en los periodos laterales, que son la mayoría del tiempo
según el filtro ER (Efficiency Ratio).

Este motor añade una señal de mean-reversion (RSI extremo) que SOLO se activa cuando el
régimen es lateral/choppy (ER < REGIME_ER_MIN, lo opuesto de is_trending) — son mutuamente
excluyentes por construcción, nunca compiten por la misma vela ni símbolo. Mismo principio
de gestión de riesgo (ATR-sizing), pero con SL/TP más ajustados (objetivo: volver a la media,
no dejar correr el ganador como en tendencia).

Uso:
    python backtest_hybrid.py                     # 36 meses, 1000 USDT
    python backtest_hybrid.py --months 60 --csv hybrid.csv
"""

import asyncio
import argparse
import sys
from typing import List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import config
from strategy import EMAStrategy
from risk_manager import RiskManager
from backtest import (
    Trade, FEE, dynamic_slippage, _fill_trade, _fmt,
    fetch_klines, compute_metrics, print_report, save_csv,
)


def run_backtest_hybrid(
    klines_1h: list,
    klines_4h: list,
    initial_balance: float,
) -> tuple[List[Trade], List[float], float]:

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
    in_position               = False
    htf_ptr                   = 0
    candles_in_position       = 0
    candles_ready             = 0

    def _mr_size(balance: float, atr: float, vol_mult: float) -> float:
        """Sizing específico de MR: usa MR_SL_ATR_MULTIPLIER como distancia de stop,
        no el multiplicador de tendencia (serían inconsistentes entre sí)."""
        risk_amount = balance * config.RISK_PER_TRADE * vol_mult
        stop_distance = atr * config.MR_SL_ATR_MULTIPLIER
        if stop_distance <= 0:
            return 0.0
        qty = round(risk_amount / stop_distance, 5)
        return qty if qty >= config.MIN_QUANTITY else 0.0

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

        slip = dynamic_slippage(strategy.current_atr, strategy.avg_atr)

        # ── Gestión de posición existente ──────────────────────────────
        if in_position and current:
            is_long = current.side == "LONG"

            if current.engine == "TREND":
                if (config.SCALE_OUT_R > 0 and not current.scale_out_done
                        and current.atr > 0 and current.qty > 0):
                    so_mult = risk_manager.get_trailing_multiplier(strategy.current_adx)
                    so_price = current.entry_price + config.SCALE_OUT_R * so_mult * current.atr
                    if high >= so_price:
                        partial_qty = round(current.qty * config.SCALE_OUT_RATIO, 5)
                        if partial_qty >= config.MIN_QUANTITY:
                            fee_so = so_price * partial_qty * FEE
                            pnl_so = (so_price - current.entry_price) * partial_qty - fee_so
                            balance += pnl_so
                            current.scale_out_pnl  = round(pnl_so, 4)
                            current.qty            = round(current.qty - partial_qty, 5)
                            current.sl             = current.entry_price
                            current.scale_out_done = True

                hit_sl = low <= current.sl
                hit_tp = high >= current.tp
            else:  # MR: sin trailing ni scale-out, SL/TP fijos según side
                hit_sl = (low <= current.sl) if is_long else (high >= current.sl)
                hit_tp = (high >= current.tp) if is_long else (low <= current.tp)

            if hit_sl or hit_tp:
                if hit_sl and hit_tp:
                    exit_price, reason = current.sl, "SL"
                elif hit_tp:
                    exit_price, reason = current.tp, "TP"
                else:
                    exit_price, reason = current.sl, "SL"

                fee = exit_price * current.qty * FEE
                if is_long:
                    pnl = (exit_price - current.entry_price) * current.qty - fee
                else:
                    pnl = (current.entry_price - exit_price) * current.qty - fee
                balance += pnl
                _fill_trade(current, open_time, exit_price, reason, pnl)
                trades.append(current)
                in_position, current = False, None
                balance_history.append(balance)
                continue

            if current.engine == "TREND" and config.TRAILING_STOP and strategy.current_atr:
                trail_mult = risk_manager.get_trailing_multiplier(strategy.current_adx)
                new_sl = round(close - strategy.current_atr * trail_mult, 2)
                if new_sl > current.sl:
                    current.sl = new_sl

        # ── Señales ──────────────────────────────────────────────────────
        signal = strategy.get_signal()

        if current and current.engine == "TREND" and signal == "SELL" and in_position:
            exit_price = close * (1 - slip)
            fee = exit_price * current.qty * FEE
            pnl = (exit_price - current.entry_price) * current.qty - fee
            balance += pnl
            _fill_trade(current, open_time, exit_price, "SIGNAL", pnl)
            trades.append(current)
            in_position, current = False, None

        if not in_position:
            htf_ok = htf_strategy.is_bullish or config.ALLOW_BUY_IN_BEARISH_HTF
            atr = strategy.current_atr
            adx = strategy.current_adx
            vol_mult = strategy.vol_multiplier

            if config.MR_ENABLED and strategy.mr_long_signal and atr:
                entry = close * (1 + slip)
                qty = _mr_size(balance, atr, vol_mult)
                if qty > 0:
                    balance -= entry * qty * FEE
                    current = Trade(
                        entry_time=_fmt(open_time), entry_price=round(entry, 2), qty=qty,
                        initial_qty=qty,
                        sl=round(entry - config.MR_SL_ATR_MULTIPLIER * atr, 2),
                        tp=round(entry + config.MR_TP_ATR_MULTIPLIER * atr, 2),
                        atr=round(atr, 2), side="LONG", engine="MR",
                    )
                    in_position = True

            elif config.MR_ENABLED and strategy.mr_short_signal and atr:
                entry = close * (1 - slip)
                qty = _mr_size(balance, atr, vol_mult)
                if qty > 0:
                    balance -= entry * qty * FEE
                    current = Trade(
                        entry_time=_fmt(open_time), entry_price=round(entry, 2), qty=qty,
                        initial_qty=qty,
                        sl=round(entry + config.MR_SL_ATR_MULTIPLIER * atr, 2),
                        tp=round(entry - config.MR_TP_ATR_MULTIPLIER * atr, 2),
                        atr=round(atr, 2), side="SHORT", engine="MR",
                    )
                    in_position = True

            elif signal == "BUY" and htf_ok and strategy.can_enter_long:
                entry = close * (1 + slip)
                qty = risk_manager.calculate_position_size(balance, entry, atr, vol_mult, adx)
                if qty > 0:
                    balance -= entry * qty * FEE
                    current = Trade(
                        entry_time=_fmt(open_time), entry_price=round(entry, 2), qty=qty,
                        initial_qty=qty,
                        sl=risk_manager.get_stop_loss(entry, "BUY", atr, adx),
                        tp=risk_manager.get_take_profit(entry, "BUY", atr),
                        atr=round(atr, 2) if atr else 0, side="LONG", engine="TREND",
                    )
                    in_position = True

        balance_history.append(balance)

    if in_position and current:
        is_long = current.side == "LONG"
        slip = dynamic_slippage(strategy.current_atr, strategy.avg_atr)
        last_close = float(klines_1h[-1][4])
        exit_price = last_close * (1 - slip) if is_long else last_close * (1 + slip)
        fee = exit_price * current.qty * FEE
        if is_long:
            pnl = (exit_price - current.entry_price) * current.qty - fee
        else:
            pnl = (current.entry_price - exit_price) * current.qty - fee
        balance += pnl
        _fill_trade(current, int(klines_1h[-1][0]), exit_price, "FIN", pnl)
        trades.append(current)

    time_in_market_pct = (candles_in_position / candles_ready * 100) if candles_ready else 0.0
    return trades, balance_history, time_in_market_pct


async def main(months: int, initial_balance: float, csv_path):
    print(f"\nDescargando velas 1H ({config.SYMBOL}, {months} meses)...")
    klines_1h = await fetch_klines(config.INTERVAL, months)
    print(f"  -> {len(klines_1h):,} velas")
    print("Descargando velas 4H (filtro HTF)...")
    klines_4h = await fetch_klines(config.HTF_INTERVAL, months)
    print(f"  -> {len(klines_4h):,} velas")

    print("Ejecutando simulación (tendencia + mean-reversion)...\n")
    trades, balance_history, tim = run_backtest_hybrid(klines_1h, klines_4h, initial_balance)

    trend_trades = [t for t in trades if t.engine == "TREND"]
    mr_trades    = [t for t in trades if t.engine == "MR"]
    print(f"  Operaciones TREND: {len(trend_trades)}  |  Operaciones MR: {len(mr_trades)}")
    for label, ts in (("TREND", trend_trades), ("MR", mr_trades)):
        if not ts:
            continue
        wins = sum(1 for t in ts if t.pnl_usdt > 0)
        pnl = sum(t.pnl_usdt for t in ts)
        print(f"    {label:<6} win rate {wins/len(ts)*100:5.1f}%  PnL total {pnl:+8.2f} USDT")
    print()

    metrics = compute_metrics(trades, balance_history, initial_balance, months, tim)
    print_report(metrics, initial_balance, months)

    if csv_path and trades:
        save_csv(trades, csv_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest híbrido tendencia + mean-reversion")
    parser.add_argument("--months",  type=int,   default=36,     help="Meses de historial (default: 36)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    parser.add_argument("--csv",     type=str,   default=None,   help="Exportar operaciones a CSV")
    args = parser.parse_args()

    asyncio.run(main(args.months, args.balance, args.csv))
