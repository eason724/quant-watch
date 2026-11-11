# -*- coding: utf-8 -*-
"""回测引擎：T日收盘产生目标仓位 → T+1开盘价成交（含滑点、手续费、最小佣金、整手取整）。"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .strategies import Strategy, build_calendar

TRADING_DAYS = 252


def price_limit_pct(symbol: str, market: str) -> float | None:
    """A股价格涨跌幅限制（%）；美股 None。用于模拟一字板无法成交。"""
    if market != "cn":
        return None
    code = symbol.split(".")[-1]
    if code.startswith(("300", "301", "159", "588", "688", "689")):
        return 20.0  # 创业板/科创板股票及其ETF ±20%
    return 10.0      # 主板股票/ETF ±10%（ST 未建模）


@dataclass
class Fees:
    buy_commission_bps: float = 0.5   # 买入佣金（万分比）
    sell_commission_bps: float = 0.5  # 卖出佣金
    sell_tax_bps: float = 0.0         # 卖出印花税（ETF为0）
    min_commission: float = 0.5       # 单笔最低佣金（元）
    slippage_bps: float = 5.0         # 滑点（万分比）
    lot: int = 100                    # 一手股数（A股ETF=100，美股=1）


@dataclass
class BacktestResult:
    name: str
    equity: pd.Series = None          # 每日收盘总资产
    benchmark: pd.Series = None       # 基准（等额买入持有）
    trades: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    market: str = "cn"                # cn / us


def _metrics(equity: pd.Series, trades: list) -> dict:
    ret = equity.pct_change().dropna()
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    years = len(equity) / TRADING_DAYS
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / max(years, 1e-9)) - 1.0
    dd = (equity / equity.cummax() - 1.0)
    mdd = float(dd.min())
    sharpe = float(ret.mean() / (ret.std() + 1e-12) * np.sqrt(TRADING_DAYS))
    sells = [t for t in trades if t["side"] == "卖出"]
    wins = [t for t in sells if t.get("pnl", 0) > 0]
    return {
        "总收益率": total,
        "年化收益率": cagr,
        "最大回撤": mdd,
        "夏普比率": sharpe,
        "交易次数": len(trades),
        "胜率": (len(wins) / len(sells)) if sells else None,
        "期末资产": float(equity.iloc[-1]),
    }


def run_backtest(strategy: Strategy, data: dict[str, pd.DataFrame],
                 fees: Fees, capital: float = 100000.0,
                 benchmark_df: pd.DataFrame | None = None,
                 market: str = "cn") -> BacktestResult:
    strategy.prepare(data, build_calendar(data))
    cal = strategy.cal

    close_ff = pd.DataFrame({s: data[s]["close"].reindex(cal).ffill() for s in strategy.universe})
    open_f = pd.DataFrame({s: data[s]["open"].reindex(cal) for s in strategy.universe})

    cash = float(capital)
    shares = {s: 0 for s in strategy.universe}
    avg_cost = {s: 0.0 for s in strategy.universe}
    lots = {s: [] for s in strategy.universe}  # T+1: [(买入日, 股数)]
    trades: list[dict] = []
    equity_rows = []

    pending = None  # 昨收算出的目标权重，今日开盘执行

    for i, date in enumerate(cal):
        # 1) 开盘执行昨日目标
        if pending is not None:
            # 当前总资产（以今日开盘价计，停牌标的用最近收盘价）
            open_px = {s: open_f.at[date, s] if not pd.isna(open_f.at[date, s])
                       else close_ff.at[date, s] for s in strategy.universe}
            total_eq = cash + sum(shares[s] * open_px[s] for s in strategy.universe)
            cur_w = {s: (shares[s] * open_px[s] / total_eq if total_eq > 0 else 0.0)
                     for s in strategy.universe}
            today = str(date.date())
            for s, w in pending.items():
                dw = w - cur_w.get(s, 0.0)
                if abs(dw) < 0.005:  # 权重变动<0.5%不动仓，省手续费
                    continue
                px = open_f.at[date, s]
                if pd.isna(px):  # 停牌今日不可交易
                    continue
                # A股一字板约束：开盘涨停买不进、开盘跌停卖不出
                limit = price_limit_pct(s, market)
                blocked_buy = blocked_sell = False
                if limit is not None and i > 0:
                    prev = close_ff.at[cal[i - 1], s]
                    if not pd.isna(prev):
                        chg = (px / prev - 1.0) * 100.0
                        blocked_buy = chg >= limit - 0.3
                        blocked_sell = chg <= -(limit - 0.3)
                if dw > 0:  # 买入
                    if blocked_buy:
                        continue
                    budget = min(dw * total_eq, cash)
                    price = px * (1 + fees.slippage_bps / 10000)
                    n = int(budget / (price * (1 + fees.buy_commission_bps / 10000)))
                    n = n // fees.lot * fees.lot
                    if n <= 0:
                        continue
                    value = n * price
                    fee = max(fees.min_commission, value * fees.buy_commission_bps / 10000)
                    cash -= value + fee
                    avg_cost[s] = (avg_cost[s] * shares[s] + value) / (shares[s] + n) \
                        if shares[s] + n > 0 else price
                    shares[s] += n
                    lots[s].append((today, n))
                    trades.append(dict(date=str(date.date()), symbol=s, side="买入",
                                       shares=n, price=round(price, 4), value=round(value, 2),
                                       fee=round(fee, 2), pnl=None))
                elif dw < 0 and shares[s] > 0:  # 卖出（受 T+1 可卖数量限制）
                    if blocked_sell:
                        continue
                    sellable = sum(sh for d, sh in lots[s] if d < today)
                    if sellable <= 0:
                        continue
                    if w <= 1e-9:
                        n = shares[s]  # 清仓
                    else:
                        target_v = w * total_eq
                        n = int((shares[s] * open_px[s] - target_v) / open_px[s]) // fees.lot * fees.lot
                        n = min(n, shares[s])
                    n = min(n, sellable)
                    if n <= 0:
                        continue
                    price = px * (1 - fees.slippage_bps / 10000)
                    value = n * price
                    fee = max(fees.min_commission, value * fees.sell_commission_bps / 10000)
                    tax = value * fees.sell_tax_bps / 10000
                    cash += value - fee - tax
                    pnl = n * (price - avg_cost[s]) - fee - tax
                    shares[s] -= n
                    remain = n
                    while remain > 0 and lots[s]:  # 从最早批次扣减
                        d, sh = lots[s][0]
                        take = min(sh, remain)
                        lots[s][0] = (d, sh - take)
                        remain -= take
                        if lots[s][0][1] == 0:
                            lots[s].pop(0)
                    trades.append(dict(date=str(date.date()), symbol=s, side="卖出",
                                       shares=n, price=round(price, 4), value=round(value, 2),
                                       fee=round(fee + tax, 2), pnl=round(pnl, 2)))
                    if shares[s] == 0:
                        avg_cost[s] = 0.0
        pending = None

        # 2) 收盘估值 + 生成明日目标
        eq = cash + sum(shares[s] * close_ff.at[date, s] for s in strategy.universe)
        equity_rows.append((date, eq))
        tgt = strategy.target(i) if not pd.isna(close_ff.iloc[i].min()) else {}
        cur_w = {}
        if eq > 0:
            cur_w = {s: shares[s] * close_ff.at[date, s] / eq for s in strategy.universe}
        new_pending = {s: w for s, w in tgt.items()
                       if abs(w - cur_w.get(s, 0.0)) > 0.005}
        # 目标里没有但手里有的 → 需要清仓
        for s, w in cur_w.items():
            if w > 0.005 and s not in tgt:
                new_pending[s] = 0.0
        pending = new_pending if new_pending else None

    equity = pd.Series(dict(equity_rows)).sort_index()

    # 基准：等额买入持有
    bench = None
    if benchmark_df is not None:
        b = benchmark_df["close"].reindex(cal).ffill().dropna()
        if len(b):
            bench = b / b.iloc[0] * capital

    res = BacktestResult(name=strategy.name, equity=equity, benchmark=bench, trades=trades,
                         market=market)
    res.metrics = _metrics(equity, trades)
    return res
