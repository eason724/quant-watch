# -*- coding: utf-8 -*-
"""数据层：东方财富免费行情接口（A股 + 美股），带本地缓存。"""
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from .paths import app_root

ROOT = app_root()
CACHE = ROOT / "data_cache"
CACHE.mkdir(exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
})

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
REALTIME_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"

# K线字段: 日期,开盘,收盘,最高,最低,成交量,成交额
KLINE_FIELDS = "f51,f52,f53,f54,f55,f56,f57"


def _get_json(url, params, tries=3, timeout=15):
    last_err = None
    for k in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # 网络抖动重试
            last_err = e
            time.sleep(1.0 + k)
    raise RuntimeError(f"请求失败 {url}: {last_err}")


_SECID_CACHE: dict | None = None


def _secid_cache() -> dict:
    global _SECID_CACHE
    if _SECID_CACHE is None:
        f = CACHE / "_secids.json"
        if f.exists():
            try:
                _SECID_CACHE = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                _SECID_CACHE = {}
        else:
            _SECID_CACHE = {}
    return _SECID_CACHE


def _save_secid(symbol: str, secid: str):
    cache = _secid_cache()
    cache[symbol.upper()] = secid
    try:
        (CACHE / "_secids.json").write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def resolve_secid(symbol: str) -> str:
    """把 '600519' / '510300' / 'SPY' / 'QQQ' 解析成东财 secid，如 '1.600519'、'105.QQQ'。"""
    s = str(symbol).strip().upper()
    cached = _secid_cache().get(s)
    if cached:
        return cached
    if re.fullmatch(r"\d{6}", s):  # A股/ETF: 6/5/9 开头沪市(1.)，0/1/3 开头深市(0.)
        secid = ("1." if s[0] in "569" else "0.") + s
        _save_secid(s, secid)
        return secid
    if re.fullmatch(r"\d{1,3}\.[A-Z0-9]+", s):  # 已是 secid，如 1.600519 / 105.QQQ / 107.SPY
        return s
    # 美股：先查符号搜索接口，失败则逐个市场探测
    try:
        js = _get_json(SUGGEST_URL, {"input": s, "type": "14", "count": 5, "status": "1"})
        for item in js["QuotationCodeTable"]["Data"] or []:
            if item.get("Code") == s and item.get("QuoteID"):
                _save_secid(s, item["QuoteID"])
                return item["QuoteID"]
    except Exception:
        pass
    for mkt in (105, 106, 107):
        try:
            js = _get_json(KLINE_URL, {
                "secid": f"{mkt}.{s}", "fields1": "f1", "fields2": "f53",
                "klt": "101", "fqt": "1", "beg": "20240102", "end": "20240105"})
            if js.get("data") and js["data"].get("klines"):
                _save_secid(s, f"{mkt}.{s}")
                return f"{mkt}.{s}"
        except Exception:
            continue
    raise ValueError(f"无法识别的标的代码: {symbol}")


def fetch_daily(symbol: str, start: str = "2018-01-01", end: str | None = None,
                adjust: bool = True, max_age_hours: float = 6.0) -> pd.DataFrame:
    """日线行情。返回 DataFrame(index=日期, open/high/low/close/volume/amount)。前复权。"""
    secid = resolve_secid(symbol)
    end = end or datetime.now().strftime("%Y-%m-%d")
    start_d = start.replace("-", "")
    end_d = end.replace("-", "")
    cache_file = CACHE / f"{secid.replace('.', '_')}.csv"
    today = datetime.now().strftime("%Y-%m-%d")

    use_cache = False
    if cache_file.exists():
        age_h = (time.time() - cache_file.stat().st_mtime) / 3600
        df_cached = None  # 惰性读取
        if age_h < max_age_hours:
            use_cache = True
        else:
            # 过期但缓存里已包含想要的历史且不要求最新数据，也可直接用
            if end_d < today:
                use_cache = True

    if use_cache:
        df = pd.read_csv(cache_file, index_col=0, parse_dates=[0])
        # 缓存覆盖不住请求的截止日则强制刷新
        if df.index.max() >= pd.Timestamp(end) or end_d < today:
            return _slice(df, start_d, end_d)

    js = _get_json(KLINE_URL, {
        "secid": secid, "fields1": "f1,f2,f3", "fields2": KLINE_FIELDS,
        "klt": "101", "fqt": "1" if adjust else "0",
        "beg": "20180101", "end": "20500101",
    })
    if not js.get("data") or not js["data"].get("klines"):
        raise ValueError(f"未取到 {symbol}({secid}) 的K线数据")
    rows = [ln.split(",") for ln in js["data"]["klines"]]
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "close", "high", "low", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.set_index("date").sort_index()
    df.to_csv(cache_file)
    return _slice(df, start_d, end_d)


def _slice(df, start_d, end_d):
    return df[(df.index >= pd.Timestamp(start_d)) & (df.index <= pd.Timestamp(end_d))].copy()


def fetch_realtime(symbols: list[str]) -> list[dict]:
    """批量实时快照: 最新价、涨跌幅。跨市场混排。"""
    code2secid = {}
    for s in symbols:
        try:
            secid = resolve_secid(s)
            code2secid[secid.split(".", 1)[1]] = secid
        except ValueError:
            continue
    secids = ",".join(code2secid.values())
    js = _get_json(REALTIME_URL, {
        "secids": secids, "fields": "f1,f2,f3,f12,f14,f18", "fltt": "2", "fid": "f3"})
    out = []
    for d in (js.get("data") or {}).get("diff") or []:
        price = d.get("f2")
        out.append({
            "symbol": d.get("f12"),
            "secid": code2secid.get(d.get("f12"), ""),
            "name": d.get("f14"),
            "price": float(price) if isinstance(price, (int, float)) else None,
            "pct_chg": float(d["f3"]) if isinstance(d.get("f3"), (int, float)) else None,
            "prev_close": d.get("f18"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out
