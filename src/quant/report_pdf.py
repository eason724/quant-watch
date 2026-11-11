# -*- coding: utf-8 -*-
"""PDF 研报导出：reportlab 排版 + matplotlib 资金曲线图。中文支持：优先微软雅黑，缺失时回退 reportlab 自带宋体。"""
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

FONT_CANDIDATES = [
    "/mnt/c/Windows/Fonts/msyh.ttc",            # WSL 挂载盘下的微软雅黑
    "C:/Windows/Fonts/msyh.ttc",                # Windows 原生
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]
FONT_NAME = "MSYH"
_registered = False


def _ensure_font():
    global _registered, FONT_NAME
    if _registered:
        return
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, p, subfontIndex=0))
                _registered = True
                return
            except Exception:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))  # 内置宋体兜底
    FONT_NAME = "STSong-Light"
    _registered = True


def _setup_matplotlib_font():
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                font_manager.fontManager.addfont(p)
                name = font_manager.FontProperties(fname=p).get_name()
                plt.rcParams["font.family"] = name
                plt.rcParams["axes.unicode_minus"] = False
                return
            except Exception:
                continue


def _plot_equity(res, out_png: Path):
    _setup_matplotlib_font()
    fig, ax = plt.subplots(figsize=(8.6, 3.4), dpi=150)
    eq = res.equity / res.equity.iloc[0]
    ax.plot(eq.index, eq.values, lw=1.6, color="#C00000", label="策略净值")
    if res.benchmark is not None:
        b = res.benchmark.dropna()
        ax.plot(b.index, (b / b.iloc[0]).values, lw=1.2, color="#7F7F7F", label="基准(买入持有)")
    dd = eq / eq.cummax() - 1
    ax.fill_between(eq.index, dd.values, 0, color="#C00000", alpha=0.12)
    ax.set_title(res.name, fontsize=13)
    ax.legend(loc="lower left", fontsize=9, frameon=False)
    ax.grid(alpha=0.25, lw=0.6)
    ax.set_ylabel("净值(期初=1)")
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


def export_pdf(results, path: Path, capital: float, start: str,
               fees_note: str, charts_dir: Path):
    _ensure_font()
    charts_dir.mkdir(exist_ok=True)

    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                            title="量化策略回测报告")
    h1 = ParagraphStyle("h1", fontName=FONT_NAME, fontSize=20, textColor=colors.HexColor("#1F4E79"),
                        spaceAfter=6, leading=26)
    h2 = ParagraphStyle("h2", fontName=FONT_NAME, fontSize=13, textColor=colors.HexColor("#1F4E79"),
                        spaceBefore=10, spaceAfter=4)
    sub = ParagraphStyle("sub", fontName=FONT_NAME, fontSize=9.5, textColor=colors.HexColor("#595959"))
    body = ParagraphStyle("body", fontName=FONT_NAME, fontSize=10, leading=16)
    small = ParagraphStyle("small", fontName=FONT_NAME, fontSize=8.5, leading=13,
                           textColor=colors.HexColor("#595959"))

    story = []
    end = results[0].equity.index[-1].strftime("%Y-%m-%d") if results else ""
    story.append(Paragraph("量化策略回测报告", h1))
    story.append(Paragraph(
        f"生成时间 {datetime.now():%Y-%m-%d %H:%M} ｜ 回测区间 {start} ~ {end} ｜ "
        f"初始资金 ¥{capital:,.0f}<br/>{fees_note}", sub))
    story.append(Spacer(1, 10))

    # 总览表
    story.append(Paragraph("一、策略总览", h2))
    head = ["策略", "总收益", "年化", "最大回撤", "夏普", "胜率", "笔数", "基准"]
    rows = [head]
    for res in results:
        m = res.metrics
        bench = ""
        if res.benchmark is not None:
            bench = f"{res.benchmark.iloc[-1] / res.benchmark.iloc[0] - 1:+.1%}"
        rows.append([
            res.name,
            f"{m['总收益率']:+.1%}", f"{m['年化收益率']:+.1%}", f"{m['最大回撤']:.1%}",
            f"{m['夏普比率']:.2f}",
            "—" if m["胜率"] is None else f"{m['胜率']:.0%}",
            str(m["交易次数"]), bench])
    t = Table(rows, colWidths=[4.6 * cm] + [1.75 * cm] * 7)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F6FA")]),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFBFBF")),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(PageBreak())

    # 各策略详情
    story.append(Paragraph("二、策略详情", h2))
    for i, res in enumerate(results, 1):
        m = res.metrics
        story.append(Paragraph(f"2.{i} {res.name}", ParagraphStyle(
            f"h3{i}", parent=h2, fontSize=11.5, textColor=colors.HexColor("#333333"))))
        sell_trades = [t for t in res.trades if t["side"] == "卖出"]
        wins = sum(1 for t in sell_trades if (t["pnl"] or 0) > 0)
        info = (f"期末资产 ¥{m['期末资产']:,.0f} ｜ 交易 {m['交易次数']} 笔 ｜ "
                f"卖出 {len(sell_trades)} 笔、盈利 {wins} 笔 ｜ 最大回撤 {m['最大回撤']:.1%}")
        story.append(Paragraph(info, body))
        png = charts_dir / f"equity_{i}.png"
        _plot_equity(res, png)
        story.append(Image(str(png), width=16.4 * cm, height=16.4 * cm * 3.4 / 8.6))
        story.append(Spacer(1, 6))

    # 说明
    story.append(PageBreak())
    story.append(Paragraph("三、口径与风险提示", h2))
    for txt in [
        "交易规则：T 日收盘产生目标仓位，T+1 日开盘价成交并计入滑点；权重变动小于 0.5% 不触发交易；"
        "A股按整手（100 股/手）取整；T+1 与一字涨跌停约束已建模（开盘涨停不追买、跌停无法卖出）；"
        "停牌日不交易、按最近收盘价估值。",
        fees_note,
        "指标口径：年化收益率按 252 个交易日折算；夏普比率按日收益计算，无风险利率取 0；"
        "胜率统计口径为卖出笔中盈利的比例。",
        "能力边界：本引擎保证的是回测过程的工程诚实性（数据真实、不偷看未来、制度约束建模、"
        "现金流逐笔可核验）；无法覆盖政策突变、市场操纵、基本面造假等深层不确定性。"
        "引擎自测样本中，均值回归策略踩雷乐视网（407天停牌+复牌连续跌停+退市），实测最大回撤 -97.3%——"
        "任何交易规则都无法挽救基本面地雷，这正是量化工具的边界所在。",
        "风险提示：回测基于历史行情数据，存在过拟合与幸存者偏差风险，不代表未来表现；"
        "实盘中还可能遇到流动性不足、数据延迟、接口变更等情况。"
        "本报告仅为技术研究用途，不构成任何投资建议，据此操作风险自负。",
    ]:
        story.append(Paragraph(txt, ParagraphStyle("note", parent=body, fontSize=9.5, leading=15)))
    doc.build(story)
