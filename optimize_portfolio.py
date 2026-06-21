"""
Optimizador de Portafolio Global para tradingPI.
Busca la configuración de parámetros globales que maximiza el Sharpe Ratio del portafolio combinado
de BTCUSDT, ETHUSDT y SOLUSDT compartiendo balance.
"""

import asyncio
import argparse
import sys
import re
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import config
from strategy import EMAStrategy
from risk_manager import RiskManager
from backtest_multi import fetch_all, run_portfolio_backtest, _symbol_stats, _portfolio_metrics

def compute_portfolio_metrics(trades_by_symbol: dict, balance_history: list, initial_balance: float, months: int) -> dict:
    pm = _portfolio_metrics(balance_history, initial_balance, months)
    by_symbol_stats = {}
    total_trades = 0
    for sym, trades in trades_by_symbol.items():
        total_trades += len(trades)
        if trades:
            # Reutiliza el cálculo per-trade de backtest_multi.py
            by_symbol_stats[sym] = _symbol_stats(trades, initial_balance, months)
            
    # _symbol_stats devuelve win_rate, profit_factor, total_pnl, etc.
    # Pero las claves pueden variar, vamos a mapear win_rate a win_rate y total_pnl a pnl_usdt
    symbol_data = {}
    for sym, stats in by_symbol_stats.items():
        symbol_data[sym] = {
            "trades": stats.get("total", 0),
            "win_rate": stats.get("win_rate", 0.0),
            "profit_factor": stats.get("profit_factor", 0.0),
            "pnl_usdt": stats.get("total_pnl", 0.0)
        }
            
    return {
        "final_balance": pm["final_balance"],
        "total_return": pm["total_return"],
        "annual_return": pm["annual_return"],
        "max_drawdown": pm["max_drawdown"],
        "sharpe_portfolio": pm["sharpe"],
        "calmar_portfolio": pm["calmar"],
        "total_trades": total_trades,
        "by_symbol": symbol_data
    }


# ------------------------------------------------------------------ #
# Parámetros de cuadrícula (Grid)                                     #
# ------------------------------------------------------------------ #

TIMEFRAMES = [
    ("4h", "1d"),
    ("6h", "1d"),
    ("8h", "1d")
]

EMAS = [
    (9, 21),
    (10, 30),
    (12, 26)
]

ER_MINS = [0.15, 0.2, 0.25]
ER_PERIODS = [10, 12]

ATR_SLS = [
    (2.5, 1.5),
    (3.0, 2.0),
    (3.5, 2.0)
]

ATR_TPS = [4.0, 5.0]
BREAKEVENS = [0.0, 1.5]

def get_portfolio_fitness(m: dict) -> float:
    """Calcula la aptitud del portafolio. Prioriza Sharpe del portafolio,
    retorno total y controla el Max Drawdown."""
    if not m or m.get("total_trades", 0) < 15:
        return 0.0
    sharpe = max(0.0, m.get("sharpe_portfolio", 0.0))
    ret = max(-50.0, m.get("total_return", 0.0)) / 10.0
    dd_penalty = 1.0 if m.get("max_drawdown", 0.0) < 10.0 else (10.0 / m.get("max_drawdown", 10.0))
    return sharpe * (1.0 + ret) * dd_penalty

# ------------------------------------------------------------------ #
# Main optimization loop                                              #
# ------------------------------------------------------------------ #

