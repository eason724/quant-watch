# -*- coding: utf-8 -*-
"""Excel 报表导出：概览 / 资金曲线 / 逐笔交易 / 说明 四个 sheet，带折线图。"""
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=16, bold=True, color="1F4E79")
SUB_FONT = Font(name="微软雅黑", size=10, color="595959")
BODY_FONT = Font(name="微软雅黑", size=10)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")


def _style_header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = BORDER


def export_xlsx(results, path: Path, capital: float, start: str, fees_note: str):
    wb = Workbook()

    # ---------- 概览 ----------
    ws = wb.active
    ws.title = "概览"
    ws["A1"] = "量化策略回测报告"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"生成时间 {datetime.now():%Y-%m-%d %H:%M} · 初始资金 ¥{capital:,.0f} · "
                f"回测区间 {start} ~ 今 · {fees_note}")
    ws["A2"].font = SUB_FONT
    headers = ["策略", "市场", "总收益率", "年化收益率", "最大回撤", "夏普比率",
               "胜率", "交易次数", "期末资产(¥)", "同期基准收益"]
    r0 = 4
    for c, h in enumerate(headers, 1):
        ws.cell(row=r0, column=c, value=h)
    _style_header(ws, r0, len(headers))
    for i, res in enumerate(results):
        m = res.metrics
        row = r0 + 1 + i
        bench_ret = (res.benchmark.iloc[-1] / res.benchmark.iloc[0] - 1) if res.benchmark is not None else None
        vals = [res.name, "A股" if res.market == "cn" else "美股",
                m["总收益率"], m["年化收益率"], m["最大回撤"], m["夏普比率"],
                m["胜率"] if m["胜率"] is not None else "—", m["交易次数"],
                round(m["期末资产"], 2), bench_ret if bench_ret is not None else "—"]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.font = BODY_FONT
            cell.border = BORDER
            if c in (3, 4, 5, 7, 10):
                cell.number_format = "0.0%"
            elif c == 6:
                cell.number_format = "0.00"
            elif c == 9:
                cell.number_format = "#,##0"
        if m["总收益率"] > 0:
            ws.cell(row=row, column=3).font = Font(name="微软雅黑", size=10, color="C00000", bold=True)
        else:
            ws.cell(row=row, column=3).font = Font(name="微软雅黑", size=10, color="008000")
    widths = [24, 8, 10, 11, 10, 10, 8, 9, 13, 12]
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.print_area = f"A1:J{r0 + len(results)}"

    # ---------- 资金曲线（每策略一块 + 折线图） ----------
    ws2 = wb.create_sheet("资金曲线")
    row = 1
    for res in results:
        eq = res.equity
        ws2.cell(row=row, column=1, value=f"{res.name}（资金曲线，¥）").font = Font(
            name="微软雅黑", size=12, bold=True, color="1F4E79")
        row += 1
        ws2.cell(row=row, column=1, value="日期")
        ws2.cell(row=row, column=2, value="策略净值")
        ws2.cell(row=row, column=3, value="基准净值" if res.benchmark is not None else "")
        _style_header(ws2, row, 3)
        data_start = row + 1
        for j, (d, v) in enumerate(eq.items()):
            ws2.cell(row=data_start + j, column=1, value=str(d.date())).font = BODY_FONT
            ws2.cell(row=data_start + j, column=2, value=round(float(v), 2)).font = BODY_FONT
            if res.benchmark is not None and str(d.date()) in res.benchmark.index.astype(str):
                bv = res.benchmark.get(d)
                if bv is not None:
                    ws2.cell(row=data_start + j, column=3, value=round(float(bv), 2)).font = BODY_FONT
        chart = LineChart()
        chart.title = res.name
        chart.height, chart.width = 7.5, 16
        chart.y_axis.numFmt = "#,##0"
        eq_min = float(eq.min())
        if eq_min > 0:  # Y轴从数据范围开始，避免曲线被压扁在顶部
            chart.y_axis.scaling.min = round(eq_min * 0.92, 2)
        data_ref = Reference(ws2, min_col=2, max_col=3, min_row=data_start - 1,
                             max_row=data_start + len(eq) - 1)
        cats = Reference(ws2, min_col=1, min_row=data_start, max_row=data_start + len(eq) - 1)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats)
        for s in chart.series:
            s.smooth = False
            s.graphicalProperties.line.width = 15000
        ws2.add_chart(chart, f"E{row}")
        row = data_start + len(eq) + 2
    ws2.column_dimensions["A"].width = 12
    ws2.column_dimensions["B"].width = 12
    ws2.column_dimensions["C"].width = 12

    # ---------- 逐笔交易 ----------
    ws3 = wb.create_sheet("逐笔交易")
    headers3 = ["策略", "日期", "标的", "方向", "数量(股)", "成交价", "成交额(¥)", "费用(¥)", "盈亏(¥)"]
    for c, h in enumerate(headers3, 1):
        ws3.cell(row=1, column=c, value=h)
    _style_header(ws3, 1, len(headers3))
    r = 2
    for res in results:
        for t in res.trades:
            vals = [res.name, t["date"], t["symbol"], t["side"], t["shares"],
                    t["price"], t["value"], t["fee"], t["pnl"] if t["pnl"] is not None else ""]
            for c, v in enumerate(vals, 1):
                cell = ws3.cell(row=r, column=c, value=v)
                cell.font = BODY_FONT
                cell.border = BORDER
            if t["side"] == "买入":
                ws3.cell(row=r, column=4).font = Font(name="微软雅黑", size=10, color="C00000")
            else:
                ws3.cell(row=r, column=4).font = Font(name="微软雅黑", size=10, color="008000")
            r += 1
    for c, w in enumerate([24, 12, 10, 8, 10, 10, 12, 9, 10], 1):
        ws3.column_dimensions[get_column_letter(c)].width = w
    ws3.freeze_panes = "A2"
    ws3.auto_filter.ref = f"A1:I{r - 1}"

    # ---------- 说明 ----------
    ws4 = wb.create_sheet("说明")
    notes = [
        "指标口径",
        "· 总收益率 = 期末资产 / 初始资金 - 1；年化收益率按 252 个交易日折算。",
        "· 最大回撤 = 净值相对历史最高点的最大跌幅；夏普比率 = 日收益均值/日收益标准差 × √252（无风险利率取 0）。",
        "· 胜率 = 盈利的卖出笔数 / 全部卖出笔数；'—' 表示该策略期间没有卖出。",
        "交易规则",
        "· T 日收盘计算目标仓位，T+1 日开盘价成交（计入滑点），避免'偷看未来'。",
        "· A股/ETF 按整手（100股）取整；权重变动 < 0.5% 不交易，节约手续费。",
        fees_note,
        "风险提示",
        "· 回测结果基于历史数据，不代表未来收益。本报表仅为策略研究工具，不构成任何投资建议。",
        "· 实盘中还可能存在流动性不足、停牌、涨跌停无法成交、接口数据延迟等回测未覆盖的风险。",
    ]
    ws4["A1"] = "使用说明"
    ws4["A1"].font = TITLE_FONT
    for i, t in enumerate(notes, 3):
        cell = ws4.cell(row=i, column=1, value=t)
        cell.font = Font(name="微软雅黑", size=10, bold=(i == 3 or t in ("交易规则", "风险提示")))
        cell.alignment = Alignment(vertical="center", wrap_text=False)
    ws4.column_dimensions["A"].width = 110

    # 打印适配：横向压缩到一页宽（屏幕浏览不受影响）
    for sheet in (ws, ws2, ws3, ws4):
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)

    wb.save(path)
