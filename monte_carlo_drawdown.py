"""
Monte Carlo de drawdown — cuantifica la probabilidad real de disparar los circuit
breakers de config.py (MAX_MONTHLY_DRAWDOWN=8%, MAX_DAILY_DRAWDOWN=4%) en vez de
descubrirla por sorpresa en vivo.

Reutiliza run_portfolio_backtest() de backtest_multi.py para generar la lista real
de trades cerrados (con y sin --portfolio-risk-cap), y aplica block bootstrap sobre
esa lista para simular miles de secuencias alternativas de la misma operativa.

Uso:
    python monte_carlo_drawdown.py --months 60
    python monte_carlo_drawdown.py --months 60 --portfolio-risk-cap 0.01
    python monte_carlo_drawdown.py --months 60 --trials 5000 --block-size 4
"""
import asyncio
import argparse
import random
import sys
from datetime import datetime
from typing import List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import config
from risk_manager import RiskManager
from backtest_multi import fetch_all, run_portfolio_backtest, DEFAULT_SYMBOLS
from backtest import Trade

DEFAULT_TRIALS = 5000


def _flatten_trades_chronological(trades_by_symbol: dict) -> List[Trade]:
    """Junta los trades de todos los símbolos en una sola secuencia ordenada por
    tiempo de cierre — es la secuencia real de eventos que afectó al balance compartido."""
    all_trades = [t for trades in trades_by_symbol.values() for t in trades]
    return sorted(all_trades, key=lambda t: t.exit_time)


def _month_key(ts: str) -> str:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M").strftime("%Y-%m")


def _day_key(ts: str) -> str:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M").strftime("%Y-%m-%d")


def _make_blocks(trades: List[Trade], block_size: int) -> List[List[Trade]]:
    """Divide la secuencia real en bloques consecutivos de N trades — preserva el
    agrupamiento temporal real (rachas, clusters de volatilidad) en vez de barajar
    cada trade de forma independiente, que perdería la estructura de racha real."""
    return [trades[i:i + block_size] for i in range(0, len(trades), block_size)]


def simulate_drawdown_breach(
    trades: List[Trade], initial_balance: float, trials: int, block_size: int,
) -> dict:
    """Block bootstrap: para cada trial, resamplea con reemplazo bloques de trades
    consecutivos hasta reconstruir una secuencia de la misma longitud que la real, y
    recorre esa secuencia sintética aplicando exactamente la misma regla de breach que
    risk_manager.is_monthly_drawdown_exceeded / is_daily_drawdown_exceeded."""
    risk_manager = RiskManager()
    blocks = _make_blocks(trades, block_size)
    target_len = len(trades)

    monthly_breaches = 0
    daily_breaches = 0
    max_dd_samples = []

    for _ in range(trials):
        synthetic: List[Trade] = []
        while len(synthetic) < target_len:
            synthetic.extend(random.choice(blocks))
        synthetic = synthetic[:target_len]

        balance = initial_balance
        month_start_balance = balance
        day_start_balance = balance
        current_month = None
        current_day = None
        peak, max_dd = balance, 0.0
        breached_month = False
        breached_day = False

        for t in synthetic:
            m_key = _month_key(t.exit_time)
            d_key = _day_key(t.exit_time)
            if m_key != current_month:
                current_month = m_key
                month_start_balance = balance
            if d_key != current_day:
                current_day = d_key
                day_start_balance = balance

            balance += t.pnl_usdt
            peak = max(peak, balance)
            max_dd = max(max_dd, (peak - balance) / peak * 100 if peak > 0 else 0.0)

            if not breached_month and risk_manager.is_monthly_drawdown_exceeded(balance, month_start_balance):
                breached_month = True
            if not breached_day and risk_manager.is_daily_drawdown_exceeded(balance, day_start_balance):
                breached_day = True

        monthly_breaches += breached_month
        daily_breaches += breached_day
        max_dd_samples.append(max_dd)

    max_dd_samples.sort()

    def pct(p):
        idx = min(int(p / 100 * len(max_dd_samples)), len(max_dd_samples) - 1)
        return max_dd_samples[idx]

    return {
        "trials": trials,
        "p_monthly_breach": monthly_breaches / trials * 100,
        "p_daily_breach": daily_breaches / trials * 100,
        "max_dd_p5": pct(5),
        "max_dd_p50": pct(50),
        "max_dd_p95": pct(95),
    }


def print_report(label: str, result: dict, total_trades: int):
    print(f"\n  ── {label} " + "─" * (50 - len(label)))
    print(f"  Trades históricos usados como base : {total_trades}")
    print(f"  Simulaciones (trials)              : {result['trials']}")
    print(f"  P(breach mensual, DD>=8%)          : {result['p_monthly_breach']:.1f}%")
    print(f"  P(breach diario,  DD>=4%)          : {result['p_daily_breach']:.1f}%")
    print(f"  Max DD simulado  p5 / p50 / p95    : "
          f"{result['max_dd_p5']:.2f}% / {result['max_dd_p50']:.2f}% / {result['max_dd_p95']:.2f}%")


async def main(months: int, balance: float, trials: int, block_size: int, portfolio_risk_cap: float):
    print(f"\nDescargando datos para Monte Carlo ({months} meses)...")
    pairs_data = await fetch_all(DEFAULT_SYMBOLS, months)

    print("\n" + "=" * 62)
    print("  MONTE CARLO DE DRAWDOWN — block bootstrap")
    print(f"  Periodo base: {months} meses  |  Trials: {trials}  |  Block size: {block_size}")
    print("=" * 62)

    # Escenario base: sin cap de portafolio (comportamiento actual de config.py)
    trades_by_symbol, _ = run_portfolio_backtest(pairs_data, balance, portfolio_risk_cap=0.0)
    trades = _flatten_trades_chronological(trades_by_symbol)
    if len(trades) < block_size * 3:
        print("\n  Muy pocos trades para un bootstrap confiable — ampliar --months.")
        return
    result_uncapped = simulate_drawdown_breach(trades, balance, trials, block_size)
    print_report("SIN cap de portafolio (actual)", result_uncapped, len(trades))

    # Escenario con cap, si se pidió uno > 0
    if portfolio_risk_cap > 0:
        trades_by_symbol_capped, _ = run_portfolio_backtest(pairs_data, balance, portfolio_risk_cap=portfolio_risk_cap)
        trades_capped = _flatten_trades_chronological(trades_by_symbol_capped)
        result_capped = simulate_drawdown_breach(trades_capped, balance, trials, block_size)
        print_report(f"CON cap de portafolio ({portfolio_risk_cap*100:.1f}%)", result_capped, len(trades_capped))

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monte Carlo de drawdown — circuit breakers")
    parser.add_argument("--months", type=int, default=60, help="Meses de historial (default: 60)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS, help=f"Simulaciones (default: {DEFAULT_TRIALS})")
    parser.add_argument("--block-size", type=int, default=4, help="Tamaño de bloque para el bootstrap (default: 4)")
    parser.add_argument(
        "--portfolio-risk-cap", type=float, default=0.0,
        help="Si > 0, compara también el escenario con cap de riesgo de portafolio (ej. 0.01 = 1%%)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.months, args.balance, args.trials, args.block_size, args.portfolio_risk_cap))
