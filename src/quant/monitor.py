# -*- coding: utf-8 -*-
"""实时监控 + 模拟盘：拉最新行情 → 算策略目标仓位 → 与模拟盘持仓比对 → 出信号并模拟记账。

用法:
    .venv/bin/python run_monitor.py --once        # 跑一次（配合系统计划任务）
    .venv/bin/python run_monitor.py               # 默认循环，间隔 config.monitor.loop_seconds
    .venv/bin/python run_monitor.py --reset       # 清空模拟盘重新开始
"""
import json
from datetime import datetime
from pathlib import Path

from .backtest import Fees, price_limit_pct
from .data import fetch_daily, fetch_realtime
from .paths import app_root
from .strategies import build_calendar, make_strategy

ROOT = app_root()
OUT = ROOT / "output"
ACCOUNT_FILE = OUT / "paper_account.json"
SIGNALS_FILE = OUT / "signals.jsonl"
STATE_FILE = OUT / "state.json"


def load_account(capital: float, reset: bool = False) -> dict:
    if not reset and ACCOUNT_FILE.exists():
        return json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    return {
        "capital": capital,
        "cash": capital,
        "positions": {},   # sym: {shares, avg_cost, name}
        "equity_history": [],
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_account(acc: dict):
    OUT.mkdir(exist_ok=True)
    ACCOUNT_FILE.write_text(json.dumps(acc, ensure_ascii=False, indent=1), encoding="utf-8")


def log_signal(sig: dict):
    OUT.mkdir(exist_ok=True)
    with SIGNALS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sig, ensure_ascii=False) + "\n")


def _paper_trade(acc: dict, symbol: str, name: str, target_w: float, price: float,
                 fees: Fees, equity: float) -> dict | None:
    """把单个标的调整到目标权重，返回成交记录（无交易返回 None）。"""
    pos = acc["positions"].get(symbol) or {"shares": 0, "avg_cost": 0.0, "name": name}
    cur_v = pos["shares"] * price
    cur_w = cur_v / equity if equity > 0 else 0.0
    dw = target_w - cur_w
    if abs(dw) < 0.005 or price <= 0:
        return None
    if dw > 0:  # 买入
        budget = min(dw * equity, acc["cash"])
        n = int(budget / (price * (1 + fees.buy_commission_bps / 10000)))
        n = n // fees.lot * fees.lot
        if n <= 0:
            return None
        value = n * price
        fee = max(fees.min_commission, value * fees.buy_commission_bps / 10000)
        acc["cash"] -= value + fee
        pos["avg_cost"] = (pos["avg_cost"] * pos["shares"] + value) / (pos["shares"] + n)
        pos["shares"] += n
        side = "买入"
        fee_out = fee
    else:  # 卖出
        if target_w <= 1e-9:
            n = pos["shares"]  # 清仓
        else:
            n = int((cur_v - target_w * equity) / price) // fees.lot * fees.lot
            n = min(n, pos["shares"])
        if n <= 0:
            return None
        value = n * price
        fee = max(fees.min_commission, value * fees.sell_commission_bps / 10000)
        tax = value * fees.sell_tax_bps / 10000
        acc["cash"] += value - fee - tax
        pos["shares"] -= n
        side = "卖出"
        fee_out = fee + tax
    if pos["shares"] > 0:
        acc["positions"][symbol] = pos
    else:
        acc["positions"].pop(symbol, None)
    return {"side": side, "symbol": symbol, "name": name, "shares": n,
            "price": round(price, 4), "value": round(value, 2), "fee": round(fee_out, 2)}


