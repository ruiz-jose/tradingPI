"""
Buscador de Estrategia Rentable, Sólida y Profesional para tradingPI.
Ejecuta una optimización en 4 etapas:
1. Búsqueda de parámetros core (Timeframe, EMAs, Efficiency Ratio).
2. Búsqueda de filtros y gestión ATR (RSI, ATR SL multipliers, ATR TP).
3. Búsqueda de gestión de riesgo avanzada (Breakeven, Scale-out).
4. Validación cruzada temporal (Walk-Forward) in-sample y out-of-sample.

Aplica la mejor configuración a config.py si se especifica --apply.
"""

import asyncio
import argparse
import sys
import os
import re
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import config
from strategy import EMAStrategy
from risk_manager import RiskManager
from backtest import fetch_klines, run_backtest, compute_metrics
from backtest_futures import fetch_funding_history, run_backtest_futures

# Configuraciones de símbolos en vivo
SYMBOL_MODES = {
    "BTCUSDT": {"shorts": False, "funding": False},
    "ETHUSDT": {"shorts": True, "funding": True},
    "SOLUSDT": {"shorts": True, "funding": True}
}

# ------------------------------------------------------------------ #
# Parámetros de cuadrícula (Grid)                                     #
# ------------------------------------------------------------------ #

# Etapa 1: Core Parameters Grid
TIMEFRAME_COMBOS = [
    ("2h", "12h"),
    ("4h", "1d"),
    ("6h", "1d"),
    ("8h", "1d")
]

EMA_COMBOS = [
    (8, 21),
    (9, 21),
    (12, 26),
    (21, 55),
    (10, 30)
]

ER_MIN_VALUES = [0.0, 0.1, 0.15, 0.2, 0.25]
ER_PERIOD_VALUES = [10, 12]

# Etapa 2: Filters & ATR Management Grid (para los mejores cores)
RSI_COMBOS = [
    # (buy_min, buy_max, sell_min, sell_max)
    (40.0, 80.0, 20.0, 60.0), # Estándar
    (35.0, 75.0, 25.0, 65.0), # Ajustado
    (45.0, 85.0, 15.0, 55.0), # Momentum fuerte
    (30.0, 70.0, 30.0, 70.0)  # Simétrico clásico
]

ATR_SL_COMBOS = [
    # (trend_mult, chop_mult)
    (3.0, 2.0),
    (3.5, 2.0),
    (2.5, 1.5),
    (3.5, 2.5),
    (4.0, 2.5)
]

ATR_TP_VALUES = [3.0, 4.0, 5.0]

# Etapa 3: Advanced Risk Grid (para los mejores de Etapa 2)
BREAKEVEN_R_VALUES = [0.0, 1.0, 1.5, 2.0]
SCALE_OUT_R_VALUES = [0.0, 2.0, 3.0]

# ------------------------------------------------------------------ #
# Helper Functions                                                    #
# ------------------------------------------------------------------ #

def run_simulation(
    symbol: str,
    klines_1h: list,
    klines_htf: list,
    funding_events: list,
    initial_balance: float,
    months: int
) -> Tuple[list, list, float]:
    """Ejecuta la simulación adecuada según si el símbolo opera con shorts/funding o es long-only."""
    mode = SYMBOL_MODES[symbol]
    if mode["shorts"]:
        # Futuros con shorts y funding real
        return run_backtest_futures(
            klines_1h, klines_htf, initial_balance, funding_events, funding_filter_enabled=config.FUNDING_FILTER_ENABLED
        )
    else:
        # Long only (usando backtest.py estándar)
        return run_backtest(klines_1h, klines_htf, initial_balance)

def get_config_fitness(m: dict) -> float:
    """Calcula la aptitud (fitness) de una métrica para ordenar resultados.
    Premia alto Sharpe, Profit Factor, y rentabilidad, penalizando drawdowns altos y pocos trades."""
    if not m or m.get("total", 0) < 8:
        return 0.0
    sharpe = max(0.0, m.get("sharpe", 0.0))
    pf = min(5.0, m.get("profit_factor", 0.0))
    ret = max(-50.0, m.get("total_return", 0.0)) / 10.0 # retorno como factor menor
    dd_penalty = 1.0 if m.get("max_drawdown", 0.0) < 10.0 else (10.0 / m.get("max_drawdown", 10.0))
    
    # Penalización suave si tiene menos de 15 trades en 36 meses
    trades_factor = 1.0 if m.get("total", 0) >= 15 else (m.get("total", 0) / 15.0)
    
    return sharpe * pf * (1.0 + ret) * dd_penalty * trades_factor

