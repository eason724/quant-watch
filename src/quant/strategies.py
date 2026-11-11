# -*- coding: utf-8 -*-
"""四种交易策略。统一接口：prepare(data, calendar) 之后，target(i) 返回 {标的: 目标仓位权重}。
权重为占组合总资产的比例，0=空仓，1=满仓。所有策略只做多。"""
import numpy as np
import pandas as pd


def build_calendar(data: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """并集交易日历（同市场内各标的价格日历一致）。"""
    idx = set()
    for df in data.values():
        idx |= set(df.index)
    return pd.DatetimeIndex(sorted(idx))


class Strategy:
    name = "策略基类"

    def __init__(self, universe, params=None, name=None):
        self.universe = list(universe)
        self.p = dict(self.DEFAULTS | (params or {}))
        if name:
            self.name = name
        self.cal: pd.DatetimeIndex | None = None
        self.data = None

    DEFAULTS: dict = {}

    def prepare(self, data: dict[str, pd.DataFrame], calendar: pd.DatetimeIndex):
        self.data = data
        self.cal = calendar
        self._prepare()

    def _prepare(self):
        pass

    def _close(self, sym) -> pd.Series:
        """对齐到交易日历的收盘价（停牌日前值填充）。"""
        return self.data[sym]["close"].reindex(self.cal).ffill()

    def _open(self, sym) -> pd.Series:
        """对齐到交易日历的开盘价（停牌日为 NaN，不可交易）。"""
        return self.data[sym]["open"].reindex(self.cal)

    def target(self, i: int) -> dict[str, float]:
        raise NotImplementedError

    def reason(self, i: int) -> str:
        """最新信号的一句话依据（用于监控面板/报告）。"""
        return ""


class DualMAStrategy(Strategy):
    """双均线趋势：快线上穿慢线买入，下穿卖出。"""
    name = "双均线趋势"
    DEFAULTS = {"fast": 10, "slow": 30}

    def _prepare(self):
        assert len(self.universe) == 1, "双均线策略只支持单标的"
        c = self._close(self.universe[0])
        self.ma_f = c.rolling(int(self.p["fast"])).mean()
        self.ma_s = c.rolling(int(self.p["slow"])).mean()

    def target(self, i):
        f, s = self.ma_f.iloc[i], self.ma_s.iloc[i]
        if pd.isna(f) or pd.isna(s):
            return {}
        return {self.universe[0]: (1.0 if f > s else 0.0)}

    def reason(self, i):
        f, s = self.ma_f.iloc[i], self.ma_s.iloc[i]
        if pd.isna(f) or pd.isna(s):
            return "均线数据不足"
        return f"MA{self.p['fast']}={f:.3f} {'↑' if f > s else '↓'} MA{self.p['slow']}={s:.3f}"


class MomentumRotation(Strategy):
    """动量轮动：每期持有近 N 日涨幅最强的 top_n 只，均分仓位；全部走弱则空仓。"""
    name = "动量轮动"
    DEFAULTS = {"lookback": 20, "top_n": 1, "min_momentum": 0.0}

    def _prepare(self):
        lb = int(self.p["lookback"])
        self.mom = pd.DataFrame({
            sym: self._close(sym) / self._close(sym).shift(lb) - 1.0
            for sym in self.universe})

    def target(self, i):
        row = self.mom.iloc[i]
        if row.isna().all():
            return {}
        valid = row.dropna()
        picked = valid[valid > float(self.p["min_momentum"])].nlargest(int(self.p["top_n"]))
        if picked.empty:
            return {}
        w = 1.0 / len(picked)
        return {sym: w for sym in picked.index}

    def reason(self, i):
        row = self.mom.iloc[i].dropna()
        if row.empty:
            return "动量数据不足"
        picked = row[row > float(self.p["min_momentum"])].nlargest(int(self.p["top_n"]))
        if picked.empty:
            return "池内标的动量全部走弱，空仓回避"
        parts = [f"{sym} 近{self.p['lookback']}日 {row[sym]:+.1%}" for sym in picked.index]
        return "动量最强: " + "、".join(parts)


class MeanReversionStrategy(Strategy):
    """均值回归：RSI 低于买入阈值超卖买入，高于卖出阈值止盈离场，其间持有。"""
    name = "均值回归RSI"
    DEFAULTS = {"rsi_period": 14, "buy_th": 30, "sell_th": 70}

    def _prepare(self):
        assert len(self.universe) == 1
        c = self._close(self.universe[0])
        n = int(self.p["rsi_period"])
        diff = c.diff()
        gain = diff.clip(lower=0.0).ewm(alpha=1 / n, adjust=False).mean()
        loss = (-diff.clip(upper=0.0)).ewm(alpha=1 / n, adjust=False).mean()
        rs = gain / loss.replace(0.0, np.nan)
        self.rsi = 100 - 100 / (1 + rs)
        # 状态化持仓序列：超卖进、超买卖
        pos = np.zeros(len(c))
        holding = 0
        for i in range(len(c)):
            r = self.rsi.iloc[i]
            if not pd.isna(r):
                if r < float(self.p["buy_th"]):
                    holding = 1
                elif r > float(self.p["sell_th"]):
                    holding = 0
            pos[i] = holding
        self.pos = pd.Series(pos, index=self.cal)

    def target(self, i):
        return {self.universe[0]: float(self.pos.iloc[i])}

    def reason(self, i):
        r = self.rsi.iloc[i]
        if pd.isna(r):
            return "RSI数据不足"
        zone = "超卖区→持仓" if self.pos.iloc[i] > 0 else "超买区/中性区→空仓"
        return f"RSI{self.p['rsi_period']}={r:.1f}（{zone}）"


class GridTrading(Strategy):
    """网格交易：以起始价为中心，每跌一档买入一格、每涨一档卖出一格。
    仓位函数：units = levels + (下方已穿越格数) - (上方已穿越格数)，满仓=2*levels 格。"""
    name = "网格交易"
    DEFAULTS = {"step": 0.03, "levels": 6}

    def _prepare(self):
        assert len(self.universe) == 1
        c = self._close(self.universe[0])
        self.center = float(c.dropna().iloc[0])
        step = float(self.p["step"])
        levels = int(self.p["levels"])
        self.down = [self.center * (1 - step * k) for k in range(1, levels + 1)]
        self.up = [self.center * (1 + step * k) for k in range(1, levels + 1)]

    def target(self, i):
        p = self._close(self.universe[0]).iloc[i]
        if pd.isna(p):
            return {}
        levels = int(self.p["levels"])
        units = levels
        units += sum(1 for ln in self.down if p < ln)
        units -= sum(1 for ln in self.up if p > ln)
        return {self.universe[0]: units / (2.0 * levels)}

    def reason(self, i):
        p = self._close(self.universe[0]).iloc[i]
        if pd.isna(p):
            return "价格数据不足"
        levels = int(self.p["levels"])
        units = levels
        units += sum(1 for ln in self.down if p < ln)
        units -= sum(1 for ln in self.up if p > ln)
        return f"价格 {p:.3f}，网格持仓 {units}/{2 * levels} 格（中心 {self.center:.3f}）"


STRATEGY_TYPES = {
    "dual_ma": DualMAStrategy,
    "momentum": MomentumRotation,
    "mean_reversion": MeanReversionStrategy,
    "grid": GridTrading,
}


def make_strategy(cfg: dict) -> Strategy:
    cls = STRATEGY_TYPES[cfg["type"]]
    return cls(cfg["universe"], cfg.get("params"), name=cfg.get("name"))