def run_once(cfg: dict, reset: bool = False) -> dict:
    mon = cfg["monitor"]
    capital = float(cfg["backtest"]["capital"])
    start = cfg["backtest"]["start"]
    fees = {m: Fees(**f) for m, f in cfg["fees"].items()}
    acc = load_account(capital, reset=reset)

    quotes = fetch_realtime(mon["watchlist_cn"] + mon["watchlist_us"])
    price_map = {q["symbol"]: (q["price"] or 0.0) for q in quotes}
    name_map = {q["symbol"]: q["name"] for q in quotes}

    # 当前总资产（实时价估值）
    equity = acc["cash"] + sum(
        p["shares"] * price_map.get(sym, p["avg_cost"])
        for sym, p in acc["positions"].items())

    signals_now, strategy_rows = [], []
    for sc in mon["strategies"]:
        try:
            mkt = sc.get("market", "cn")
            data = {s: fetch_daily(s, start=start, max_age_hours=1.5) for s in sc["universe"]}
            strat = make_strategy(sc)
            strat.prepare(data, build_calendar(data))
        except Exception as e:   # 单策略数据失败不拖死整轮监控
            strategy_rows.append({"name": sc.get("name", sc["type"]),
                                  "target": {}, "reason": f"数据拉取失败，本轮跳过（{type(e).__name__}）"})
            continue
        i = len(strat.cal) - 1
        tgt = strat.target(i)
        reason = strat.reason(i)
        strategy_rows.append({"name": strat.name, "target": tgt, "reason": reason})

        cur = {s: (acc["positions"].get(s, {}).get("shares", 0)
                   * price_map.get(s, 0.0) / equity if equity > 0 else 0.0)
               for s in sc["universe"]}
        wanted = dict(tgt)
        for s, w in cur.items():
            if w > 0.005 and s not in wanted:
                wanted[s] = 0.0
        for s, w in wanted.items():
            if abs(w - cur.get(s, 0.0)) < 0.005:
                continue
            # A股一字板约束：涨停不追买、跌停不卖（模拟盘按实时涨跌幅近似）
            limit = price_limit_pct(s, mkt)
            q = next((x for x in quotes if x["symbol"] == s), None)
            if limit and q and q["pct_chg"] is not None:
                if w > cur.get(s, 0.0) and q["pct_chg"] >= limit - 0.3:
                    continue
                if w < cur.get(s, 0.0) and q["pct_chg"] <= -(limit - 0.3):
                    continue
            price = price_map.get(s) or data[s]["close"].iloc[-1]
            trade = _paper_trade(acc, s, name_map.get(s, s), w, float(price), fees[mkt], equity)
            if trade:
                trade.update({"strategy": strat.name, "reason": reason,
                              "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                signals_now.append(trade)
                log_signal(trade)

    if signals_now:  # 交易后重新估值
        equity = acc["cash"] + sum(
            p["shares"] * price_map.get(sym, p["avg_cost"])
            for sym, p in acc["positions"].items())

    today = datetime.now().strftime("%Y-%m-%d")
    acc["equity_history"] = [h for h in acc["equity_history"] if h.get("date") != today]
    acc["equity_history"].append({"date": today, "equity": round(equity, 2)})
    acc["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_account(acc)

    recent = []
    if SIGNALS_FILE.exists():
        lines = SIGNALS_FILE.read_text(encoding="utf-8").strip().splitlines()
        recent = [json.loads(l) for l in lines[-30:]][::-1]

    state = {
        "updated": acc["updated"],
        "quotes": quotes,
        "strategies": strategy_rows,
        "account": {
            "capital": capital,
            "cash": round(acc["cash"], 2),
            "equity": round(equity, 2),
            "total_return": round(equity / capital - 1, 4),
            "positions": [
                {"symbol": s, "name": p.get("name", s), "shares": p["shares"],
                 "avg_cost": p["avg_cost"], "price": price_map.get(s),
                 "value": round(p["shares"] * price_map.get(s, p["avg_cost"]), 2),
                 "pct": round(p["shares"] * price_map.get(s, p["avg_cost"]) / equity * 100, 1) if equity else 0}
                for s, p in acc["positions"].items()],
            "equity_history": acc["equity_history"],
        },
        "signals_today": signals_now,
        "signals_recent": recent,
    }
    OUT.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return state


def print_panel(state: dict):
    acc = state["account"]
    print("=" * 64)
    print(f" 量化监控 · {state['updated']}")
    print("=" * 64)
    print(" 【实时行情】")
    for q in state["quotes"]:
        pct = "—" if q["pct_chg"] is None else f"{q['pct_chg']:+.2f}%"
        px = "—" if q["price"] is None else f"{q['price']:,.3f}"
        print(f"   {q['symbol']:<10} {q['name']:<14} {px:>12}  {pct:>8}")
    print(" 【策略信号】")
    for s in state["strategies"]:
        tgt = "、".join(f"{k} {v:.0%}" for k, v in s["target"].items()) or "空仓"
        print(f"   [{s['name']}] 目标: {tgt}")
        print(f"      依据: {s['reason']}")
    print(f" 【模拟盘】总资产 ¥{acc['equity']:,.2f}（累计 {acc['total_return']:+.2%}）"
          f"  现金 ¥{acc['cash']:,.2f}")
    for p in acc["positions"]:
        px = f"{p['price']:,.3f}" if p["price"] else "—"
        print(f"   持仓 {p['symbol']:<10} {p['shares']:>8}股  成本 {p['avg_cost']:,.3f}"
              f"  现价 {px}  市值 ¥{p['value']:,.0f}（{p['pct']:.0f}%）")
    if state["signals_today"]:
        print(f" 【新信号·已记账】{len(state['signals_today'])} 条")
        for t in state["signals_today"]:
            print(f"   {t['time']} {t['side']} {t['symbol']} {t['shares']}股 @ {t['price']}"
                  f"  [{t['strategy']}]")
    else:
        print(" 【新信号】无（持仓与策略目标一致）")
    print("=" * 64)