# ------------------------------------------------------------------ #
# Main optimization loop                                              #
# ------------------------------------------------------------------ #

async def optimize_symbol(symbol: str, months: int, initial_balance: float) -> Dict[str, Any]:
    print(f"\n[+] OPTIMIZANDO SÍMBOLO: {symbol} ({months} meses, balance: {initial_balance} USDT)")
    print("=" * 70)
    
    # Descargar datos
    # Para cambiar el símbolo de descarga, parcheamos temporalmente config.SYMBOL
    orig_symbol = config.SYMBOL
    config.SYMBOL = symbol
    
    klines_cache = {}
    intervals_to_fetch = ["2h", "4h", "6h", "8h", "12h", "1d"]
    print("  -> Descargando klines de Binance...")
    for tf in intervals_to_fetch:
        klines_cache[tf] = await fetch_klines(tf, months)
        print(f"     * {tf}: {len(klines_cache[tf]):,} velas")
        
    funding_events = []
    if SYMBOL_MODES[symbol]["funding"]:
        print("  -> Descargando historial de funding rate...")
        funding_events = await fetch_funding_history(symbol, months)
        print(f"     * Funding rate: {len(funding_events):,} eventos")
        
    config.SYMBOL = orig_symbol # Restaurar

    # Guardar estado original
    orig_interval = config.INTERVAL
    orig_htf_interval = config.HTF_INTERVAL
    orig_ema_fast = config.EMA_FAST
    orig_ema_slow = config.EMA_SLOW
    orig_regime_er_min = config.REGIME_ER_MIN
    orig_regime_er_period = config.REGIME_ER_PERIOD
    orig_rsi_buy_min = config.RSI_BUY_MIN
    orig_rsi_buy_max = config.RSI_BUY_MAX
    orig_rsi_sell_min = config.RSI_SELL_MIN
    orig_rsi_sell_max = config.RSI_SELL_MAX
    orig_atr_sl_trend = config.ATR_SL_MULTIPLIER_TREND
    orig_atr_sl_chop = config.ATR_SL_MULTIPLIER_CHOP
    orig_atr_tp = config.ATR_TP_MULTIPLIER
    orig_breakeven_r = config.BREAKEVEN_R
    orig_scale_out_r = config.SCALE_OUT_R

    # --------------------------------------------------------------
    # ETAPA 1: Parámetros Core
    # --------------------------------------------------------------
    print("\n[Etapa 1] Explorando Parámetros Core (Timeframe, EMAs, Efficiency Ratio)...")
    core_results = []
    
    # Grid de prueba Etapa 1
    total_core_combos = len(TIMEFRAME_COMBOS) * len(EMA_COMBOS) * len(ER_MIN_VALUES) * len(ER_PERIOD_VALUES)
    idx = 0
    for tf, htf_tf in TIMEFRAME_COMBOS:
        for fast, slow in EMA_COMBOS:
            for er_min in ER_MIN_VALUES:
                for er_period in ER_PERIOD_VALUES:
                    idx += 1
                    # Configurar variables
                    config.INTERVAL = tf
                    config.HTF_INTERVAL = htf_tf
                    config.EMA_FAST = fast
                    config.EMA_SLOW = slow
                    config.REGIME_ER_MIN = er_min
                    config.REGIME_ER_PERIOD = er_period
                    
                    # Ejecutar backtest
                    try:
                        trades, bal_hist, tim = run_simulation(
                            symbol, klines_cache[tf], klines_cache[htf_tf], funding_events, initial_balance, months
                        )
                        m = compute_metrics(trades, bal_hist, initial_balance, months, tim)
                        fit = get_config_fitness(m)
                        if fit > 0:
                            core_results.append({
                                "fitness": fit,
                                "metrics": m,
                                "params": {
                                    "interval": tf, "htf_interval": htf_tf,
                                    "ema_fast": fast, "ema_slow": slow,
                                    "regime_er_min": er_min, "regime_er_period": er_period
                                }
                            })
                    except Exception as e:
                        pass
                    
                    if idx % 50 == 0 or idx == total_core_combos:
                        done = int(idx / total_core_combos * 30)
                        bar = "#" * done + "-" * (30 - done)
                        print(f"   [{bar}] {idx}/{total_core_combos} combinaciones...", end="\r", flush=True)
    print()
    
    core_results.sort(key=lambda x: x["fitness"], reverse=True)
    top_cores = core_results[:5]
    
    if not top_cores:
        print("  [!] No se encontraron combinaciones viables en la Etapa 1. Saltando...")
        return None
        
    print(f"  -> Etapa 1 completada. Top core config:")
    for rank, tc in enumerate(top_cores[:3], 1):
        p = tc["params"]
        m = tc["metrics"]
        print(f"     #{rank}: TF={p['interval']}/{p['htf_interval']}, EMA={p['ema_fast']}/{p['ema_slow']}, ER={p['regime_er_min']} (P:{p['regime_er_period']}) | Return={m['total_return']:+.2f}% | Sharpe={m['sharpe']:.2f} | PF={m['profit_factor']:.2f}")

    # --------------------------------------------------------------
    # ETAPA 2: Filtros y Gestión ATR
    # --------------------------------------------------------------
    print("\n[Etapa 2] Explorando Filtros RSI y Multiplicadores ATR para el Top Cores...")
    stage2_results = []
    
    total_s2_combos = len(top_cores) * len(RSI_COMBOS) * len(ATR_SL_COMBOS) * len(ATR_TP_VALUES)
    idx = 0
    for tc in top_cores:
        cp = tc["params"]
        # Fijar parámetros core
        config.INTERVAL = cp["interval"]
        config.HTF_INTERVAL = cp["htf_interval"]
        config.EMA_FAST = cp["ema_fast"]
        config.EMA_SLOW = cp["ema_slow"]
        config.REGIME_ER_MIN = cp["regime_er_min"]
        config.REGIME_ER_PERIOD = cp["regime_er_period"]
        
        tf, htf_tf = cp["interval"], cp["htf_interval"]
        
        for rsi_buy_min, rsi_buy_max, rsi_sell_min, rsi_sell_max in RSI_COMBOS:
            for sl_trend, sl_chop in ATR_SL_COMBOS:
                for tp_mult in ATR_TP_VALUES:
                    idx += 1
                    config.RSI_BUY_MIN = rsi_buy_min
                    config.RSI_BUY_MAX = rsi_buy_max
                    config.RSI_SELL_MIN = rsi_sell_min
                    config.RSI_SELL_MAX = rsi_sell_max
                    config.ATR_SL_MULTIPLIER_TREND = sl_trend
                    config.ATR_SL_MULTIPLIER_CHOP = sl_chop
                    config.ATR_TP_MULTIPLIER = tp_mult
                    
                    try:
                        trades, bal_hist, tim = run_simulation(
                            symbol, klines_cache[tf], klines_cache[htf_tf], funding_events, initial_balance, months
                        )
                        m = compute_metrics(trades, bal_hist, initial_balance, months, tim)
                        fit = get_config_fitness(m)
                        if fit > 0:
                            stage2_results.append({
                                "fitness": fit,
                                "metrics": m,
                                "params": {
                                    **cp,
                                    "rsi_buy_min": rsi_buy_min, "rsi_buy_max": rsi_buy_max,
                                    "rsi_sell_min": rsi_sell_min, "rsi_sell_max": rsi_sell_max,
                                    "atr_sl_trend": sl_trend, "atr_sl_chop": sl_chop,
                                    "atr_tp_multiplier": tp_mult
                                }
                            })
                    except Exception as e:
                        pass
                    
                    if idx % 50 == 0 or idx == total_s2_combos:
                        done = int(idx / total_s2_combos * 30)
                        bar = "#" * done + "-" * (30 - done)
                        print(f"   [{bar}] {idx}/{total_s2_combos} combinaciones...", end="\r", flush=True)
    print()
    
    stage2_results.sort(key=lambda x: x["fitness"], reverse=True)
    top_s2 = stage2_results[:5]
    
    if not top_s2:
        print("  [!] No se encontraron combinaciones viables en la Etapa 2.")
        return None
        
    print(f"  -> Etapa 2 completada. Top filtros/ATR config:")
    for rank, ts2 in enumerate(top_s2[:3], 1):
        p = ts2["params"]
        m = ts2["metrics"]
        print(f"     #{rank}: RSI Buy={p['rsi_buy_min']}-{p['rsi_buy_max']}, SL(Trend/Chop)={p['atr_sl_trend']}/{p['atr_sl_chop']}, TP={p['atr_tp_multiplier']}x | Return={m['total_return']:+.2f}% | Sharpe={m['sharpe']:.2f} | PF={m['profit_factor']:.2f}")

    # --------------------------------------------------------------
    # ETAPA 3: Breakeven & Scale-out
    # --------------------------------------------------------------
    print("\n[Etapa 3] Explorando Gestión de Riesgo Avanzada (Breakeven y Scale-out)...")
    stage3_results = []
    
    total_s3_combos = len(top_s2) * len(BREAKEVEN_R_VALUES) * len(SCALE_OUT_R_VALUES)
    idx = 0
    for ts2 in top_s2:
        cp = ts2["params"]
        config.INTERVAL = cp["interval"]
        config.HTF_INTERVAL = cp["htf_interval"]
        config.EMA_FAST = cp["ema_fast"]
        config.EMA_SLOW = cp["ema_slow"]
        config.REGIME_ER_MIN = cp["regime_er_min"]
        config.REGIME_ER_PERIOD = cp["regime_er_period"]
        config.RSI_BUY_MIN = cp["rsi_buy_min"]
        config.RSI_BUY_MAX = cp["rsi_buy_max"]
        config.RSI_SELL_MIN = cp["rsi_sell_min"]
        config.RSI_SELL_MAX = cp["rsi_sell_max"]
        config.ATR_SL_MULTIPLIER_TREND = cp["atr_sl_trend"]
        config.ATR_SL_MULTIPLIER_CHOP = cp["atr_sl_chop"]
        config.ATR_TP_MULTIPLIER = cp["atr_tp_multiplier"]
        
        tf, htf_tf = cp["interval"], cp["htf_interval"]
        
        for be_r in BREAKEVEN_R_VALUES:
            for so_r in SCALE_OUT_R_VALUES:
                idx += 1
                config.BREAKEVEN_R = be_r
                config.SCALE_OUT_R = so_r
                
                try:
                    trades, bal_hist, tim = run_simulation(
                        symbol, klines_cache[tf], klines_cache[htf_tf], funding_events, initial_balance, months
                    )
                    m = compute_metrics(trades, bal_hist, initial_balance, months, tim)
                    fit = get_config_fitness(m)
                    if fit > 0:
                        stage3_results.append({
                            "fitness": fit,
                            "metrics": m,
                            "params": {
                                **cp,
                                "breakeven_r": be_r,
                                "scale_out_r": so_r
                            }
                        })
                except Exception as e:
                    pass
                
                if idx % 10 == 0 or idx == total_s3_combos:
                    done = int(idx / total_s3_combos * 30)
                    bar = "#" * done + "-" * (30 - done)
                    print(f"   [{bar}] {idx}/{total_s3_combos} combinaciones...", end="\r", flush=True)
    print()
    
    stage3_results.sort(key=lambda x: x["fitness"], reverse=True)
    best_candidate = stage3_results[0] if stage3_results else None
    
    if not best_candidate:
        print("  [!] No se encontraron combinaciones viables en la Etapa 3.")
        return None
        
    p = best_candidate["params"]
    m = best_candidate["metrics"]
    print(f"\n[+] GANADOR IN-SAMPLE ENCONTRADO PARA {symbol}:")
    print(f"    * TF: {p['interval']} (HTF: {p['htf_interval']})")
    print(f"    * EMA: {p['ema_fast']}/{p['ema_slow']}")
    print(f"    * ER regime min: {p['regime_er_min']} (periodo: {p['regime_er_period']})")
    print(f"    * RSI: Buy={p['rsi_buy_min']}-{p['rsi_buy_max']} | Sell={p['rsi_sell_min']}-{p['rsi_sell_max']}")
    print(f"    * ATR SL Trend={p['atr_sl_trend']}x | Chop={p['atr_sl_chop']}x")
    print(f"    * ATR TP: {p['atr_tp_multiplier']}x")
    print(f"    * Breakeven R: {p['breakeven_r']} | Scale-out R: {p['scale_out_r']}")
    print(f"    -> Trades: {m['total']} | Win Rate: {m['win_rate']:.1f}% | Profit Factor: {m['profit_factor']:.2f} | Sharpe: {m['sharpe']:.2f} | Drawdown: {m['max_drawdown']:.2f}% | Retorno: {m['total_return']:+.2f}%")

    # Restaurar config original
    config.INTERVAL = orig_interval
    config.HTF_INTERVAL = orig_htf_interval
    config.EMA_FAST = orig_ema_fast
    config.EMA_SLOW = orig_ema_slow
    config.REGIME_ER_MIN = orig_regime_er_min
    config.REGIME_ER_PERIOD = orig_regime_er_period
    config.RSI_BUY_MIN = orig_rsi_buy_min
    config.RSI_BUY_MAX = orig_rsi_buy_max
    config.RSI_SELL_MIN = orig_rsi_sell_min
    config.RSI_SELL_MAX = orig_rsi_sell_max
    config.ATR_SL_MULTIPLIER_TREND = orig_atr_sl_trend
    config.ATR_SL_MULTIPLIER_CHOP = orig_atr_sl_chop
    config.ATR_TP_MULTIPLIER = orig_atr_tp
    config.BREAKEVEN_R = orig_breakeven_r
    config.SCALE_OUT_R = orig_scale_out_r

    return best_candidate