async def main():
    parser = argparse.ArgumentParser(description="Optimizador global de portafolio")
    parser.add_argument("--months", type=int, default=36, help="Meses de historial (default: 36)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    parser.add_argument("--apply", action="store_true", help="Aplicar la mejor configuración global a config.py")
    args = parser.parse_args()

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    print(f"\n[+] OPTIMIZANDO PORTAFOLIO GLOBAL: {symbols}")
    print(f"    Historial: {args.months} meses | Balance: {args.balance} USDT")
    print("=" * 70)

    # Descargar datos para todos los timeframes necesarios para todos los símbolos
    # Necesitamos descargar 4h, 6h, 8h y 1d para cada símbolo
    all_data = {}
    print("  -> Descargando datos históricos en paralelo...")
    
    # Parcheamos temporalmente config para la descarga
    orig_interval = config.INTERVAL
    orig_htf_interval = config.HTF_INTERVAL
    
    for tf, htf_tf in TIMEFRAMES:
        config.INTERVAL = tf
        config.HTF_INTERVAL = htf_tf
        # fetch_all descarga los símbolos indicados
        pairs_data = await fetch_all(symbols, args.months)
        for sym, (kl_4h, kl_1d) in pairs_data.items():
            if sym not in all_data:
                all_data[sym] = {}
            all_data[sym][tf] = kl_4h
            all_data[sym][htf_tf] = kl_1d
            
    print(f"  -> Descarga finalizada. Símbolos cargados: {list(all_data.keys())}")
    
    # Restaurar config de descarga
    config.INTERVAL = orig_interval
    config.HTF_INTERVAL = orig_htf_interval

    # Guardar estado original
    orig_interval = config.INTERVAL
    orig_htf_interval = config.HTF_INTERVAL
    orig_ema_fast = config.EMA_FAST
    orig_ema_slow = config.EMA_SLOW
    orig_regime_er_min = config.REGIME_ER_MIN
    orig_regime_er_period = config.REGIME_ER_PERIOD
    orig_atr_sl_trend = config.ATR_SL_MULTIPLIER_TREND
    orig_atr_sl_chop = config.ATR_SL_MULTIPLIER_CHOP
    orig_atr_tp = config.ATR_TP_MULTIPLIER
    orig_breakeven_r = config.BREAKEVEN_R

    # Generar grid
    grid = []
    for tf, htf_tf in TIMEFRAMES:
        for fast, slow in EMAS:
            for er_min in ER_MINS:
                for er_period in ER_PERIODS:
                    for sl_trend, sl_chop in ATR_SLS:
                        for tp_mult in ATR_TPS:
                            for be_r in BREAKEVENS:
                                grid.append({
                                    "interval": tf, "htf_interval": htf_tf,
                                    "ema_fast": fast, "ema_slow": slow,
                                    "regime_er_min": er_min, "regime_er_period": er_period,
                                    "atr_sl_trend": sl_trend, "atr_sl_chop": sl_chop,
                                    "atr_tp_multiplier": tp_mult,
                                    "breakeven_r": be_r
                                })
                                
    total_combos = len(grid)
    print(f"  -> Grid generado: {total_combos} combinaciones globales.")
    
    results = []
    for idx, p in enumerate(grid, 1):
        # Configurar variables globales
        config.INTERVAL = p["interval"]
        config.HTF_INTERVAL = p["htf_interval"]
        config.EMA_FAST = p["ema_fast"]
        config.EMA_SLOW = p["ema_slow"]
        config.REGIME_ER_MIN = p["regime_er_min"]
        config.REGIME_ER_PERIOD = p["regime_er_period"]
        config.ATR_SL_MULTIPLIER_TREND = p["atr_sl_trend"]
        config.ATR_SL_MULTIPLIER_CHOP = p["atr_sl_chop"]
        config.ATR_TP_MULTIPLIER = p["atr_tp_multiplier"]
        config.BREAKEVEN_R = p["breakeven_r"]
        
        # Preparar pairs_data para run_portfolio_backtest
        pairs_data = {}
        for sym in symbols:
            pairs_data[sym] = (all_data[sym][p["interval"]], all_data[sym][p["htf_interval"]])
            
        try:
            trades_dict, bal_hist = run_portfolio_backtest(pairs_data, args.balance)
            m = compute_portfolio_metrics(trades_dict, bal_hist, args.balance, args.months)
            fit = get_portfolio_fitness(m)
            if fit > 0:
                results.append({
                    "fitness": fit,
                    "metrics": m,
                    "params": p
                })
        except Exception as e:
            pass
            
        if idx % 100 == 0 or idx == total_combos:
            done = int(idx / total_combos * 40)
            bar = "#" * done + "-" * (40 - done)
            print(f"   [{bar}] {idx}/{total_combos} combinaciones...", end="\r", flush=True)
            
    print()
    
    # Ordenar y mostrar resultados
    results.sort(key=lambda x: x["fitness"], reverse=True)
    valid_results = results[:15]
    
    print("\n" + "=" * 85)
    print(f"  TOP 15 CONFIGURACIONES GLOBALES DE PORTAFOLIO  |  3 SÍMBOLOS  |  {args.months} meses")
    print("=" * 85)
    print(f"  {'#':>2}  {'TF':<3} {'EMA':<6} {'ER_Min':<6} {'SL(T/C)':<8} {'TP':<4} {'BE':<4} | {'Trades':>5} {'PF':>5} {'Sharpe':>6} {'MaxDD%':>6} {'Retorno':>8}")
    print("  " + "-" * 81)
    
    for rank, r in enumerate(valid_results, 1):
        p = r["params"]
        m = r["metrics"]
        marker = "  <<" if rank == 1 else ""
        print(
            f"  {rank:>2}  {p['interval']:<3} {p['ema_fast']:>2}/{p['ema_slow']:<3} "
            f"{p['regime_er_min']:<6.2f} {p['atr_sl_trend']}/{p['atr_sl_chop']:<5} {p['atr_tp_multiplier']:<4.1f} {p['breakeven_r']:<4.1f} | "
            f"{m['total_trades']:>5} "
            f"{m.get('profit_factor', 0.0):>5.2f} "
            f"{m['sharpe_portfolio']:>6.2f} "
            f"{m['max_drawdown']:>6.2f}% "
            f"{m['total_return']:>+7.2f}%"
            f"{marker}"
        )
        
    if not valid_results:
        print("  Ninguna combinación superó el criterio mínimo de rentabilidad/trades.")
        # Restaurar
        config.INTERVAL = orig_interval
        config.HTF_INTERVAL = orig_htf_interval
        config.EMA_FAST = orig_ema_fast
        config.EMA_SLOW = orig_ema_slow
        config.REGIME_ER_MIN = orig_regime_er_min
        config.REGIME_ER_PERIOD = orig_regime_er_period
        config.ATR_SL_MULTIPLIER_TREND = orig_atr_sl_trend
        config.ATR_SL_MULTIPLIER_CHOP = orig_atr_sl_chop
        config.ATR_TP_MULTIPLIER = orig_atr_tp
        config.BREAKEVEN_R = orig_breakeven_r
        return

    # Detalle ganadora
    best = valid_results[0]
    bp = best["params"]
    bm = best["metrics"]
    
    print("\n" + "=" * 85)
    print("  DETALLE DE LA CONFIGURACIÓN GLOBAL GANADORA")
    print("=" * 85)
    print(f"  Temporalidad principal : {bp['interval']}  (HTF: {bp['htf_interval']})")
    print(f"  EMA                    : {bp['ema_fast']}/{bp['ema_slow']}")
    print(f"  Filtro ER régimen min  : {bp['regime_er_min']} (periodo: {bp['regime_er_period']})")
    print(f"  ATR Stop-Loss          : Trend={bp['atr_sl_trend']}x | Chop={bp['atr_sl_chop']}x")
    print(f"  ATR Take-Profit        : {bp['atr_tp_multiplier']}x")
    print(f"  Break-even R           : {bp['breakeven_r']}")
    print()
    print(f"  Total Trades           : {bm['total_trades']}")
    print(f"  Sharpe Portafolio      : {bm['sharpe_portfolio']:.2f}")
    print(f"  Max Drawdown           : {bm['max_drawdown']:.2f}%")
    print(f"  Retorno Total          : {bm['total_return']:+.2f}%  (Anual: {bm['annual_return']:+.2f}%)")
    print(f"  Balance Final          : {bm['final_balance']:.2f} USDT")
    
    # Detalle de símbolos individuales
    print("\n  Rendimiento por Símbolo:")
    for sym in symbols:
        sm = bm["by_symbol"].get(sym, {})
        print(f"    * {sym:<9}: Trades={sm.get('trades',0):>3} | Win%={sm.get('win_rate',0.0):>5.1f}% | PF={sm.get('profit_factor',0.0):>5.2f} | PnL={sm.get('pnl_usdt',0.0):>+7.2f} USDT")

    if args.apply:
        # Modificamos config.py
        path = "config.py"
        with open(path, encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r'(?<!HTF_)INTERVAL: str = "[^"]*"', f'INTERVAL: str = "{bp["interval"]}"', text)
        text = re.sub(r'HTF_INTERVAL: str = "[^"]*"', f'HTF_INTERVAL: str = "{bp["htf_interval"]}"', text)
        text = re.sub(r'EMA_FAST: int = \d+', f'EMA_FAST: int = {bp["ema_fast"]}', text)
        text = re.sub(r'EMA_SLOW: int = \d+', f'EMA_SLOW: int = {bp["ema_slow"]}', text)
        text = re.sub(r'REGIME_ER_MIN:    float = [\d.]+', f'REGIME_ER_MIN:    float = {bp["regime_er_min"]}', text)
        text = re.sub(r'REGIME_ER_PERIOD: int   = \d+', f'REGIME_ER_PERIOD: int   = {bp["regime_er_period"]}', text)
        text = re.sub(r'ATR_SL_MULTIPLIER_TREND: float = [\d.]+', f'ATR_SL_MULTIPLIER_TREND: float = {bp["atr_sl_trend"]}', text)
        text = re.sub(r'ATR_SL_MULTIPLIER_CHOP:  float = [\d.]+', f'ATR_SL_MULTIPLIER_CHOP:  float = {bp["atr_sl_chop"]}', text)
        text = re.sub(r'ATR_TP_MULTIPLIER: float = [\d.]+', f'ATR_TP_MULTIPLIER: float = {bp["atr_tp_multiplier"]}', text)
        text = re.sub(r'BREAKEVEN_R:      float = [\d.]+', f'BREAKEVEN_R:      float = {bp["breakeven_r"]}', text)
        
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print("\n[+] config.py actualizado con los parámetros de la cartera óptima.")
        
    # Restaurar config original
    config.INTERVAL = orig_interval
    config.HTF_INTERVAL = orig_htf_interval
    config.EMA_FAST = orig_ema_fast
    config.EMA_SLOW = orig_ema_slow
    config.REGIME_ER_MIN = orig_regime_er_min
    config.REGIME_ER_PERIOD = orig_regime_er_period
    config.ATR_SL_MULTIPLIER_TREND = orig_atr_sl_trend
    config.ATR_SL_MULTIPLIER_CHOP = orig_atr_sl_chop
    config.ATR_TP_MULTIPLIER = orig_atr_tp
    config.BREAKEVEN_R = orig_breakeven_r

if __name__ == "__main__":
    asyncio.run(main())