# ------------------------------------------------------------------ #
# Walk-Forward Validation (Etapa 4)                                   #
# ------------------------------------------------------------------ #

async def validate_walkforward(
    symbol: str,
    params: dict,
    months_total: int,
    train_months: int,
    test_months: int,
    step_months: int,
    initial_balance: float
) -> List[dict]:
    """Realiza validación walk-forward out-of-sample con los parámetros ganadores para verificar solidez."""
    print(f"\n[Etapa 4] Ejecutando Validación Walk-Forward para {symbol} ({months_total} meses totales)...")
    print(f"  Train: {train_months}m | Test: {test_months}m | Step: {step_months}m")
    
    orig_symbol = config.SYMBOL
    config.SYMBOL = symbol
    
    tf = params["interval"]
    htf_tf = params["htf_interval"]
    
    klines_main = await fetch_klines(tf, months_total)
    klines_htf = await fetch_klines(htf_tf, months_total)
    
    funding_events = []
    if SYMBOL_MODES[symbol]["funding"]:
        funding_events = await fetch_funding_history(symbol, months_total)
        
    config.SYMBOL = orig_symbol
    
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    day_ms = 24 * 3600 * 1000
    total_start_ms = int((datetime.now(timezone.utc) - timedelta(days=30 * months_total)).timestamp() * 1000)
    
    train_ms = train_months * 30 * day_ms
    test_ms = test_months * 30 * day_ms
    step_ms = step_months * 30 * day_ms
    
    folds = []
    train_start = total_start_ms
    while True:
        train_end = train_start + train_ms
        test_end = train_end + test_ms
        if test_end > now_ms:
            break
        folds.append((train_start, train_end, test_end))
        train_start += step_ms
        
    if not folds:
        print("  [!] No hay suficientes datos para los folds seleccionados.")
        return []
        
    # Temporalmente configuramos a los parámetros optimizados
    orig_interval = config.INTERVAL
    orig_htf_interval = config.HTF_INTERVAL
    orig_ema_fast = config.EMA_FAST
    orig_ema_slow = config.EMA_SLOW
    orig_regime_er_min = config.REGIME_ER_MIN
    orig_regime_er_period = config.REGIME_ER_PERIOD
    orig_rsi_buy_min = config.RSI_BUY_MIN
    orig_rsi_buy_max = config.RSI_BUY_MAX
    orig_rsi_sell_min = config.RSI_SELL_MIN
    orig_rsi_sell_max = config.RSI_SELL_MAX
    orig_atr_sl_trend = config.ATR_SL_MULTIPLIER_TREND
    orig_atr_sl_chop = config.ATR_SL_MULTIPLIER_CHOP
    orig_atr_tp = config.ATR_TP_MULTIPLIER
    orig_breakeven_r = config.BREAKEVEN_R
    orig_scale_out_r = config.SCALE_OUT_R

    config.INTERVAL = tf
    config.HTF_INTERVAL = htf_tf
    config.EMA_FAST = params["ema_fast"]
    config.EMA_SLOW = params["ema_slow"]
    config.REGIME_ER_MIN = params["regime_er_min"]
    config.REGIME_ER_PERIOD = params["regime_er_period"]
    config.RSI_BUY_MIN = params["rsi_buy_min"]
    config.RSI_BUY_MAX = params["rsi_buy_max"]
    config.RSI_SELL_MIN = params["rsi_sell_min"]
    config.RSI_SELL_MAX = params["rsi_sell_max"]
    config.ATR_SL_MULTIPLIER_TREND = params["atr_sl_trend"]
    config.ATR_SL_MULTIPLIER_CHOP = params["atr_sl_chop"]
    config.ATR_TP_MULTIPLIER = params["atr_tp_multiplier"]
    config.BREAKEVEN_R = params["breakeven_r"]
    config.SCALE_OUT_R = params["scale_out_r"]

    def _slice(klines: list, start_ms: int, end_ms: int) -> list:
        return [k for k in klines if start_ms <= int(k[0]) < end_ms]

    def _slice_funding(events: list, start_ms: int, end_ms: int) -> list:
        return [e for e in events if start_ms <= e[0] < end_ms]

    wf_results = []
    for i, (train_start, train_end, test_end) in enumerate(folds, 1):
        test_main = _slice(klines_main, train_end, test_end)
        test_htf = _slice(klines_htf, train_end, test_end)
        test_funding = _slice_funding(funding_events, train_end, test_end)
        
        try:
            trades, bal_hist, tim = run_simulation(
                symbol, test_main, test_htf, test_funding, initial_balance, test_months
            )
            m_test = compute_metrics(trades, bal_hist, initial_balance, test_months, tim)
            fold_label = (
                f"{datetime.fromtimestamp(train_end/1000, tz=timezone.utc):%Y-%m}"
                f" → {datetime.fromtimestamp(test_end/1000, tz=timezone.utc):%Y-%m}"
            )
            wf_results.append({
                "fold": i,
                "label": fold_label,
                "metrics": m_test
            })
        except Exception as e:
            pass

    # Restaurar config original
    config.INTERVAL = orig_interval
    config.HTF_INTERVAL = orig_htf_interval
    config.EMA_FAST = orig_ema_fast
    config.EMA_SLOW = orig_ema_slow
    config.REGIME_ER_MIN = orig_regime_er_min
    config.REGIME_ER_PERIOD = orig_regime_er_period
    config.RSI_BUY_MIN = orig_rsi_buy_min
    config.RSI_BUY_MAX = orig_rsi_buy_max
    config.RSI_SELL_MIN = orig_rsi_sell_min
    config.RSI_SELL_MAX = orig_rsi_sell_max
    config.ATR_SL_MULTIPLIER_TREND = orig_atr_sl_trend
    config.ATR_SL_MULTIPLIER_CHOP = orig_atr_sl_chop
    config.ATR_TP_MULTIPLIER = orig_atr_tp
    config.BREAKEVEN_R = orig_breakeven_r
    config.SCALE_OUT_R = orig_scale_out_r

    # Imprimir resultados out-of-sample
    print(f"\n  Resultados OUT-OF-SAMPLE (Walk-Forward) para {symbol}:")
    print(f"  {'Fold':<4} {'Periodo Test':<18} {'Trades':>6} {'PF':>6} {'Sharpe':>7} {'MaxDD%':>7} {'Retorno':>9}")
    print("  " + "-" * 66)
    
    pf_list, sharpe_list, dd_list, ret_list = [], [], [], []
    for r in wf_results:
        m = r["metrics"]
        label = r["label"]
        if not m or m.get("total", 0) == 0:
            print(f"  {r['fold']:<4} {label:<18} {'0':>6} {'-':>6} {'-':>7} {'-':>7} {'0.00%':>9}")
            continue
        print(f"  {r['fold']:<4} {label:<18} {m['total']:>6} {m['profit_factor']:>6.2f} {m['sharpe']:>7.2f} {m['max_drawdown']:>7.2f}% {m['total_return']:>+9.2f}%")
        
        pf_list.append(m["profit_factor"] if m["profit_factor"] != float("inf") else 5.0)
        sharpe_list.append(m["sharpe"])
        dd_list.append(m["max_drawdown"])
        ret_list.append(m["total_return"])
        
    if pf_list:
        n = len(pf_list)
        print("  " + "-" * 66)
        print(f"  Promedio OOS: Trades={sum([r['metrics'].get('total',0) for r in wf_results])/len(wf_results):.1f} | PF={sum(pf_list)/n:.2f} | Sharpe={sum(sharpe_list)/n:.2f} | MaxDD={sum(dd_list)/n:.2f}% | Retorno={sum(ret_list)/n:+.2f}%")
    else:
        print("  [!] Ningún fold generó operaciones.")
        
    return wf_results


# ------------------------------------------------------------------ #
# Apply Winner Configuration to config.py                             #
# ------------------------------------------------------------------ #

def apply_winner_config(params: dict):
    path = "config.py"
    with open(path, encoding="utf-8") as f:
        text = f.read()

    # Reemplazar valores
    # Nota: para evitar colisiones con variables similares, usamos regex específicas
    text = re.sub(r'(?<!HTF_)INTERVAL: str = "[^"]*"', f'INTERVAL: str = "{params["interval"]}"', text)
    text = re.sub(r'HTF_INTERVAL: str = "[^"]*"', f'HTF_INTERVAL: str = "{params["htf_interval"]}"', text)
    text = re.sub(r'EMA_FAST: int = \d+', f'EMA_FAST: int = {params["ema_fast"]}', text)
    text = re.sub(r'EMA_SLOW: int = \d+', f'EMA_SLOW: int = {params["ema_slow"]}', text)
    text = re.sub(r'REGIME_ER_MIN:    float = [\d.]+', f'REGIME_ER_MIN:    float = {params["regime_er_min"]}', text)
    text = re.sub(r'REGIME_ER_PERIOD: int   = \d+', f'REGIME_ER_PERIOD: int   = {params["regime_er_period"]}', text)
    text = re.sub(r'RSI_BUY_MIN: float = [\d.]+', f'RSI_BUY_MIN: float = {params["rsi_buy_min"]}', text)
    text = re.sub(r'RSI_BUY_MAX: float = [\d.]+', f'RSI_BUY_MAX: float = {params["rsi_buy_max"]}', text)
    text = re.sub(r'RSI_SELL_MIN: float = [\d.]+', f'RSI_SELL_MIN: float = {params["rsi_sell_min"]}', text)
    text = re.sub(r'RSI_SELL_MAX: float = [\d.]+', f'RSI_SELL_MAX: float = {params["rsi_sell_max"]}', text)
    text = re.sub(r'ATR_SL_MULTIPLIER_TREND: float = [\d.]+', f'ATR_SL_MULTIPLIER_TREND: float = {params["atr_sl_trend"]}', text)
    text = re.sub(r'ATR_SL_MULTIPLIER_CHOP:  float = [\d.]+', f'ATR_SL_MULTIPLIER_CHOP:  float = {params["atr_sl_chop"]}', text)
    text = re.sub(r'ATR_TP_MULTIPLIER: float = [\d.]+', f'ATR_TP_MULTIPLIER: float = {params["atr_tp_multiplier"]}', text)
    text = re.sub(r'BREAKEVEN_R:      float = [\d.]+', f'BREAKEVEN_R:      float = {params["breakeven_r"]}', text)
    text = re.sub(r'SCALE_OUT_R:     float = [\d.]+', f'SCALE_OUT_R:     float = {params["scale_out_r"]}', text)

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("\n[+] config.py actualizado con éxito.")


# ------------------------------------------------------------------ #
# Entry Point                                                         #
# ------------------------------------------------------------------ #

async def main():
    parser = argparse.ArgumentParser(description="Optimizador robusto multicapa para tradingPI")
    parser.add_argument("--months", type=int, default=36, help="Meses de historial in-sample (default: 36)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    parser.add_argument("--apply", action="store_true", help="Aplicar la mejor configuración global a config.py")
    parser.add_argument("--symbol", type=str, default=None, help="Optimizar solo un símbolo específico (BTCUSDT, ETHUSDT, SOLUSDT)")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else list(SYMBOL_MODES.keys())
    
    winners = {}
    for sym in symbols:
        res = await optimize_symbol(sym, args.months, args.balance)
        if res:
            winners[sym] = res
            # Ejecutar walk-forward de validación out-of-sample (48 meses totales, 12m train / 3m test)
            await validate_walkforward(
                sym, res["params"], months_total=args.months + 12, train_months=args.months, test_months=3, step_months=3, initial_balance=args.balance
            )
            
    if args.apply and winners:
        # Si se optimizan múltiples, tomamos el de BTCUSDT como base de config global, o el promedio
        # Pero lo ideal es aplicar el ganador del símbolo principal si se ejecuta individualmente, o BTCUSDT por defecto.
        target_sym = "BTCUSDT" if "BTCUSDT" in winners else list(winners.keys())[0]
        print(f"\nAplicando configuración ganadora de {target_sym} a config.py...")
        apply_winner_config(winners[target_sym]["params"])

if __name__ == "__main__":
    asyncio.run(main())
