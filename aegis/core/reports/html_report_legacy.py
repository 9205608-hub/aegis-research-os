"""HTML Report Generator — Visual Investment Research Report.

Generates a self-contained HTML file with embedded CSS and Chart.js
visualizations. Open in any browser, print to PDF.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ── Naming & formatting helpers ──────────────────────────────────

# Brand-correct segment/product names
_SEGMENT_NAME_MAP = {
    # Apple
    "iphone": "iPhone",
    "ipad": "iPad",
    "mac": "Mac",
    "imac": "iMac",
    "ipod": "iPod",
    "wearables homeand accessories": "Wearables, Home & Accessories",
    "wearables home and accessories": "Wearables, Home & Accessories",
    "wearableshomeandaccessories": "Wearables, Home & Accessories",
    # Geographic (common ISO / XBRL member codes)
    "us": "United States",
    "tw": "Taiwan",
    "cn": "China",
    "hk": "Hong Kong",
    "jp": "Japan",
    "kr": "South Korea",
    "de": "Germany",
    "gb": "United Kingdom",
    "in": "India",
    "sg": "Singapore",
    "greater china": "Greater China",
    "rest of asia pacific": "Rest of Asia Pacific",
    "americas": "Americas",
    "europe": "Europe",
    "japan": "Japan",
    "china including hong kong": "China (incl. Hong Kong)",
    "other countries": "Other Countries",
    # Product / segment generics
    "product": "Products",
    "service": "Services",
    "services": "Services",
    # NVIDIA specific
    "oemand other": "OEM & Other",
    "oem and other": "OEM & Other",
    "compute and networking": "Compute & Networking",
    "professional visualization": "Professional Visualization",
    "data center": "Data Center",
    "gaming": "Gaming",
    "automotive": "Automotive",
}


def _format_segment_name(raw: str) -> str:
    """Convert raw XBRL segment IDs to human-readable branded names.

    'iphone' → 'iPhone', 'wearables_homeand_accessories' → 'Wearables, Home & Accessories'
    """
    cleaned = raw.replace("_", " ").strip()
    lookup = cleaned.lower()
    if lookup in _SEGMENT_NAME_MAP:
        return _SEGMENT_NAME_MAP[lookup]
    # Fallback: title case, then fix common uppercase abbreviations
    result = cleaned.title()
    # Restore uppercase for known abbreviations (2-3 letter country/industry codes)
    import re
    _UPPER_ABBREVS = {"Oem", "Usa", "Emea", "Apac", "Roi", "Uk", "Eu", "Ai", "Gpu", "Cpu", "Hpc"}
    for abbr in _UPPER_ABBREVS:
        result = re.sub(rf'\b{abbr}\b', abbr.upper(), result)
    return result


_ZH_LABELS = {
    # Section headings (<h3>)
    "Research Process: Hypothesis Validation": "研究过程：假设验证",
    "Executive Summary": "执行摘要",
    "Investment Thesis": "投资论点",
    "Edge Assessment": "信息优势评估",
    "Risk Summary": "风险摘要",
    "Closing Assessment": "结论性评估",
    "Key Financials": "关键财务数据",
    "Key Metrics Radar": "关键指标雷达图",
    "Valuation Scenarios vs Price": "估值情景与股价对比",
    "Agent Analysis": "智能体分析",
    "Critic Review Board": "批评审核委员会",
    "Kill Criteria": "终止条件",
    "Monitoring Watchlist": "监控清单",
    "Open Research Questions": "待研究问题",
    "Consensus Estimates": "一致预期",
    "Earnings History (Beat / Miss)": "财报历史（超预期/不及预期）",
    "Earnings Call Insights": "财报电话会议洞察",
    "Peer Relative Valuation": "同业相对估值",
    "Forecast Bridge (DCF Projections)": "预测桥接（DCF 投影）",
    "Historical Financials": "历史财务",
    "Catalyst Timeline": "催化剂时间线",
    "Insider Trading Activity (12 Months)": "内部人交易活动（12 个月）",
    "News Sentiment": "新闻情绪",
    "Prediction Calibration Dashboard": "预测校准仪表板",
    "Prediction Calibration": "预测校准",
    "Segment Breakdown": "分部拆分",
    # Labels (<div class="label">...</div>)
    "Current Price": "当前股价",
    "Bear Case": "悲观情景",
    "Base Case": "基准情景",
    "Bull Case": "乐观情景",
    "Edge Type": "优势类型",
    "Durability": "持续性",
    "Why Market Is Wrong": "市场为何错误",
    "What Would Change My Mind": "何种证据会改变判断",
    "Decay Trigger": "优势衰减触发因素",
    "Low": "低",
    "Median": "中位",
    "High": "高",
    "Mean": "均值",
    "Consensus": "一致预期",
    "Upside": "上行空间",
    # Column headers / inline text
    "ANALYST PRICE TARGETS": "分析师目标价",
    "SEGMENT REVENUE BREAKDOWN": "分部收入拆分",
    "SHAREHOLDER RETURNS": "股东回报",
    "Actionable Gaps": "可行动缺口",
    "Active Predictions": "活跃预测",
    "Active": "活跃",
    "Agent": "智能体",
    "Analyst Focus Areas": "分析师关注领域",
    "Analysts": "分析师",
    "Assumption": "假设",
    "Base": "基准",
    "Bearish Signals": "看空信号",
    "Bullish Signals": "看多信号",
    "Bucket": "档位",
    "Calibrated": "已校准",
    "Calibration Score": "校准得分",
    "Cluster Detected": "检测到聚集",
    "Company": "公司",
    "Counterarguments": "反向论点",
    "Date": "日期",
    "Direction Accuracy": "方向准确率",
    "Direction": "方向",
    "Dividends": "股息",
    "Due Review": "待复核",
    "Due for Review": "待复核",
    "EV/EBITDA": "EV/EBITDA",
    "Event": "事件",
    "Forecast Accuracy": "预测准确性",
    "Gross Margin": "毛利率",
    "Guidance": "指引",
    "Hedging Signals": "对冲信号",
    "Impact": "影响",
    "Key Inferences": "关键推断",
    "Key Numbers from Management": "管理层关键数据",
    "Management Guidance": "管理层指引",
    "Mean Absolute Error": "平均绝对误差",
    "Metric": "指标",
    "Mkt Cap": "市值",
    "Name": "名称",
    "Negative": "负面",
    "No kill criteria defined": "未定义终止条件",
    "No monitorables": "无监控项",
    "None identified": "未识别",
    "None": "无",
    "Notable Language Changes": "值得关注的措辞变化",
    "Operating Margin": "营业利润率",
    "P/E Ratio": "市盈率",
    "Payout Ratio (vs FCF)": "派息率（占 FCF）",
    "Peer Median": "同业中位",
    "Period": "期间",
    "Positive": "正面",
    "Precision": "精确率",
    "Priority": "优先级",
    "Question": "问题",
    "ROIC": "ROIC",
    "Revenue": "营收",
    "Segment": "分部",
    "Share Buybacks": "股票回购",
    "Shareholder Yield": "股东回报率",
    "Shares": "股份",
    "Shocked": "冲击值",
    "Signal": "信号",
    "Surprise": "超预期",
    "System Bias": "系统偏差",
    "Title": "标题",
    "Topic": "主题",
    "Total Return": "总回报",
    "Total": "合计",
    "Type": "类型",
    "Uncertain": "不确定",
    "Value": "数值",
    "Year": "年份",
    # Risk / severity enum values (appear in critic and event cells)
    "HIGH": "高",
    "MEDIUM": "中",
    "LOW": "低",
    "CRITICAL": "严重",
    "WARN": "告警",
    "Critical": "严重",
    "Warning": "告警",
    "Filing": "财报披露",
    "Earnings": "业绩发布",
    "Launch": "产品发布",
    "quarterly": "季度",
    "annual": "年度",
    "EV/Revenue": "EV/营收",
    "wacc": "WACC",
    "terminal": "永续",
    "capex": "资本开支",
    "revenue_growth": "收入增速",
    "revenue": "收入",
    "growth": "增长",
    "rate": "比率",
    "Current": "当前",
    "effective": "有效",
    "buyback": "回购",
    "yield": "回报率",
    "Variant": "变体",
    "variant": "变体",
    # DCF / valuation terms
    "EBIT": "息税前利润",
    "NOPAT": "税后营业利润",
    "FCFF": "自由现金流",
    "FCFE": "股权自由现金流",
    "EBITDA": "EBITDA",
    "WACC": "加权资本成本",
    "CapEx": "资本开支",
    "ROIC": "投入资本回报率",
    "ROE": "净资产收益率",
    "P/E": "市盈率",
    "EV/Revenue": "EV/营收",
    "EV/EBITDA": "企业倍数",
    "DCF": "DCF",
    "CFO": "经营现金流",
    "CFF": "筹资现金流",
    "CFI": "投资现金流",
    "EPS": "每股收益",
    # Sensitivity table parameter names
    "capex_to_revenue": "资本开支率",
    "sbc_to_revenue": "股权激励率",
    "effective_tax_rate": "有效税率",
    "buyback_yield_annual": "回购收益率",
    "revenue_growth_rate": "收入增速",
    "terminal_growth_rate": "永续增长率",
    "operating_margin": "营业利润率",
    "wacc": "加权资本成本",
    # Enum values
    "Analytical": "分析型",
    "Informational": "信息型",
    "Short Term": "短期",
    "Medium Term": "中期",
    "Long Term": "长期",
    # Consensus period codes
    "CQ": "本季度",
    "NQ": "下季度",
    "FY_Current": "本财年",
    "FY_Next": "下财年",
    # Critic names
    "Logic": "逻辑",
    "Sector": "行业",
    "Market": "市场",
    "Evidence": "证据",
    "Accounting": "财务",
    "Valuation": "估值",
    "Cognitive Bias": "认知偏差",
    "Macro Consistency": "宏观一致性",
    # Badge text
    "Tone: ": "语调：",
    "Materiality: ": "重要性：",
    "issues": "项问题",
    # Page title
    "Aegis Research — ": "Aegis 投研 — ",
}


_ZH_FREE_TEXT_EXTRA = [
    # Additional tight replacements for leaky spots
    ("Swing Factor:", "核心变量："),
    (">Historical Valuation Range (", ">历史估值区间 ("),
    (">Year</th>", ">年份</th>"),
    (">Revenue</th>", ">营收</th>"),
    (">EBIT</th>", ">EBIT</th>"),
    (">NOPAT</th>", ">NOPAT</th>"),
    (">CapEx</th>", ">资本开支</th>"),
    (">FCFF</th>", ">自由现金流</th>"),
    (">PV(FCFF)</th>", ">折现值</th>"),
    # Confidence/Bias badge uppercase values
    (";color:#000\">HIGH<", ";color:#000\">高<"),
    (";color:#000\">LOW<", ";color:#000\">低<"),
    (";color:#000\">MEDIUM<", ";color:#000\">中<"),
    (";color:#000\">VERY_HIGH<", ";color:#000\">极高<"),
    (";color:#000\">VERY_LOW<", ";color:#000\">极低<"),
    # Critic bar suffix "N issues"
    (" issues</span>", " 项问题</span>"),
    # Peer chart title
    ("EV/EBITDA Comparison", "EV/EBITDA 对比"),
    # "3 issues" style
]

_ZH_FREE_TEXT = [
    # Header / footer / meta — ordered longest-first to avoid partial collisions
    ("Aegis Research OS — Investment Research Report", "Aegis 投研 OS — 投资研究报告"),
    ("Aegis Research OS v2", "Aegis 投研 OS v2"),
    ("This report is for research purposes only. Not investment advice.",
     "本报告仅供研究使用，不构成投资建议。"),
    ("Generated ", "生成于 "),
    ("Run ", "运行编号 "),
    ("Run:", "运行编号："),
    ("Confidence:", "置信度："),
    ("Bias:", "偏差检查："),
    ("Scenario Valuation", "估值情景"),
    ("Probability-Weighted", "概率加权"),
    # Status / confidence enum values (uppercased)
    ("PUBLISHED", "已发布"),
    ("DOWNGRADED", "已降级"),
    ("BLOCKED", "已拦截"),
    ("DRAFT", "草稿"),
    ("VERY_LOW", "极低"),
    ("VERY_HIGH", "极高"),
    (" HIGH", " 高"),  # leading space to avoid replacing in English narrative
    (" LOW", " 低"),
    (" MEDIUM", " 中"),
    ("PASSED", "通过"),
    ("WARNED", "告警"),
    ("FAILED", "失败"),
    # Scenario case labels (with probability suffix)
    (">Bear Case (", ">悲观情景 ("),
    (">Base Case (", ">基准情景 ("),
    (">Bull Case (", ">乐观情景 ("),
    (">Bear Case<", ">悲观情景<"),
    (">Base Case<", ">基准情景<"),
    (">Bull Case<", ">乐观情景<"),
    # Agent names (as they appear in <span class="agent-name">X</span>)
    ("Management Analyst", "管理层分析师"),
    ("Business Analyst", "业务分析师"),
    ("Valuation Analyst", "估值分析师"),
    ("Accounting Analyst", "财务分析师"),
    ("Risk Analyst", "风险分析师"),
    ("Variant Analyst", "变体分析师"),
    ("Research Director", "研究总监"),
    # Agent card stats suffixes
    (" obs · ", " 条观察 · "),
    (" inf", " 条推断"),
    # Critic names (as they appear in the Critic Review Board)
    ("Macro Consistency Critic", "宏观一致性批评员"),
    ("Logic Critic", "逻辑批评员"),
    ("Sector Critic", "行业批评员"),
    ("Market Critic", "市场批评员"),
    ("Evidence Critic", "证据批评员"),
    ("Accounting Critic", "财务批评员"),
    ("Valuation Critic", "估值批评员"),
    (" Critic</span>", " 批评员</span>"),  # catches leftover "逻辑 Critic"
    ("Critics:", "批评员："),
    # Strength / confidence bracketed tags
    ("[strong]", "[强]"),
    ("[moderate]", "[中等]"),
    ("[weak]", "[弱]"),
    # Sensitivity / DCF table
    (">revenue_growth_rate<", ">收入增速<"),
    (">terminal_growth_rate<", ">永续增长率<"),
    (">operating_margin<", ">营业利润率<"),
    (">wacc<", ">加权资本成本<"),
    (">tax_rate<", ">税率<"),
    (">capex_rate<", ">资本开支率<"),
    (">nwc_rate<", ">营运资本率<"),
    (">ROIC<", ">ROIC<"),
    (">EV/Revenue<", ">EV/营收<"),
    (">EV/EBITDA<", ">EV/EBITDA<"),
    (">P/E<", ">市盈率<"),
    # Counter/Inference headings inside agent cards are already in _ZH_LABELS
    # Agent emphasis / research process
    ("Research Process: Hypothesis Validation", "研究过程：假设验证"),
    # Frequent LLM emission patterns for A-share narratives
    (" variant", " 变体观点"),
    (" quarterly", " 季度"),
    (" covenant", " 契约条款"),
    # Misc
    (" Next<", " 下一步<"),
    ("<th>Year</th>", "<th>年份</th>"),
    # Edited report section headers (inside <strong> tags)
    ("<strong>Core Thesis:</strong>", "<strong>核心论点：</strong>"),
    ("<strong>Our Variant:</strong>", "<strong>我们的变体观点：</strong>"),
    ("<strong>Variant:</strong>", "<strong>变体观点：</strong>"),
    ("<strong>Why Now:</strong>", "<strong>为何是现在：</strong>"),
    ("<strong>Market's Story:</strong>", "<strong>市场的故事：</strong>"),
    ("<strong>Counter Thesis:</strong>", "<strong>反向论点：</strong>"),
    ("<strong>Key Characteristics:</strong>", "<strong>关键特征：</strong>"),
    # Earnings history columns (after tag-bounded replace for colon-suffixed labels)
    (">EPS Est.<", ">预期每股收益<"),
    (">EPS Act.<", ">实际每股收益<"),
    (">Rev Est.<", ">预期营收<"),
    (">Rev Act.<", ">实际营收<"),
    (">Surprise<", ">超预期<"),
    # Consensus table section headers
    (">Revenue Consensus<", ">营收一致预期<"),
    (">EPS Consensus<", ">每股收益一致预期<"),
    # Catalyst timeline event types (English fallback strings in html_report)
    ("SEC 10-K", "年度报告"),
    ("SEC 10-Q", "季度报告"),
    (" Due<", " 待发布<"),
    # Valuation chart title
    ("Aegis Research — ", "Aegis 投研 — "),
    # Historical valuation range label "... (median Xx, current Yx)"
    ("(median ", "(中位 "),
    (", current ", ", 当前 "),
    # Prediction calibration placeholder
    ("No post-mortems yet. Predictions are recorded automatically.",
     "暂无回顾分析。预测已自动记录。"),
    ("Review due predictions", "复核待检验预测"),
    # Out of scope / questions headers
    ("Out-of-Scope Follow-ups",
     "超出数据范围的追问"),
    ("Questions raised by agents that could not be answered from available data.",
     "智能体提出但无法从现有数据回答的问题。"),
    ("Addressing these would strengthen the thesis.",
     "解决这些问题将强化论点可信度。"),
    ("Questions agents would have asked of a domain expert but cannot be answered from our data sources.",
     "智能体本会向领域专家提问，但无法通过当前数据源获得答案。"),
    ("These are NOT analytical gaps.", "这些并非分析缺口。"),
    ("Quarterly trends (we ingest annual filings only)",
     "季度趋势（当前仅采集年报）"),
    ("Customer concentration (not disclosed in 10-K)",
     "客户集中度（年报未披露）"),
    # Narrative mixed-English keywords the LLM tends to emit
    (" CFO ", " 经营现金流 "),
    (" CFO，", " 经营现金流，"),
    (" CFO。", " 经营现金流。"),
    ("accruals ratio", "应计项目占比"),
    (" accruals", " 应计项目"),
    ("我们的variant", "我们的变体观点"),
    ("的variant是", "的变体观点是"),
    ("variant是", "变体观点是"),
    ("variant观点", "变体观点"),
    ("variant分析师", "变体分析师"),
    ("specialist分析", "专家分析"),
    ("specialist agents", "专家智能体"),
    ("specialists", "专家"),
    ("Yahoo Finance", "雅虎财经"),
    ("Sector Context Agent", "行业背景分析师"),
    ("revenue-based估值", "基于收入的估值"),
    ("covenant违约", "契约条款违约"),
    ("covenants", "契约条款"),
    ("covenant触发", "契约条款触发"),
    ("covenant", "契约条款"),
    ("核心variant", "核心变体观点"),
    ("variant", "变体"),
    ("late_expansion", "周期后段"),
    ("late expansion", "周期后段"),
    (" capex", " 资本开支"),
    ("capex_to_revenue", "资本开支率"),
    ("revenue_growth", "收入增速"),
    (" EBIT ", " 息税前利润 "),
    (" EBIT，", " 息税前利润，"),
    (" EBIT。", " 息税前利润。"),
    (" EBIT)", " 息税前利润)"),
    ("EV/Revenue", "企业价值/营收"),
    ("gross_margin", "毛利率"),
    ("operating_margin", "营业利润率"),
    ("net_margin", "净利率"),
    # Additional narrative leakage patterns
    ("trapped in working capital", "被营运资本占用"),
    ("working capital", "营运资本"),
    ("implied growth", "隐含增长"),
    ("key assumption", "关键假设"),
    ("margin of safety", "安全边际"),
    ("mean reversion", "均值回归"),
    ("price discovery", "价格发现"),
    ("downside protection", "下行保护"),
    ("upside potential", "上行空间"),
    # Scenario fallback labels (mechanical fallback when LLM narrative missing)
    (">Bear<", ">悲观<"),
    (">Base<", ">基准<"),
    (">Bull<", ">乐观<"),
]


def _localize_zh(html: str) -> str:
    """Post-process HTML to replace English UI labels with Chinese.

    Two passes:
      1. Tag-boundary replacements from _ZH_LABELS (safest)
      2. Free-text phrase replacements from _ZH_FREE_TEXT for headers/footers/badges
         where English appears as unique phrases outside agent narrative text.
    Longest keys first to prevent prefix collisions.
    """
    import re
    for en in sorted(_ZH_LABELS.keys(), key=len, reverse=True):
        zh = _ZH_LABELS[en]
        patterns = [
            (f"<h3>{re.escape(en)}</h3>", f"<h3>{zh}</h3>"),
            (f"<h3>{re.escape(en)} ", f"<h3>{zh} "),
            (f'<div class="label">{re.escape(en)}</div>', f'<div class="label">{zh}</div>'),
            (f'<div class="label">{re.escape(en)}<', f'<div class="label">{zh}<'),
            (f"<th>{re.escape(en)}</th>", f"<th>{zh}</th>"),
            (f"<td>{re.escape(en)}</td>", f"<td>{zh}</td>"),
            (f">{re.escape(en)}<", f">{zh}<"),
        ]
        for pat, rep in patterns:
            html = re.sub(pat, rep, html)

    # Free-text pass — simple string replace, longest-first
    for en, zh in sorted(_ZH_FREE_TEXT + _ZH_FREE_TEXT_EXTRA, key=lambda p: -len(p[0])):
        html = html.replace(en, zh)
    return html


def _build_timeliness_banner(meta_facts: dict | None, currency: str) -> str:
    """Render a warning banner if the fetched fiscal year is > 15 months stale.

    A-share analysis must be timely. If yfinance / data source hasn't yet
    ingested the latest annual report, surface this loudly to the reader
    rather than silently presenting a year-old report as 'latest'.
    """
    if not meta_facts:
        return ""
    fy = meta_facts.get("__fiscal_year")
    if not isinstance(fy, int):
        return ""
    from datetime import datetime as _dt
    now = _dt.now()
    # Months since fiscal year end (assumed Dec 31 for A-share calendar year)
    months_stale = (now.year - fy - 1) * 12 + now.month
    if months_stale <= 15:
        return ""
    if currency == "CNY":
        msg = (
            f"⚠ 数据时效提醒：最新可得财报为 FY{fy}（截至 {fy} 年 12 月 31 日），"
            f"距今约 {months_stale} 个月。数据源 (yfinance) 尚未收录更新的年报。"
            f"请注意本分析的价格动态基于实时行情，但财务基数可能落后 1 个财年。"
        )
    else:
        msg = (
            f"⚠ Data timeliness notice: latest available filing is FY{fy} "
            f"(~{months_stale} months old). Upstream data source has not yet "
            f"ingested a more recent annual report. Financial base is lagging; "
            f"price data is live."
        )
    return (
        f'<div style="margin-top:10px;padding:10px 14px;border-radius:8px;'
        f'background:rgba(251,146,60,0.15);border-left:4px solid #fb923c;'
        f'color:#fed7aa;font-size:12px;line-height:1.5">{msg}</div>'
    )


def _build_dq_banner(meta_facts: dict | None, currency: str) -> str:
    """Render a banner surfacing data quality issues to the reader.

    error-level: red banner (impossible values — DCF unreliable)
    warn-level:  orange banner (inconsistencies — review recommended)
    info-level:  not shown (edge cases, acceptable)
    """
    if not meta_facts:
        return ""
    dq_issues = meta_facts.get("__data_quality_issues", [])
    if not dq_issues:
        return ""

    errors = [i for i in dq_issues if i.get("severity") == "error"]
    warns = [i for i in dq_issues if i.get("severity") == "warn"]

    if not errors and not warns:
        return ""

    parts: list[str] = []

    if errors:
        is_zh = currency == "CNY"
        heading = "🔴 数据质量异常（严重）" if is_zh else "🔴 Data Quality Errors (Critical)"
        items = "".join(
            f"<li><code>{i['code']}</code>: {i['message']}</li>" for i in errors
        )
        parts.append(
            f'<div style="margin-top:10px;padding:10px 14px;border-radius:8px;'
            f'background:rgba(239,68,68,0.15);border-left:4px solid #ef4444;'
            f'color:#fca5a5;font-size:12px;line-height:1.5">'
            f'<strong>{heading}</strong><ul style="margin:4px 0 0 16px;padding:0">{items}</ul></div>'
        )

    if warns:
        is_zh = currency == "CNY"
        heading = "⚠ 数据质量提醒" if is_zh else "⚠ Data Quality Warnings"
        items = "".join(
            f"<li><code>{i['code']}</code>: {i['message']}</li>" for i in warns
        )
        parts.append(
            f'<div style="margin-top:10px;padding:10px 14px;border-radius:8px;'
            f'background:rgba(249,115,22,0.15);border-left:4px solid #f97316;'
            f'color:#fed7aa;font-size:12px;line-height:1.5">'
            f'<strong>{heading}</strong><ul style="margin:4px 0 0 16px;padding:0">{items}</ul></div>'
        )

    return "\n".join(parts)


def _status_label(status: str, currency: str) -> str:
    if currency != "CNY":
        return status.upper()
    return {"published": "已发布", "downgraded": "已降级", "blocked": "已拦截",
            "draft": "草稿"}.get(status, status.upper())


def _enum_label(val: str, currency: str) -> str:
    if currency != "CNY":
        return str(val).upper()
    return {"very_low": "极低", "low": "低", "medium": "中", "high": "高", "very_high": "极高",
            "passed": "通过", "warned": "告警", "failed": "失败", "unknown": "未知"}.get(val, str(val).upper())


def _format_hv_window(hv: dict | None) -> str:
    """Format historical valuation window label honoring actual data months.

    If actual months < requested years*12 (e.g. recent IPO), show the real span
    rather than the hardcoded window.
    """
    if not hv:
        return "5Y"
    months = hv.get("months")
    years = hv.get("years", 5)
    if months and months < years * 12:
        if months >= 12:
            y, m = divmod(months, 12)
            return f"{y}Y{m}M" if m else f"{y}Y"
        return f"{months}M"
    return f"{years}Y"


def _format_enum(val: Any) -> str:
    """Convert Python enum repr like 'EdgeType.ANALYTICAL' to 'Analytical'."""
    s = str(val)
    # Handle enum-style strings: "EnumClass.VALUE" → "VALUE"
    if "." in s:
        s = s.split(".")[-1]
    # Convert SCREAMING_SNAKE to Title Case
    return s.replace("_", " ").title()


def _build_hypothesis_validation_html(synthesized_thesis: Any | None, currency: str = "USD") -> str:
    """Build hypothesis validation section for the report."""
    if synthesized_thesis is None:
        return ""

    evolution = getattr(synthesized_thesis, "hypothesis_evolution", "")
    surprise = getattr(synthesized_thesis, "biggest_surprise", "")
    challengers = getattr(synthesized_thesis, "agents_that_challenged", [])
    validated = getattr(synthesized_thesis, "hypothesis_validated", True)

    if not evolution and not surprise:
        return ""

    is_zh = currency == "CNY"
    agent_zh = {
        "management_analyst": "管理层分析师", "business_analyst": "业务分析师",
        "valuation_analyst": "估值分析师", "accounting_analyst": "财务分析师",
        "risk_analyst": "风险分析师", "variant_analyst": "变体分析师",
    }
    status_color = "#22c55e" if validated else "#f97316"
    if is_zh:
        status_text = "假设已确认" if validated else "假设已修正"
        title = "研究过程：假设验证"
        challengers_label = "提出挑战的智能体"
        none_label = "无"
        evolution_label = "论点演化"
        surprise_label = "最大意外"
        challengers_text = "，".join(agent_zh.get(c, c.replace("_", " ")) for c in challengers) if challengers else none_label
    else:
        status_text = "Hypothesis Confirmed" if validated else "Hypothesis Revised"
        title = "Research Process: Hypothesis Validation"
        challengers_label = "Agents that challenged"
        none_label = "None"
        evolution_label = "How the thesis evolved"
        surprise_label = "Biggest surprise"
        challengers_text = ", ".join(c.replace("_", " ").title() for c in challengers) if challengers else none_label

    return f'''
    <div class="card grid-full" style="border-left:4px solid {status_color}">
      <h3>{title}</h3>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
        <span style="background:{status_color};color:white;padding:4px 12px;border-radius:4px;font-size:12px;font-weight:600">{status_text}</span>
        <span style="font-size:13px;color:var(--text2)">{challengers_label}: {challengers_text}</span>
      </div>
      <p><strong>{evolution_label}:</strong> {evolution}</p>
      {f'<p><strong>{surprise_label}:</strong> {surprise}</p>' if surprise else ''}
    </div>'''


def _build_executive_summary_html(
    decision: Any,
    edited_report: Any | None,
    research_directive: Any | None,
    edge_type: str,
    edge_dur: str,
    edge_why: str,
    edge_decay: str,
    edge_source: str,
    edge_change: str,
    currency: str = "USD",
    synthesized_thesis: Any | None = None,
) -> str:
    """Build executive summary HTML — uses editorial layer if available, falls back to basic."""
    is_zh = currency == "CNY"
    # Labels
    L = {
        "key_chars": "关键特征：" if is_zh else "Key Characteristics:",
        "exec_summary": "执行摘要" if is_zh else "Executive Summary",
        "inv_thesis": "投资论点" if is_zh else "Investment Thesis",
        "core_thesis": "核心论点" if is_zh else "Core Thesis",
        "our_variant": "我们的变体观点" if is_zh else "Our Variant",
        "variant": "变体观点" if is_zh else "Variant",
        "why_now": "为何是现在" if is_zh else "Why Now",
        "market_story": "市场的故事" if is_zh else "Market's Story",
        "key_disag": "关键分歧" if is_zh else "Key Disagreement",
        "counter_thesis": "反向论点" if is_zh else "Counter Thesis",
        "edge_assessment": "信息优势评估" if is_zh else "Edge Assessment",
        "edge_type": "优势类型" if is_zh else "Edge Type",
        "durability": "持续性" if is_zh else "Durability",
        "why_wrong": "市场为何错误" if is_zh else "Why Market Is Wrong",
        "change_mind": "何种证据会改变判断" if is_zh else "What Would Change My Mind",
        "decay_trigger": "优势衰减触发因素" if is_zh else "Decay Trigger",
        "risk_summary": "风险摘要" if is_zh else "Risk Summary",
        "closing": "结论性评估" if is_zh else "Closing Assessment",
        "na": "暂无" if is_zh else "N/A",
        "see_risk_below": "详见下方风险评估。" if is_zh else "See detailed risk assessment below.",
    }
    # Translate enum values (edge_type, edge_dur) for Chinese
    if is_zh:
        enum_zh = {"Analytical": "分析型", "Informational": "信息型", "Behavioral": "行为型",
                   "Short Term": "短期", "Medium Term": "中期", "Long Term": "长期",
                   "Structural": "结构性", "N/A": "暂无"}
        edge_type = enum_zh.get(edge_type, edge_type)
        edge_dur = enum_zh.get(edge_dur, edge_dur)

    if edited_report is not None:
        # LLM Editor Layer — rich, opinionated front page
        # Front page numbers
        numbers_html = ""
        for n in getattr(edited_report, "front_page_numbers", []):
            numbers_html += f'''
            <div class="front-number">
              <div class="front-number-value">{n.get("value", "")}</div>
              <div class="front-number-label">{n.get("label", "")}</div>
              <div class="front-number-context">{n.get("context", "")}</div>
            </div>'''

        # Salient characteristics from Research Director
        chars_html = ""
        if research_directive:
            chars = getattr(research_directive, "salient_characteristics", [])
            if chars:
                chars_items = "".join(f"<li>{c}</li>" for c in chars)
                chars_html = f'<div class="salient-chars"><strong>{L["key_chars"]}</strong><ul>{chars_items}</ul></div>'

        return f'''
    <div class="card grid-full chief-analyst-summary">
      <div class="headline-banner">
        <h2 class="report-headline">{getattr(edited_report, "headline", "")}</h2>
      </div>
      <div class="opening-paragraph">
        {getattr(edited_report, "opening_paragraph", "")}
      </div>
      {chars_html}
      <div class="front-numbers-grid">
        {numbers_html}
      </div>
      <div class="executive-summary-text">
        <h3>{L["exec_summary"]}</h3>
        <p>{getattr(edited_report, "executive_summary", "")}</p>
      </div>
    </div>

    <div class="card grid-full">
      <h3>{L["inv_thesis"]}</h3>
      <p><strong>{L["core_thesis"]}:</strong> {getattr(decision, "core_thesis", L["na"])}</p>
      <p><strong>{L["our_variant"]}:</strong> {getattr(decision, "my_variant", L["na"])}</p>
      <p><strong>{L["why_now"]}:</strong> {getattr(decision, "why_now", L["na"])}</p>
      <p><strong>{L["market_story"]}:</strong> {getattr(decision, "market_implied_story", L["na"])}</p>
      <p><strong>{L["key_disag"]}:</strong> {getattr(decision, "key_assumption_disagreement", L["na"])}</p>
      <p><strong>{L["counter_thesis"]}:</strong> {getattr(decision, "counter_thesis", L["na"])}</p>
    </div>

    <div class="card">
      <h3>{L["edge_assessment"]}</h3>
      <div class="edge-grid">
        <div class="edge-item"><div class="label">{L["edge_type"]}</div><div class="val">{edge_type}</div></div>
        <div class="edge-item"><div class="label">{L["durability"]}</div><div class="val">{edge_dur}</div></div>
        <div class="edge-item"><div class="label">{L["why_wrong"]}</div><div class="val">{edge_why[:300]}</div></div>
        <div class="edge-item"><div class="label">{L["change_mind"]}</div><div class="val">{edge_change[:200]}</div></div>
      </div>
    </div>

    <div class="card">
      <h3>{L["risk_summary"]}</h3>
      <p>{getattr(edited_report, "risk_summary", L["see_risk_below"])}</p>
    </div>

    <div class="card grid-full">
      <h3>{L["closing"]}</h3>
      <p>{getattr(edited_report, "closing_paragraph", "")}</p>
    </div>

    {_build_hypothesis_validation_html(synthesized_thesis, currency)}'''
    else:
        # Fallback: basic executive summary (original behavior)
        return f'''
    <div class="card grid-full">
      <h3>{L["exec_summary"]}</h3>
      <p><strong>{L["core_thesis"]}:</strong> {getattr(decision, "core_thesis", L["na"])}</p>
      <p><strong>{L["variant"]}:</strong> {getattr(decision, "my_variant", L["na"])}</p>
      <p><strong>{L["counter_thesis"]}:</strong> {getattr(decision, "counter_thesis", L["na"])}</p>
    </div>

    <div class="card">
      <h3>{L["edge_assessment"]}</h3>
      <div class="edge-grid">
        <div class="edge-item"><div class="label">{L["edge_type"]}</div><div class="val">{edge_type}</div></div>
        <div class="edge-item"><div class="label">{L["durability"]}</div><div class="val">{edge_dur}</div></div>
        <div class="edge-item"><div class="label">{L["why_wrong"]}</div><div class="val">{edge_why[:150]}</div></div>
        <div class="edge-item"><div class="label">{L["decay_trigger"]}</div><div class="val">{edge_decay}</div></div>
      </div>
    </div>'''


def _build_valuation_chart_js(hist_val: dict | None, currency: str = "USD") -> str:
    """Build Chart.js code for historical P/E and EV/EBITDA charts."""
    if not hist_val or not hist_val.get("dates"):
        return ""

    import json
    dates = json.dumps(hist_val["dates"])
    pe_data = json.dumps(hist_val.get("pe_ratio", []))
    ev_data = json.dumps(hist_val.get("ev_ebitda", []))
    pe_stats = hist_val.get("pe_stats", {})
    ev_stats = hist_val.get("ev_ebitda_stats", {})

    pe_median = pe_stats.get("median", 0)
    pe_p25 = pe_stats.get("p25", 0)
    pe_p75 = pe_stats.get("p75", 0)
    ev_median = ev_stats.get("median", 0)
    ev_p25 = ev_stats.get("p25", 0)
    ev_p75 = ev_stats.get("p75", 0)

    return f"""
(function() {{
  var valDates = {dates};
  var peData = {pe_data};
  var evData = {ev_data};

  function makeBand(vals, center, halfWidth) {{
    return vals.map(function() {{ return center; }});
  }}

  var peEl = document.getElementById('peChart');
  if (peEl && peData.length > 0) {{
    new Chart(peEl, {{
      type: 'line',
      data: {{
        labels: valDates,
        datasets: [
          {{label: 'P/E', data: peData, borderColor: 'rgba(59,130,246,1)', backgroundColor: 'rgba(59,130,246,0.1)', fill: false, tension: 0.3, pointRadius: 0, borderWidth: 2}},
          {{label: '{"上四分位" if currency == "CNY" else "P75"} ({pe_p75:.0f}x)', data: makeBand(peData, {pe_p75}, 0), borderColor: 'rgba(100,100,100,0.3)', borderDash: [4,4], pointRadius: 0, borderWidth: 1, fill: false}},
          {{label: '{"中位数" if currency == "CNY" else "Median"} ({pe_median:.0f}x)', data: makeBand(peData, {pe_median}, 0), borderColor: 'rgba(234,179,8,0.6)', borderDash: [6,3], pointRadius: 0, borderWidth: 1.5, fill: false}},
          {{label: '{"下四分位" if currency == "CNY" else "P25"} ({pe_p25:.0f}x)', data: makeBand(peData, {pe_p25}, 0), borderColor: 'rgba(100,100,100,0.3)', borderDash: [4,4], pointRadius: 0, borderWidth: 1, fill: '-1'}}
        ]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{title: {{display: true, text: '{"滚动市盈率 (TTM P/E)" if currency == "CNY" else "Trailing P/E"}', color: '#e2e8f0'}}, legend: {{display: false}}}},
        scales: {{
          x: {{ticks: {{color: '#94a3b8', maxTicksLimit: 10}}, grid: {{color: 'rgba(255,255,255,0.05)'}}}},
          y: {{ticks: {{color: '#94a3b8', callback: function(v) {{return v + 'x'}}}}, grid: {{color: 'rgba(255,255,255,0.05)'}}}}
        }}
      }}
    }});
  }}

  var evEl = document.getElementById('evChart');
  if (evEl && evData.length > 0) {{
    new Chart(evEl, {{
      type: 'line',
      data: {{
        labels: valDates,
        datasets: [
          {{label: 'EV/EBITDA', data: evData, borderColor: 'rgba(34,197,94,1)', backgroundColor: 'rgba(34,197,94,0.1)', fill: false, tension: 0.3, pointRadius: 0, borderWidth: 2}},
          {{label: '{"上四分位" if currency == "CNY" else "P75"} ({ev_p75:.0f}x)', data: makeBand(evData, {ev_p75}, 0), borderColor: 'rgba(100,100,100,0.3)', borderDash: [4,4], pointRadius: 0, borderWidth: 1, fill: false}},
          {{label: '{"中位数" if currency == "CNY" else "Median"} ({ev_median:.0f}x)', data: makeBand(evData, {ev_median}, 0), borderColor: 'rgba(234,179,8,0.6)', borderDash: [6,3], pointRadius: 0, borderWidth: 1.5, fill: false}},
          {{label: '{"下四分位" if currency == "CNY" else "P25"} ({ev_p25:.0f}x)', data: makeBand(evData, {ev_p25}, 0), borderColor: 'rgba(100,100,100,0.3)', borderDash: [4,4], pointRadius: 0, borderWidth: 1, fill: '-1'}}
        ]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{title: {{display: true, text: 'EV/EBITDA', color: '#e2e8f0'}}, legend: {{display: false}}}},
        scales: {{
          x: {{ticks: {{color: '#94a3b8', maxTicksLimit: 10}}, grid: {{color: 'rgba(255,255,255,0.05)'}}}},
          y: {{ticks: {{color: '#94a3b8', callback: function(v) {{return v + 'x'}}}}, grid: {{color: 'rgba(255,255,255,0.05)'}}}}
        }}
      }}
    }});
  }}
}})();
"""


def _build_peer_chart_js(peer_data: dict, currency: str = "USD") -> str:
    """Build Chart.js code for peer valuation bar charts."""
    if not peer_data or not peer_data.get("names"):
        return ""

    import json
    names = json.dumps(peer_data["names"])
    pe = json.dumps(peer_data.get("pe", []))
    eveb = json.dumps(peer_data.get("eveb", []))

    # First bar (subject company) gets accent color, rest get muted
    n = len(peer_data["names"])
    pe_colors = json.dumps(["rgba(59,130,246,0.8)"] + ["rgba(148,163,184,0.5)"] * (n - 1))
    ev_colors = json.dumps(["rgba(34,197,94,0.8)"] + ["rgba(148,163,184,0.5)"] * (n - 1))

    return f"""
(function() {{
  var peerNames = {names};
  var peerPE = {pe};
  var peerEV = {eveb};
  var peColors = {pe_colors};
  var evColors = {ev_colors};

  function makeChart(id, data, colors, title) {{
    var el = document.getElementById(id);
    if (!el || data.every(function(v){{return v===null||v===0}})) return;
    new Chart(el, {{
      type: 'bar',
      data: {{labels: peerNames, datasets: [{{data: data, backgroundColor: colors, borderRadius: 4}}]}},
      options: {{
        responsive: true, maintainAspectRatio: false, indexAxis: 'y',
        plugins: {{title: {{display: true, text: title, color: '#e2e8f0'}}, legend: {{display: false}}}},
        scales: {{
          x: {{ticks: {{color: '#94a3b8', callback: function(v){{return v+'x'}}}}, grid: {{color: 'rgba(255,255,255,0.05)'}}}},
          y: {{ticks: {{color: '#e2e8f0', font: {{size: 11}}}}, grid: {{display: false}}}}
        }}
      }}
    }});
  }}
  makeChart('peerPeChart', peerPE, peColors, '{"市盈率对比" if currency == "CNY" else "P/E Comparison"}');
  makeChart('peerEvChart', peerEV, evColors, '{"EV/EBITDA 对比" if currency == "CNY" else "EV/EBITDA Comparison"}');
}})();
"""


def _build_catalyst_timeline_card(catalyst_timeline: Any, currency: str = "USD") -> str:
    """Build catalyst timeline card for the HTML report."""
    if catalyst_timeline is None:
        return ""

    upcoming = catalyst_timeline.upcoming[:10]
    if not upcoming:
        return ""

    is_zh = currency == "CNY"
    if is_zh:
        type_labels = {
            "earnings": "业绩公告", "filing": "财报披露", "product_launch": "产品发布",
            "regulatory": "监管事件", "macro": "宏观", "management": "管理层",
            "dividend": "分红", "other": "事件",
        }
        impact_labels_zh = {
            "positive": '<span style="color:#22c55e">正面</span>',
            "negative": '<span style="color:#ef4444">负面</span>',
            "uncertain": '<span style="color:#eab308">不确定</span>',
        }
        header_label = "下次业绩"
        days_suffix = "天"
    else:
        type_labels = {
            "earnings": "Earnings", "filing": "Filing", "product_launch": "Launch",
            "regulatory": "Regulatory", "macro": "Macro", "management": "Mgmt",
            "dividend": "Dividend", "other": "Event",
        }
        impact_labels_zh = None
        header_label = "Next earnings"
        days_suffix = "d"

    rows = ""
    for e in upcoming:
        date_str = e.expected_date.strftime("%Y-%m-%d" if is_zh else "%b %d") if e.expected_date else ("未定" if is_zh else "TBD")
        days = e.days_until
        days_str = f"{days}{days_suffix}" if days is not None else ""

        urgency_colors = {
            "imminent": "#ef4444", "near_term": "#f97316",
            "medium_term": "#eab308", "long_term": "#94a3b8",
        }
        u_color = urgency_colors.get(e.urgency, "#94a3b8")

        if is_zh:
            impact_html = impact_labels_zh.get(e.impact_direction, "")
        else:
            impact_labels = {
                "positive": '<span style="color:#22c55e">Positive</span>',
                "negative": '<span style="color:#ef4444">Negative</span>',
                "uncertain": '<span style="color:#eab308">Uncertain</span>',
            }
            impact_html = impact_labels.get(e.impact_direction, "")

        # Translate event title prefixes (e.g. "SEC 10-K Annual Report", "Q1 Earnings")
        title = e.title[:60]
        if is_zh:
            title = (title
                .replace("SEC 10-K", "年度报告")
                .replace("SEC 10-Q", "季度报告")
                .replace("10-K", "年报")
                .replace("10-Q", "季报")
                .replace(" Annual Report", "年度报告")
                .replace(" Quarterly Report", "季度报告")
                .replace(" Earnings", "业绩公告")
                .replace(" Due", " 预计披露"))

        type_label = type_labels.get(e.event_type, e.event_type.replace("_", " ").title())
        rows += (
            f'<tr>'
            f'<td style="white-space:nowrap"><span style="color:{u_color};font-weight:600">{date_str}</span>'
            f' <span style="color:var(--muted);font-size:11px">{days_str}</span></td>'
            f'<td>{type_label}</td>'
            f'<td>{title}</td>'
            f'<td>{impact_html}</td>'
            f'</tr>'
        )

    next_earn = catalyst_timeline.next_earnings
    header_extra = ""
    if next_earn and next_earn.days_until is not None:
        header_extra = (
            f' <span style="font-size:12px;color:var(--muted);font-weight:400">'
            f'{header_label}: {next_earn.expected_date} ({next_earn.days_until}{days_suffix})</span>'
        )

    tl = catalyst_timeline.to_dict()
    if is_zh:
        stats_html = (
            f'<span>未来 30 天: {tl.get("events_30d", 0)} 起</span>'
            f'<span>未来 90 天: {tl.get("events_90d", 0)} 起</span>'
            f'<span>合计: {tl.get("event_count", 0)} 起</span>'
        )
        headers_html = "<tr><th>日期</th><th>类型</th><th>事件</th><th>影响</th></tr>"
        title_label = "催化剂时间线"
    else:
        stats_html = (
            f'<span>Next 30d: {tl.get("events_30d", 0)} events</span>'
            f'<span>Next 90d: {tl.get("events_90d", 0)} events</span>'
            f'<span>Total: {tl.get("event_count", 0)} events</span>'
        )
        headers_html = "<tr><th>Date</th><th>Type</th><th>Event</th><th>Impact</th></tr>"
        title_label = "Catalyst Timeline"
    return f"""<div class="card grid-full">
      <h3>{title_label}{header_extra}</h3>
      <div style="display:flex;gap:16px;margin-bottom:12px;font-size:13px;color:var(--muted)">
        {stats_html}
      </div>
      <table>
        {headers_html}
        {rows}
      </table>
    </div>"""


def _build_insider_trading_card(insider_summary: Any, currency: str = "USD") -> str:
    """Build insider trading activity card for the HTML report."""
    if insider_summary is None:
        return ""

    txns = getattr(insider_summary, "transactions", [])
    if not txns:
        return ""

    is_zh = currency == "CNY"
    ccy_sym = {"USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£", "JPY": "¥", "HKD": "HK$"}.get(currency, "$")

    buy_ct = getattr(insider_summary, "buy_count", 0)
    sell_ct = getattr(insider_summary, "sell_count", 0)
    buy_val = getattr(insider_summary, "total_buy_value", 0)
    sell_val = getattr(insider_summary, "total_sell_value", 0)
    net_val = getattr(insider_summary, "net_value", 0)
    sentiment = getattr(insider_summary, "sentiment", "neutral")
    cluster = getattr(insider_summary, "cluster_detected", False)
    notable = getattr(insider_summary, "notable_transactions", [])

    # Sentiment color
    sent_colors = {
        "bullish": "#22c55e", "bearish": "#ef4444",
        "mixed": "#eab308", "neutral": "#94a3b8",
    }
    sent_color = sent_colors.get(sentiment, "#94a3b8")

    net_color = "#22c55e" if net_val > 0 else "#ef4444" if net_val < 0 else "#94a3b8"
    if is_zh:
        net_label = "净买入" if net_val > 0 else "净卖出" if net_val < 0 else "中性"
        cluster_html = (
            '<span style="color:#ef4444;font-weight:600">检测到集中交易</span>'
            if cluster else '<span style="color:#94a3b8">无</span>'
        )
    else:
        net_label = "Net Buying" if net_val > 0 else "Net Selling" if net_val < 0 else "Neutral"
        cluster_html = (
            '<span style="color:#ef4444;font-weight:600">Cluster Detected</span>'
            if cluster else '<span style="color:#94a3b8">None</span>'
        )

    # Notable transactions table
    rows = ""
    for t in notable[:8]:
        t_name = getattr(t, "filer_name", "")
        t_title = getattr(t, "filer_title", "")
        t_type = getattr(t, "transaction_type", "")
        t_val = getattr(t, "total_value", 0)
        t_date = getattr(t, "transaction_date", "")
        t_shares = getattr(t, "shares", 0)

        if is_zh:
            type_label = {"P": "买入", "S": "卖出", "A": "授予"}.get(t_type, t_type)
        else:
            type_label = {"P": "Buy", "S": "Sell", "A": "Award"}.get(t_type, t_type)
        type_color = "#22c55e" if t_type == "P" else "#ef4444" if t_type == "S" else "#94a3b8"
        rows += (
            f'<tr>'
            f'<td>{t_name}</td>'
            f'<td style="font-size:12px;color:var(--muted)">{t_title}</td>'
            f'<td><span style="color:{type_color};font-weight:600">{type_label}</span></td>'
            f'<td style="text-align:right">{t_shares:,.0f}</td>'
            f'<td style="text-align:right;font-weight:600">{ccy_sym}{t_val:,.0f}</td>'
            f'<td>{t_date}</td>'
            f'</tr>'
        )

    table_html = ""
    if rows:
        if is_zh:
            _hdr_notable = f"显著交易 (&gt;{ccy_sym}100万)"
            _hdrs = ["姓名", "职务", "类型", "股数", "金额", "日期"]
        else:
            _hdr_notable = f"Notable Transactions (&gt;{ccy_sym}1M)"
            _hdrs = ["Name", "Title", "Type", "Shares", "Value", "Date"]
        table_html = f"""
      <h4 style="margin:12px 0 6px;font-size:13px;color:var(--muted)">{_hdr_notable}</h4>
      <table>
        <tr><th>{_hdrs[0]}</th><th>{_hdrs[1]}</th><th>{_hdrs[2]}</th><th style="text-align:right">{_hdrs[3]}</th>
            <th style="text-align:right">{_hdrs[4]}</th><th>{_hdrs[5]}</th></tr>
        {rows}
      </table>"""

    if is_zh:
        _title = "内部人交易活动 (近12个月)"
        _sent_lbl = "整体倾向"
        _buys_lbl = "买入"
        _sells_lbl = "卖出"
        _cluster_lbl = "集中交易"
    else:
        _title = "Insider Trading Activity (12 Months)"
        _sent_lbl = "Sentiment"
        _buys_lbl = "Buys"
        _sells_lbl = "Sells"
        _cluster_lbl = "Cluster"

    return f"""<div class="card grid-full">
      <h3>{_title}</h3>
      <div style="display:flex;gap:24px;margin-bottom:12px;font-size:13px">
        <span>{_sent_lbl}: <span style="color:{sent_color};font-weight:600">{sentiment.title()}</span></span>
        <span style="color:{net_color};font-weight:600">{net_label}: {ccy_sym}{abs(net_val):,.0f}</span>
        <span>{_buys_lbl}: {buy_ct} ({ccy_sym}{buy_val:,.0f})</span>
        <span>{_sells_lbl}: {sell_ct} ({ccy_sym}{sell_val:,.0f})</span>
        <span>{_cluster_lbl}: {cluster_html}</span>
      </div>{table_html}
    </div>"""


def _build_news_sentiment_card(insights: Any) -> str:
    """Build news sentiment analysis card for the HTML report."""
    if insights is None:
        return ""
    if getattr(insights, "article_count", 0) == 0:
        return ""

    sentiment = getattr(insights, "overall_sentiment", "neutral")
    score = getattr(insights, "sentiment_score", 0.0)
    trend = getattr(insights, "sentiment_trend", "stable")
    themes = getattr(insights, "key_themes", [])
    bulls = getattr(insights, "bullish_signals", [])
    bears = getattr(insights, "bearish_signals", [])
    summary = getattr(insights, "news_summary", "")
    materiality = getattr(insights, "materiality", "low")
    count = getattr(insights, "article_count", 0)
    date_range = getattr(insights, "date_range", "")

    sent_colors = {
        "positive": "#22c55e", "negative": "#ef4444",
        "mixed": "#eab308", "neutral": "#94a3b8",
    }
    sent_color = sent_colors.get(sentiment, "#94a3b8")
    trend_icons = {"improving": "\u2197", "deteriorating": "\u2198", "stable": "\u2192"}
    trend_icon = trend_icons.get(trend, "\u2192")

    # Score bar color
    if score > 0.2:
        score_color = "#22c55e"
    elif score < -0.2:
        score_color = "#ef4444"
    else:
        score_color = "#94a3b8"

    # Themes as tags
    theme_html = " ".join(
        f'<span style="background:#f1f5f9;padding:2px 8px;border-radius:4px;font-size:12px;margin-right:4px">{t}</span>'
        for t in themes[:5]
    )

    # Bullish/Bearish signals
    signal_html = ""
    if bulls or bears:
        bull_items = "".join(f'<li style="color:#22c55e">{s}</li>' for s in bulls[:4])
        bear_items = "".join(f'<li style="color:#ef4444">{s}</li>' for s in bears[:4])
        signal_html = f"""
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:10px;font-size:13px">
        <div>
          <div style="font-weight:600;margin-bottom:4px">Bullish Signals</div>
          <ul style="margin:0;padding-left:16px">{bull_items if bull_items else '<li style="color:#94a3b8">None identified</li>'}</ul>
        </div>
        <div>
          <div style="font-weight:600;margin-bottom:4px">Bearish Signals</div>
          <ul style="margin:0;padding-left:16px">{bear_items if bear_items else '<li style="color:#94a3b8">None identified</li>'}</ul>
        </div>
      </div>"""

    date_label = f" ({date_range})" if date_range else ""

    return f"""<div class="card grid-full">
      <h3>News Sentiment{date_label}</h3>
      <div style="display:flex;gap:24px;margin-bottom:10px;font-size:13px;align-items:center">
        <span>Sentiment: <span style="color:{sent_color};font-weight:600">{sentiment.title()}</span></span>
        <span>Score: <span style="color:{score_color};font-weight:600">{score:+.2f}</span></span>
        <span>Trend: {trend_icon} {trend.title()}</span>
        <span style="color:var(--muted)">{count} articles</span>
        <span style="color:var(--muted)">Materiality: {materiality}</span>
      </div>
      {f'<div style="margin-bottom:8px">{theme_html}</div>' if theme_html else ''}
      {f'<p style="font-size:13px;color:var(--muted);margin:8px 0">{summary}</p>' if summary else ''}
      {signal_html}
    </div>"""


def _build_calibration_card(currency: str = "USD") -> str:
    """Build calibration dashboard card from stored prediction data."""
    is_zh = currency == "CNY"
    try:
        from aegis.core.memory.calibration_loop import CalibrationLoop
        loop = CalibrationLoop()
        ctx = loop.get_calibration_context()

        total_pm = ctx.get("total_postmortems", 0)
        if total_pm == 0:
            if is_zh:
                return """<div class="card">
                  <h3>预测校准</h3>
                  <p style="color:var(--muted);font-size:13px">
                    暂无回顾分析。预测已自动记录。复核待检验预测以积累校准数据。</p>
                  <table>
                    <tr><td>活跃预测</td><td class="num">{}</td></tr>
                    <tr><td>待复核</td><td class="num">{}</td></tr>
                  </table>
                </div>""".format(ctx.get("active_predictions", 0), ctx.get("due_for_review", 0))
            return """<div class="card">
              <h3>Prediction Calibration</h3>
              <p style="color:var(--muted);font-size:13px">
                No post-mortems yet. Predictions are recorded automatically.
                Review due predictions to build calibration data.</p>
              <table>
                <tr><td>Active Predictions</td><td class="num">{}</td></tr>
                <tr><td>Due for Review</td><td class="num">{}</td></tr>
              </table>
            </div>""".format(ctx.get("active_predictions", 0), ctx.get("due_for_review", 0))

        # Build bucket precision table
        bp = ctx.get("bucket_precision", {})
        bucket_rows = ""
        for bucket in ["very_high", "high", "medium", "low", "very_low"]:
            stats = bp.get(bucket)
            if stats:
                prec = stats["precision"]
                n = stats["sample_size"]
                cal = "Yes" if stats["is_calibrated"] else "No"
                color = "#22c55e" if prec >= 0.5 else "#f97316" if prec >= 0.3 else "#ef4444"
                bucket_rows += (
                    f'<tr><td>{bucket.replace("_", " ").title()}</td>'
                    f'<td class="num" style="color:{color}">{prec:.0%}</td>'
                    f'<td class="num">{n}</td>'
                    f'<td class="num">{cal}</td></tr>'
                )

        # Forecast accuracy
        fa = ctx.get("forecast_accuracy")
        acc_html = ""
        if fa:
            da = fa.get("direction_accuracy", 0)
            mae = fa.get("mean_absolute_error_pct", 0)
            bias = fa.get("bias", "neutral")
            bias_color = "#22c55e" if bias == "neutral" else "#f97316"
            acc_html = f"""
              <div style="margin-top:12px">
                <strong>Forecast Accuracy</strong>
                <table>
                  <tr><td>Direction Accuracy</td><td class="num">{da:.0%}</td></tr>
                  <tr><td>Mean Absolute Error</td><td class="num">{mae:.1%}</td></tr>
                  <tr><td>System Bias</td><td class="num" style="color:{bias_color}">{bias.title()}</td></tr>
                </table>
              </div>"""

            sr = fa.get("scenario_hit_rate", {})
            if sr:
                acc_html += '<div style="margin-top:8px;font-size:12px;color:var(--muted)">'
                acc_html += "Scenario Hit Rate: "
                parts = [f"{k.title()} {v:.0%}" for k, v in sr.items() if v > 0]
                acc_html += " · ".join(parts)
                acc_html += "</div>"

        score = ctx.get("overall_calibration_score", 0)
        score_color = "#22c55e" if score >= 0.7 else "#eab308" if score >= 0.4 else "#ef4444"

        return f"""<div class="card">
          <h3>Prediction Calibration Dashboard</h3>
          <div style="display:flex;gap:24px;margin-bottom:12px">
            <div><span style="font-size:24px;font-weight:700;color:{score_color}">{score:.0%}</span>
              <div style="font-size:11px;color:var(--muted)">Calibration Score</div></div>
            <div><span style="font-size:24px;font-weight:700">{total_pm}</span>
              <div style="font-size:11px;color:var(--muted)">Post-Mortems</div></div>
            <div><span style="font-size:24px;font-weight:700">{ctx.get('active_predictions', 0)}</span>
              <div style="font-size:11px;color:var(--muted)">Active</div></div>
            <div><span style="font-size:24px;font-weight:700">{ctx.get('due_for_review', 0)}</span>
              <div style="font-size:11px;color:var(--muted)">Due Review</div></div>
          </div>
          {"<table><tr><th>Bucket</th><th>Precision</th><th>N</th><th>Calibrated</th></tr>" + bucket_rows + "</table>" if bucket_rows else ""}
          {acc_html}
        </div>"""

    except Exception:
        return ""


def generate_html_report(
    decision: Any,
    computed_metrics: dict[str, float],
    market_data: dict[str, float],
    agent_judgments: list[Any],
    critic_results: list[Any],
    meta_facts: dict[str, Any] | None = None,
    dcf_projections: list[dict] | None = None,
    sensitivity_table: dict | None = None,
    sensitivity_rankings: list[dict] | None = None,
    segment_projections: dict[str, list[dict]] | None = None,
    entity_name: str | None = None,
    segment_detail: dict[str, Any] | None = None,
    consensus_estimates: list[Any] | None = None,
    earnings_history: list[Any] | None = None,
    peer_fundamentals: list[Any] | None = None,
    price_target_consensus: dict[str, Any] | None = None,
    edited_report: Any | None = None,
    research_directive: Any | None = None,
    synthesized_thesis: Any | None = None,
    earnings_call_insights: Any | None = None,
    historical_valuation: dict | None = None,
    catalyst_timeline: Any | None = None,
    insider_summary: Any | None = None,
    news_sentiment_insights: Any | None = None,
    scenarios: dict | None = None,
) -> str:
    """Generate a complete HTML investment research report."""
    entity_id_raw = getattr(decision, "entity_id", "Unknown")
    entity = entity_name or entity_id_raw.replace("_", " ").title()
    run_id = getattr(decision, "run_id", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Currency symbol for display — prefer explicit scenarios arg, fall back to decision
    scenarios_raw = scenarios or getattr(decision, "scenarios", {}) or {}
    _currency = scenarios_raw.get("currency", None) if isinstance(scenarios_raw, dict) else None
    if not _currency:
        # A-share entity_id is a 6-digit numeric code
        _clean_eid = str(entity_id_raw).replace(".SS", "").replace(".SZ", "").strip()
        if len(_clean_eid) == 6 and _clean_eid.isdigit():
            _currency = "CNY"
        else:
            _currency = "USD"
    ccy = {"USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£", "JPY": "¥", "HKD": "HK$"}.get(_currency, "$")
    ccy_unit = "B" if _currency == "USD" else ("亿" if _currency == "CNY" else "B")
    ccy_divisor = 1e9 if _currency != "CNY" else 1e8  # Chinese reports use 亿 (100M)

    # Extract values
    bear = getattr(decision, "bear_case_value", 0) or 0
    base = getattr(decision, "base_case_value", 0) or 0
    bull = getattr(decision, "bull_case_value", 0) or 0
    price = market_data.get("current_price", 0)
    sc_narr = getattr(decision, "scenario_narratives", {})
    sc_prob = getattr(decision, "scenario_probabilities", {})
    pw_val = getattr(decision, "probability_weighted_value", None)
    swing_factor = getattr(decision, "primary_swing_factor", "")
    confidence = getattr(decision, "confidence_bucket", "medium")
    status = getattr(decision, "publishing_status", "draft")
    bias_status = getattr(decision, "bias_check_status", "unknown")

    edge = getattr(decision, "edge_assessment", None)
    edge_type = _format_enum(getattr(edge, "primary_edge_type", "N/A")) if edge else "N/A"
    edge_source = getattr(edge, "edge_source", "") if edge else ""
    edge_dur = _format_enum(getattr(edge, "edge_durability", "")) if edge else ""
    edge_decay = getattr(edge, "edge_decay_trigger", "") if edge else ""
    edge_why = getattr(edge, "why_market_is_wrong", "") if edge else ""
    edge_change = getattr(edge, "what_would_change_my_mind", "") if edge else ""

    # Metrics for radar — cap at 100% for display
    gm = min(computed_metrics.get("gross_margin", 0) * 100, 100)
    om = min(computed_metrics.get("operating_margin", 0) * 100, 100)
    nm = min(computed_metrics.get("net_margin", 0) * 100, 100)
    roic = min(computed_metrics.get("roic", 0) * 100, 100)
    roe = min(computed_metrics.get("roe", 0) * 100, 100)  # Apple ROE=164% → cap at 100

    # Confidence badge
    conf_colors = {"very_low": "#ef4444", "low": "#f97316", "medium": "#eab308", "high": "#22c55e", "very_high": "#06b6d4"}
    conf_color = conf_colors.get(confidence, "#9ca3af")

    status_colors = {"published": "#22c55e", "downgraded": "#f97316", "blocked": "#ef4444", "draft": "#9ca3af"}
    status_color = status_colors.get(status, "#9ca3af")

    # Critic bars
    critic_zh_map = {
        "logic": "逻辑批评员",
        "accounting": "财务批评员",
        "evidence": "证据批评员",
        "sector": "行业批评员",
        "cognitive_bias": "认知偏差批评员",
        "macro_consistency": "宏观一致性批评员",
        "market": "市场批评员",
        "valuation": "估值批评员",
        "numeric_consistency": "数值一致性批评员",
        "narrative_fact": "叙述事实核查员",
        "llm_judge": "LLM数值审核员",
    }
    critic_html = ""
    for cr in critic_results:
        ct = getattr(cr, "critic_type", "unknown")
        # Normalize: strip trailing "_critic" suffix if present
        ct_key = ct[:-7] if ct.endswith("_critic") else ct
        n_issues = len(getattr(cr, "issues", []))
        blocked = getattr(cr, "block_publish", False)
        risk = getattr(cr, "overall_risk", "low")
        bar_color = "#ef4444" if blocked else "#f97316" if risk == "medium" else "#22c55e"
        if _currency == "CNY":
            label = critic_zh_map.get(ct_key, ct_key.replace("_", " ").title() + "批评员")
            count_suffix = "项问题"
        else:
            label = ct_key.replace("_", " ").title() + " Critic"
            count_suffix = "issues"
        critic_html += f'''
        <div class="critic-row">
          <span class="critic-name">{label}</span>
          <div class="critic-bar-bg">
            <div class="critic-bar" style="width:{min(100, n_issues*15+10)}%;background:{bar_color}"></div>
          </div>
          <span class="critic-count">{n_issues} {count_suffix}</span>
        </div>'''

    # Earnings call insights
    ec_html = ""
    if earnings_call_insights:
        eci = earnings_call_insights
        tone = getattr(eci, "overall_tone", "neutral")
        tone_color = {"confident": "var(--green)", "cautiously_optimistic": "var(--yellow)",
                      "defensive": "var(--red)", "neutral": "var(--muted)"}.get(tone, "var(--muted)")
        materiality = getattr(eci, "materiality", "medium")
        mat_color = {"high": "var(--red)", "medium": "var(--yellow)", "low": "var(--muted)"}.get(materiality, "var(--muted)")

        # Guidance table
        guidance_rows = ""
        for g in getattr(eci, "guidance_items", []):
            dir_color = {"raised": "var(--green)", "lowered": "var(--red)", "maintained": "var(--muted)",
                         "new": "var(--accent)", "withdrawn": "var(--red)"}.get(g.get("direction", ""), "var(--muted)")
            guidance_rows += f'<tr><td>{g.get("metric", "")}</td><td>{g.get("guidance_text", "")}</td><td style="color:{dir_color};font-weight:600">{g.get("direction", "").upper()}</td></tr>'

        guidance_table = f'''<table class="data-table"><thead>
            <tr><th>Metric</th><th>Guidance</th><th>Direction</th></tr>
            </thead><tbody>{guidance_rows}</tbody></table>''' if guidance_rows else ""

        # Analyst focus
        focus_items = "".join(f"<li>{t}</li>" for t in getattr(eci, "analyst_focus_topics", []))
        focus_html = f"<ul style='margin:0;padding-left:20px'>{focus_items}</ul>" if focus_items else ""

        # Hedging signals
        hedge_rows = ""
        for h in getattr(eci, "hedging_signals", []):
            hedge_rows += f'<tr><td style="font-weight:600">{h.get("topic", "")}</td><td>{h.get("signal", "")}</td></tr>'
        hedge_table = f'''<table class="data-table"><thead>
            <tr><th>Topic</th><th>Signal</th></tr>
            </thead><tbody>{hedge_rows}</tbody></table>''' if hedge_rows else ""

        # Key numbers
        nums = getattr(eci, "management_key_numbers", [])
        nums_html = "<br>".join(f"• {n}" for n in nums) if nums else ""

        # Language changes
        lang_changes = getattr(eci, "notable_language_changes", [])
        lang_html = "<br>".join(f"• {c}" for c in lang_changes) if lang_changes else ""

        ec_html = f'''
        <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
          <span class="badge" style="background:{tone_color};color:#000">Tone: {tone.replace("_", " ").title()}</span>
          <span class="badge" style="background:{mat_color};color:#000">Materiality: {materiality.upper()}</span>
          <span class="badge" style="background:var(--surface2)">{getattr(eci, "quarter", "")} {getattr(eci, "year", "")}</span>
        </div>
        <p style="color:var(--text);margin-bottom:16px">{getattr(eci, "call_summary", "")}</p>
        {f'<h4 style="margin:16px 0 8px">Management Guidance</h4>{guidance_table}' if guidance_table else ""}
        {f'<h4 style="margin:16px 0 8px">Analyst Focus Areas</h4>{focus_html}' if focus_html else ""}
        {f'<h4 style="margin:16px 0 8px">Hedging Signals</h4>{hedge_table}' if hedge_table else ""}
        {f'<h4 style="margin:16px 0 8px">Key Numbers from Management</h4><div style="color:var(--muted);font-size:13px">{nums_html}</div>' if nums_html else ""}
        {f'<h4 style="margin:16px 0 8px">Notable Language Changes</h4><div style="color:var(--muted);font-size:13px">{lang_html}</div>' if lang_html else ""}
        '''

    # Open research questions from agent follow-ups — split into:
    # 1) actionable gaps (data we should have but didn't surface to the agent)
    # 2) out-of-scope (quarterly trends, customer concentration, policy text —
    #    structurally unavailable from annual XBRL filings; calling these out
    #    explicitly prevents readers from thinking we failed to answer them)
    oq_list = getattr(decision, "open_questions", [])
    open_q_html = ""
    if oq_list:
        # Backfill out_of_scope tag for older caches that pre-date the
        # orchestrator's tagging logic.
        try:
            from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
            class _FQ:
                def __init__(self, q):
                    self.question = q.get("question", "")
                    self.data_key = q.get("data_key", "")
                    self.data_type = q.get("data_type", "")
            for q in oq_list:
                if "out_of_scope" not in q:
                    cls = AutoResearchOrchestrator._classify_out_of_scope(_FQ(q))
                    if cls:
                        q["out_of_scope"] = cls
        except Exception:
            pass

        actionable = [q for q in oq_list if not q.get("out_of_scope")]
        oos = [q for q in oq_list if q.get("out_of_scope")]

        is_zh_oq = _currency == "CNY"
        agent_zh_map = {
            "management_analyst": "管理层分析师",
            "business_analyst": "业务分析师",
            "valuation_analyst": "估值分析师",
            "accounting_analyst": "财务分析师",
            "risk_analyst": "风险分析师",
            "variant_analyst": "变体分析师",
            "research_director": "研究总监",
            "sector_context_agent": "行业背景分析师",
        }
        priority_zh_map = {"high": "高", "medium": "中", "low": "低"}

        def _row(oq):
            agent_raw = oq.get("agent", "")
            if is_zh_oq:
                agent_label = agent_zh_map.get(agent_raw, agent_raw.replace("_", " "))
            else:
                agent_label = agent_raw.replace("_", " ").title()
            pri = oq.get("priority", "medium")
            pri_color = {"high": "var(--red)", "medium": "var(--yellow)", "low": "var(--muted)"}.get(pri, "var(--muted)")
            pri_display = priority_zh_map.get(pri, pri.upper()) if is_zh_oq else pri.upper()
            return f'''<tr>
              <td style="color:{pri_color};font-weight:600">{pri_display}</td>
              <td>{agent_label}</td>
              <td>{oq.get("question", "")}</td>
            </tr>'''

        parts = []
        if actionable:
            rows = "".join(_row(q) for q in actionable)
            if is_zh_oq:
                h4_label = "可行动缺口"
                headers = "<tr><th>优先级</th><th>智能体</th><th>问题</th></tr>"
            else:
                h4_label = "Actionable Gaps"
                headers = "<tr><th>Priority</th><th>Agent</th><th>Question</th></tr>"
            parts.append(f'''<h4 style="margin:8px 0 6px">{h4_label}</h4>
              <table class="data-table"><thead>
                {headers}
              </thead><tbody>{rows}</tbody></table>''')
        if oos:
            from collections import defaultdict
            by_reason: dict[str, list] = defaultdict(list)
            for q in oos:
                by_reason[q.get("out_of_scope", "unknown")].append(q)
            if is_zh_oq:
                reason_label = {
                    "quarterly_data_not_available": "季度趋势（当前仅采集年报）",
                    "customer_concentration_not_disclosed": "客户集中度（年报未披露）",
                    "qualitative_text_not_extracted": "定性政策/叙述文本（系统仅提取数值）",
                    "balance_sheet_subline_not_disclosed": "资产负债表细项（年报未单列）",
                    "unit_pricing_not_disclosed": "单位定价/销量（财报未披露）",
                    "segment_history_not_available": "分部多年趋势（仅当年分部 + 合并多年数据）",
                }
                more_tmpl = "+ 另有 {n} 条"
                h4_label = "超出数据范围的追问"
                blurb = "智能体本会向领域专家提问，但无法通过当前数据源获得答案。这些并非分析缺口。"
            else:
                reason_label = {
                    "quarterly_data_not_available": "Quarterly trends (we ingest annual filings only)",
                    "customer_concentration_not_disclosed": "Customer concentration (not disclosed in 10-K)",
                    "qualitative_text_not_extracted": "Qualitative policy / narrative text (we extract numeric facts)",
                    "balance_sheet_subline_not_disclosed": "Sub-line balance-sheet detail (not broken out in filings)",
                    "unit_pricing_not_disclosed": "Unit pricing / volume (not in financial filings)",
                    "segment_history_not_available": "Multi-year segment trends (we have current-year segments + multi-year consolidated only)",
                }
                more_tmpl = "+ {n} more"
                h4_label = "Out-of-Scope Follow-ups"
                blurb = "Questions agents would have asked of a domain expert but cannot be answered from our data sources. These are NOT analytical gaps."
            oos_rows = ""
            for reason, qs in by_reason.items():
                label = reason_label.get(reason, reason.replace("_", " "))
                qlist = "<ul style='margin:4px 0 8px 16px;padding:0'>" + "".join(
                    f"<li style='font-size:13px;color:var(--muted)'>{q.get('question','')}</li>"
                    for q in qs[:5]
                ) + "</ul>"
                more = f"<div style='font-size:12px;color:var(--muted)'>{more_tmpl.format(n=len(qs)-5)}</div>" if len(qs) > 5 else ""
                oos_rows += f"<div style='margin-bottom:10px'><strong style='font-size:13px'>{label}</strong>{qlist}{more}</div>"
            parts.append(f'''<h4 style="margin:14px 0 6px">{h4_label}</h4>
              <div style='font-size:12px;color:var(--muted);margin-bottom:6px'>
                {blurb}
              </div>
              {oos_rows}''')
        open_q_html = "".join(parts)

    # Agent summaries
    agent_html = ""
    for j in agent_judgments:
        name = getattr(j, "agent_name", "agent")
        inferences = getattr(j, "inferences", [])
        counterargs = getattr(j, "counterarguments", [])
        obs_count = len(getattr(j, "observations", []))
        inf_count = len(inferences)

        inf_items = ""
        for inf in inferences[:3]:
            text = getattr(inf, "text", "")
            conf = getattr(inf, "confidence", "medium")
            conf_dot = "🟢" if conf == "high" else "🟡" if conf == "medium" else "🔴"
            inf_items += f'<li>{conf_dot} {text}</li>'

        agent_zh_name_map = {
            "management_analyst": "管理层分析师",
            "business_analyst": "业务分析师",
            "valuation_analyst": "估值分析师",
            "accounting_analyst": "财务分析师",
            "risk_analyst": "风险分析师",
            "variant_analyst": "变体分析师",
            "research_director": "研究总监",
            "sector_context_agent": "行业背景分析师",
        }
        strength_zh_map = {"strong": "强", "moderate": "中等", "weak": "弱"}

        counter_items = ""
        for ca in counterargs[:2]:
            text = getattr(ca, "text", "")
            strength = getattr(ca, "strength", "moderate")
            strength_display = strength_zh_map.get(strength, strength) if _currency == "CNY" else strength
            counter_items += f'<li><em>[{strength_display}]</em> {text}</li>'

        if _currency == "CNY":
            name_display = agent_zh_name_map.get(name, name.replace("_", " ").title())
            stats_display = f'{obs_count} 条观察 · {inf_count} 条推断'
            inf_heading = "关键推断"
            counter_heading = "反向论点"
        else:
            name_display = name.replace("_", " ").title()
            stats_display = f'{obs_count} obs · {inf_count} inf'
            inf_heading = "Key Inferences"
            counter_heading = "Counterarguments"

        agent_html += f'''
        <details class="agent-card">
          <summary class="agent-header">
            <span class="agent-name">{name_display}</span>
            <span class="agent-stats">{stats_display}</span>
          </summary>
          <div class="agent-body">
            <h4>{inf_heading}</h4>
            <ul>{inf_items}</ul>
            <h4>{counter_heading}</h4>
            <ul class="counter-list">{counter_items}</ul>
          </div>
        </details>'''

    # Kill criteria & monitorables
    kill_html = ""
    for k in getattr(decision, "kill_criteria", [])[:7]:
        desc = k.get("description", "") if isinstance(k, dict) else str(k)
        kill_html += f'<tr><td class="kill-icon">⛔</td><td>{desc}</td></tr>'

    monitor_html = ""
    for m in getattr(decision, "monitorables", [])[:8]:
        desc = m.get("description", "") if isinstance(m, dict) else str(m)
        freq = m.get("check_frequency", "") if isinstance(m, dict) else ""
        monitor_html += f'<tr><td class="monitor-icon">👁</td><td>{desc}</td><td class="freq">{freq}</td></tr>'

    # Key financials table
    fin_html = ""
    if meta_facts:
        # Use currency-aware labels: CNY → Chinese labels + 亿 unit, USD → English + B
        if _currency == "CNY":
            fin_rows = [
                ("营收", meta_facts.get("revenue", 0)),
                ("净利润", meta_facts.get("net_income", 0)),
                ("EBITDA", meta_facts.get("ebitda", 0)),
                ("自由现金流", meta_facts.get("free_cash_flow", 0)),
                ("经营现金流", meta_facts.get("operating_cash_flow", 0)),
            ]
        else:
            fin_rows = [
                ("Revenue", meta_facts.get("revenue", 0)),
                ("Net Income", meta_facts.get("net_income", 0)),
                ("EBITDA", meta_facts.get("ebitda", 0)),
                ("FCF", meta_facts.get("free_cash_flow", 0)),
                ("Operating Cash Flow", meta_facts.get("operating_cash_flow", 0)),
            ]
        for label, val in fin_rows:
            if val:
                fin_html += f'<tr><td>{label}</td><td class="num">{ccy}{val/ccy_divisor:.1f}{ccy_unit}</td></tr>'

    # Shareholder returns section
    shareholder_html = ""
    if meta_facts:
        buybacks = meta_facts.get("share_buybacks", 0)
        dividends = meta_facts.get("dividends_paid", 0)
        total_return = meta_facts.get("total_shareholder_return_cash", 0)
        if buybacks or dividends:
            shareholder_html += '<p style="font-size:13px;color:var(--accent);margin:16px 0 8px;font-weight:600">SHAREHOLDER RETURNS</p>'
            shareholder_html += '<table>'
            if buybacks:
                shareholder_html += f'<tr><td>Share Buybacks</td><td class="num">{ccy}{buybacks/ccy_divisor:.1f}{ccy_unit}</td></tr>'
            if dividends:
                shareholder_html += f'<tr><td>Dividends</td><td class="num">{ccy}{dividends/ccy_divisor:.1f}{ccy_unit}</td></tr>'
            if total_return:
                shareholder_html += f'<tr><td><strong>Total Return</strong></td><td class="num"><strong>{ccy}{total_return/ccy_divisor:.1f}{ccy_unit}</strong></td></tr>'
            fcf = meta_facts.get("free_cash_flow", 0)
            if total_return and fcf:
                payout = total_return / fcf * 100
                shareholder_html += f'<tr><td>Payout Ratio (vs FCF)</td><td class="num">{payout:.0f}%</td></tr>'
            if total_return and price:
                mkt_cap = market_data.get("market_cap", 0)
                if mkt_cap:
                    syield = total_return / mkt_cap * 100
                    shareholder_html += f'<tr><td>Shareholder Yield</td><td class="num">{syield:.1f}%</td></tr>'
            shareholder_html += '</table>'

    # Historical trend data for chart
    hist_chart_js = ""
    hist_revenue = meta_facts.get("__historical_revenue", {}) if meta_facts else {}
    if hist_revenue and len(hist_revenue) >= 2:
        years = sorted(hist_revenue.keys())
        rev_values = [hist_revenue[y] / ccy_divisor for y in years]
        year_labels = [str(y) for y in years]

        # Also try to get net income history
        # US GAAP uses "us-gaap:NetIncomeLoss" / "us-gaap:OperatingIncomeLoss"
        # CAS (A-share) uses Chinese names: "净利润" / "营业利润"
        hist_data = meta_facts.get("__historical_data", {}) if meta_facts else {}
        ni_values = []
        oi_values = []
        for y in years:
            yd = hist_data.get(y, {})
            ni = yd.get("us-gaap:NetIncomeLoss", 0) or yd.get("净利润", 0)
            oi = yd.get("us-gaap:OperatingIncomeLoss", 0) or yd.get("营业利润", 0)
            ni_values.append(ni / ccy_divisor if ni else 0)
            oi_values.append(oi / ccy_divisor if oi else 0)

        # Currency-aware chart labels (e.g. "营收 (¥亿)" vs "Revenue ($B)")
        if _currency == "CNY":
            _lbl_rev = f"营收 ({ccy}{ccy_unit})"
            _lbl_oi = f"营业利润 ({ccy}{ccy_unit})"
            _lbl_ni = f"净利润 ({ccy}{ccy_unit})"
        else:
            _lbl_rev = f"Revenue ({ccy}{ccy_unit})"
            _lbl_oi = f"Operating Income ({ccy}{ccy_unit})"
            _lbl_ni = f"Net Income ({ccy}{ccy_unit})"

        hist_chart_js = f"""
new Chart(document.getElementById('histChart'), {{
  type: 'bar',
  data: {{
    labels: {year_labels},
    datasets: [
      {{label:'{_lbl_rev}',data:{[round(v,1) for v in rev_values]},backgroundColor:'rgba(59,130,246,0.7)',borderRadius:4}},
      {{label:'{_lbl_oi}',data:{[round(v,1) for v in oi_values]},backgroundColor:'rgba(34,197,94,0.7)',borderRadius:4}},
      {{label:'{_lbl_ni}',data:{[round(v,1) for v in ni_values]},backgroundColor:'rgba(168,85,247,0.7)',borderRadius:4}}
    ]
  }},
  options: {{
    responsive:true, maintainAspectRatio:false,
    plugins:{{legend:{{labels:{{color:'#94a3b8',font:{{size:11}}}}}}}},
    scales:{{
      y:{{beginAtZero:true,grid:{{color:'rgba(148,163,184,0.1)'}},ticks:{{color:'#94a3b8',callback:v=>'{ccy}'+v+'{ccy_unit}'}}}},
      x:{{ticks:{{color:'#94a3b8'}},grid:{{display:false}}}}
    }}
  }}
}});"""

    # ── Consensus Estimates table ──────────────────────────────────
    consensus_html = ""
    if consensus_estimates:
        # Group by metric
        by_metric: dict[str, list] = {}
        for est in consensus_estimates:
            m = getattr(est, "metric", "unknown")
            by_metric.setdefault(m, []).append(est)

        if _currency == "CNY":
            metric_name_map = {"revenue": "营收", "eps": "每股收益", "ebitda": "EBITDA"}
            group_suffix = "一致预期"
            consensus_headers = ["期间", "低", "均值", "高", "分析师"]
            period_map = {"CQ": "本季", "NQ": "下季", "FY_Current": "本财年", "FY_Next": "下财年"}
        else:
            metric_name_map = {"revenue": "Revenue", "eps": "EPS", "ebitda": "EBITDA"}
            group_suffix = "Consensus"
            consensus_headers = ["Period", "Low", "Mean", "High", "Analysts"]
            period_map = {}

        for metric_name, estimates in by_metric.items():
            metric_label = metric_name_map.get(metric_name, metric_name.upper())
            consensus_html += f'<p style="font-size:13px;color:var(--accent);margin:16px 0 8px;font-weight:600">{metric_label} {group_suffix}</p>'
            consensus_html += '<table style="font-size:12px"><tr style="background:var(--surface2)">'
            for i, hdr in enumerate(consensus_headers):
                align = "left" if i == 0 else "right"
                consensus_html += f'<th style="padding:6px 8px;text-align:{align}">{hdr}</th>'
            consensus_html += '</tr>'
            for est in sorted(estimates, key=lambda e: getattr(e, "period", "")):
                period_raw = getattr(est, "period", "")
                period = period_map.get(period_raw, period_raw)
                mean = getattr(est, "consensus_mean", 0)
                high = getattr(est, "consensus_high", 0)
                low = getattr(est, "consensus_low", 0)
                count = getattr(est, "analyst_count", 0)
                fmt = lambda v: f"{ccy}{v/ccy_divisor:.1f}{ccy_unit}" if abs(v) > 1e6 else f"{ccy}{v:.2f}"
                consensus_html += f'<tr><td style="padding:4px 8px">{period}</td>'
                consensus_html += f'<td class="num" style="padding:4px 8px">{fmt(low)}</td>'
                consensus_html += f'<td class="num" style="padding:4px 8px;font-weight:600">{fmt(mean)}</td>'
                consensus_html += f'<td class="num" style="padding:4px 8px">{fmt(high)}</td>'
                consensus_html += f'<td class="num" style="padding:4px 8px">{count}</td></tr>'
            consensus_html += '</table>'

    # Price target consensus
    pt_html = ""
    if price_target_consensus:
        pt_low = price_target_consensus.get("target_low", 0)
        pt_cons = price_target_consensus.get("target_consensus", 0)
        pt_high = price_target_consensus.get("target_high", 0)
        pt_med = price_target_consensus.get("target_median", 0)
        if pt_cons > 0:
            upside = ((pt_cons - price) / price * 100) if price else 0
            if _currency == "CNY":
                pt_title = "分析师目标价"
                pt_labels = ("最低", "中位", "一致预期", "上行空间")
            else:
                pt_title = "ANALYST PRICE TARGETS"
                pt_labels = ("Low", "Median", "Consensus", "Upside")
            pt_html = f'''<p style="font-size:13px;color:var(--accent);margin:16px 0 8px;font-weight:600">{pt_title}</p>
            <div class="val-grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:12px">
              <div class="val-item val-bear"><div class="label">{pt_labels[0]}</div><div class="value">{ccy}{pt_low:.0f}</div></div>
              <div class="val-item val-base"><div class="label">{pt_labels[1]}</div><div class="value">{ccy}{pt_med:.0f}</div></div>
              <div class="val-item val-bull"><div class="label">{pt_labels[2]}</div><div class="value">{ccy}{pt_cons:.0f}</div></div>
              <div class="val-item val-price"><div class="label">{pt_labels[3]}</div><div class="value">{upside:+.1f}%</div></div>
            </div>'''

    # ── Earnings History (beat/miss) ─────────────────────────────────
    earnings_html = ""
    if earnings_history:
        if _currency == "CNY":
            eh_headers = ["日期", "预期EPS", "实际EPS", "超预期", "预期营收", "实际营收", "超预期"]
        else:
            eh_headers = ["Date", "EPS Est.", "EPS Act.", "Surprise", "Rev Est.", "Rev Act.", "Surprise"]
        earnings_html += '<table style="font-size:12px"><tr style="background:var(--surface2)">'
        for i, hdr in enumerate(eh_headers):
            align = "left" if i == 0 else "right"
            earnings_html += f'<th style="padding:6px 8px;text-align:{align}">{hdr}</th>'
        earnings_html += '</tr>'
        for eh in earnings_history[:8]:
            rdate = getattr(eh, "report_date", "")
            eps_c = getattr(eh, "eps_consensus", None)
            eps_a = getattr(eh, "eps_actual", None)
            rev_c = getattr(eh, "revenue_consensus", None)
            rev_a = getattr(eh, "revenue_actual", None)
            eps_surp = getattr(eh, "eps_surprise_pct", None)
            rev_surp = getattr(eh, "revenue_surprise_pct", None)
            eps_surp_val = eps_surp if eps_surp is not None else (
                (eps_a - eps_c) / abs(eps_c) if eps_c and eps_a and eps_c != 0 else None
            )
            rev_surp_val = rev_surp if rev_surp is not None else (
                (rev_a - rev_c) / abs(rev_c) if rev_c and rev_a and rev_c != 0 else None
            )
            # BUG-25b: Flag implausible EPS data — yfinance sometimes returns
            # wrong quarterly EPS (e.g. META Q3 2025 $1.05 vs est $6.71).
            # Skip quarters where |surprise| > 60% as likely data errors.
            _eps_suspect = (eps_surp_val is not None and abs(eps_surp_val) > 0.60)
            if _eps_suspect:
                continue  # Drop this quarter from the table
            surp_color = lambda v: "color:var(--green)" if v and v > 0 else "color:var(--red)" if v and v < 0 else ""
            earnings_html += f'<tr><td style="padding:4px 8px">{rdate[:10]}</td>'
            earnings_html += f'<td class="num" style="padding:4px 8px">{ccy}{eps_c:.2f}</td>' if eps_c is not None else '<td class="num" style="padding:4px 8px">—</td>'
            earnings_html += f'<td class="num" style="padding:4px 8px">{ccy}{eps_a:.2f}</td>' if eps_a is not None else '<td class="num" style="padding:4px 8px">—</td>'
            earnings_html += f'<td class="num" style="padding:4px 8px;{surp_color(eps_surp_val)}">{eps_surp_val:+.1%}</td>' if eps_surp_val is not None else '<td class="num" style="padding:4px 8px">—</td>'
            earnings_html += f'<td class="num" style="padding:4px 8px">{ccy}{rev_c/ccy_divisor:.1f}{ccy_unit}</td>' if rev_c is not None else '<td class="num" style="padding:4px 8px">—</td>'
            earnings_html += f'<td class="num" style="padding:4px 8px">{ccy}{rev_a/ccy_divisor:.1f}{ccy_unit}</td>' if rev_a is not None else '<td class="num" style="padding:4px 8px">—</td>'
            earnings_html += f'<td class="num" style="padding:4px 8px;{surp_color(rev_surp_val)}">{rev_surp_val:+.1%}</td>' if rev_surp_val is not None else '<td class="num" style="padding:4px 8px">—</td>'
            earnings_html += '</tr>'
        earnings_html += '</table>'

    # ── Peer Comparison table with relative valuation ──────────────────
    peer_html = ""
    peer_chart_data: dict = {}  # For bar chart
    if peer_fundamentals:
        import statistics as _stats

        # Extract peer data
        peer_rows = []
        for p in sorted(peer_fundamentals, key=lambda x: getattr(x, "market_cap", 0), reverse=True):
            peer_rows.append({
                "name": (getattr(p, "name", "") or getattr(p, "symbol", ""))[:20],
                "symbol": getattr(p, "symbol", ""),
                "mkt": getattr(p, "market_cap", 0),
                "rev": getattr(p, "revenue", 0),
                "gm": getattr(p, "gross_margin", 0),
                "om": getattr(p, "operating_margin", 0),
                "roic": getattr(p, "roic", None),
                "pe": getattr(p, "pe_trailing", None),
                "eveb": getattr(p, "ev_to_ebitda", None),
            })

        # Subject company metrics
        # BUG-fix (2026-04-15): peers' pe comes from yfinance trailingPE (TTM)
        # so we MUST compare against subject's TTM P/E, not the FY-static one
        # the orchestrator computes from the last reported annual NI. Prefer
        # `pe_ratio_ttm` (set by orchestrator from historical_valuation) and
        # fall back to `pe_ratio` only when TTM is unavailable.
        subj_pe = computed_metrics.get("pe_ratio_ttm") or computed_metrics.get("pe_ratio")
        subj_eveb = computed_metrics.get("ev_to_ebitda")
        subj_gm = computed_metrics.get("gross_margin", 0)
        subj_om = computed_metrics.get("operating_margin", 0)
        subj_roic = computed_metrics.get("roic", 0)
        subj_rev = (meta_facts or {}).get("revenue", 0) if meta_facts else 0
        subj_mkt = market_data.get("market_cap", 0)

        # Compute peer medians.
        # BUG-56: Prior filter `> 0` let through data-quality outliers —
        # Sony with EV/EBITDA ≈ 0.03 (renders as 0.0x but passes > 0) pulled
        # AAPL's peer median down to 12.5x, inflating the displayed EV/EBITDA
        # premium from ~+80% to +112%. Symmetrically Snap's negative EBITDA
        # produces a huge negative multiple that must be excluded. Use sane
        # bounds: 1 < P/E < 200 and 1 < EV/EBITDA < 100 captures all real
        # trading multiples while dropping distressed / data-error rows.
        def _sane(val: float | None, lo: float, hi: float) -> bool:
            return val is not None and lo < val < hi
        pe_vals = [r["pe"] for r in peer_rows if _sane(r["pe"], 1.0, 200.0)]
        eveb_vals = [r["eveb"] for r in peer_rows if _sane(r["eveb"], 1.0, 100.0)]
        med_pe = _stats.median(pe_vals) if pe_vals else None
        med_eveb = _stats.median(eveb_vals) if eveb_vals else None

        # Premium/discount
        def _prem(subj, median):
            if subj and median and median > 0:
                pct = (subj - median) / median * 100
                color = "var(--red)" if pct > 10 else ("var(--green)" if pct < -10 else "var(--muted)")
                label = f"+{pct:.0f}% premium" if pct > 0 else f"{pct:.0f}% discount"
                return f'<span style="color:{color};font-weight:600">{label}</span>'
            return "—"

        pe_premium = _prem(subj_pe, med_pe)
        eveb_premium = _prem(subj_eveb, med_eveb)

        # Build table
        hdr_style = 'style="padding:6px 8px;text-align:right"'
        cell = 'style="padding:4px 8px"'
        peer_html += f'<table class="data-table" style="font-size:12px"><thead><tr>'
        peer_html += f'<th style="padding:6px 8px;text-align:left">Company</th>'
        peer_html += f'<th {hdr_style}>Mkt Cap</th><th {hdr_style}>Revenue</th>'
        peer_html += f'<th {hdr_style}>GM%</th><th {hdr_style}>OM%</th><th {hdr_style}>ROIC%</th>'
        peer_html += f'<th {hdr_style}>P/E</th><th {hdr_style}>EV/EBITDA</th></tr></thead><tbody>'

        # Subject company row (highlighted)
        peer_html += f'<tr style="background:rgba(59,130,246,0.15);border-left:3px solid var(--accent)">'
        peer_html += f'<td {cell}><strong>{entity}</strong> ★</td>'
        peer_html += f'<td class="num" {cell}>{ccy}{subj_mkt/ccy_divisor:.0f}{ccy_unit}</td>'
        peer_html += f'<td class="num" {cell}>{ccy}{subj_rev/ccy_divisor:.1f}{ccy_unit}</td>'
        peer_html += f'<td class="num" {cell}>{subj_gm*100:.1f}%</td>'
        peer_html += f'<td class="num" {cell}>{subj_om*100:.1f}%</td>'
        peer_html += f'<td class="num" {cell}>{subj_roic*100:.1f}%</td>'
        peer_html += (f'<td class="num" {cell}>{subj_pe:.1f}x</td>' if subj_pe else f'<td class="num" {cell}>—</td>')
        peer_html += (f'<td class="num" {cell}>{subj_eveb:.1f}x</td>' if subj_eveb else f'<td class="num" {cell}>—</td>')
        peer_html += '</tr>'

        # Peer rows
        for r in peer_rows:
            peer_html += f'<tr><td {cell}>{r["name"]}</td>'
            peer_html += f'<td class="num" {cell}>{ccy}{r["mkt"]/ccy_divisor:.0f}{ccy_unit}</td>'
            peer_html += f'<td class="num" {cell}>{ccy}{r["rev"]/ccy_divisor:.1f}{ccy_unit}</td>'
            peer_html += f'<td class="num" {cell}>{r["gm"]*100:.1f}%</td>'
            peer_html += f'<td class="num" {cell}>{r["om"]*100:.1f}%</td>'
            peer_html += (f'<td class="num" {cell}>{r["roic"]*100:.1f}%</td>' if r["roic"] is not None else f'<td class="num" {cell}>—</td>')
            # BUG-56: render out-of-range multiples as "—" to match median filter
            peer_html += (f'<td class="num" {cell}>{r["pe"]:.1f}x</td>'
                          if _sane(r["pe"], 1.0, 200.0)
                          else f'<td class="num" {cell}>—</td>')
            peer_html += (f'<td class="num" {cell}>{r["eveb"]:.1f}x</td>'
                          if _sane(r["eveb"], 1.0, 100.0)
                          else f'<td class="num" {cell}>—</td>')
            peer_html += '</tr>'

        # Median row
        gm_vals = [r["gm"] for r in peer_rows if r["gm"]]
        om_vals = [r["om"] for r in peer_rows if r["om"]]
        peer_html += f'<tr style="background:var(--surface2);font-style:italic">'
        peer_html += f'<td {cell}>Peer Median</td><td {cell}></td><td {cell}></td>'
        peer_html += f'<td class="num" {cell}>{_stats.median(gm_vals)*100:.1f}%</td>' if gm_vals else f'<td {cell}>—</td>'
        peer_html += f'<td class="num" {cell}>{_stats.median(om_vals)*100:.1f}%</td>' if om_vals else f'<td {cell}>—</td>'
        peer_html += f'<td {cell}></td>'
        peer_html += f'<td class="num" {cell}>{med_pe:.1f}x</td>' if med_pe else f'<td {cell}>—</td>'
        peer_html += f'<td class="num" {cell}>{med_eveb:.1f}x</td>' if med_eveb else f'<td {cell}>—</td>'
        peer_html += '</tr></tbody></table>'

        # Premium/discount summary
        peer_html += f'<div style="margin-top:10px;font-size:13px;color:var(--muted)">'
        peer_html += f'P/E vs peer median: {pe_premium} &nbsp;|&nbsp; '
        peer_html += f'EV/EBITDA vs peer median: {eveb_premium}</div>'

        # Build chart data for peer valuation bar chart
        # BUG-32: apply same _sane() bounds as table/median to exclude outliers
        chart_names = [entity] + [r["name"] for r in peer_rows]
        chart_pe = [subj_pe or 0] + [r["pe"] if _sane(r["pe"], 1.0, 200.0) else None for r in peer_rows]
        chart_eveb = [subj_eveb or 0] + [r["eveb"] if _sane(r["eveb"], 1.0, 100.0) else None for r in peer_rows]
        peer_chart_data = {"names": chart_names, "pe": chart_pe, "eveb": chart_eveb}

    # Segment breakdown from XBRL instance
    segment_breakdown_html = ""
    if segment_detail:
        for category, segments in segment_detail.items():
            if not segments:
                continue
            cat_label = _format_segment_name(category)
            segment_breakdown_html += f'<p style="font-size:13px;color:var(--accent);margin:16px 0 8px;font-weight:600">{cat_label} Breakdown</p>'
            segment_breakdown_html += '<table style="font-size:12px"><tr style="background:var(--surface2)">'
            segment_breakdown_html += '<th style="padding:6px 8px;text-align:left">Segment</th>'
            segment_breakdown_html += '<th style="padding:6px 8px;text-align:right">Revenue</th>'
            segment_breakdown_html += '<th style="padding:6px 8px;text-align:right">% of Total</th>'
            segment_breakdown_html += '</tr>'
            # BUG-46: denominator is company total revenue, not group-local sum.
            # Using the group-local sum made % of Total always 100% even when
            # the axis had parent/child overlap (1.5-2.9x) or missing members.
            company_total_rev = meta_facts.get("revenue", 0) if meta_facts else 0
            group_total_rev = sum(s.get("revenue", 0) for s in segments.values())
            total_rev = company_total_rev or group_total_rev
            # BUG-26: warn when segments don't reconcile to total revenue
            _seg_gap_note = ""
            if company_total_rev > 0 and group_total_rev > 0:
                _gap = group_total_rev - company_total_rev
                _gap_pct = abs(_gap) / company_total_rev
                if _gap_pct > 0.02:  # >2% discrepancy
                    if _gap > 0:
                        _seg_gap_note = (
                            f'<tr><td colspan="3" style="padding:4px 8px;color:var(--muted);font-size:11px;font-style:italic">'
                            f'{"⚠ 分项有重叠" if _currency == "CNY" else "⚠ Segments overlap"}'
                            f' ({ccy}{abs(_gap)/ccy_divisor:.1f}{ccy_unit}, '
                            f'{"各项合计超出总营收" if _currency == "CNY" else "sum exceeds total revenue"} '
                            f'{_gap_pct:.0%})</td></tr>'
                        )
                    else:
                        _seg_gap_note = (
                            f'<tr><td colspan="3" style="padding:4px 8px;color:var(--muted);font-size:11px;font-style:italic">'
                            f'{"⚠ 分项未覆盖全部营收" if _currency == "CNY" else "⚠ Segments incomplete"}'
                            f' ({ccy}{abs(_gap)/ccy_divisor:.1f}{ccy_unit} '
                            f'{"未分配" if _currency == "CNY" else "unallocated"}, '
                            f'{_gap_pct:.0%})</td></tr>'
                        )
            for seg_id, seg_data in sorted(segments.items(), key=lambda x: x[1].get("revenue", 0), reverse=True):
                rev = seg_data.get("revenue", 0)
                if rev <= 0:
                    continue
                pct = rev / total_rev * 100 if total_rev else 0
                seg_label = _format_segment_name(seg_id)
                # Flag synthetic / proxy entries so the user knows they are
                # not directly reported
                is_synth = isinstance(seg_data, dict) and seg_data.get("_synthetic")
                row_label = f"{seg_label} <span style='color:var(--muted);font-size:10px'>(est. gap)</span>" if is_synth else seg_label
                segment_breakdown_html += f'<tr><td style="padding:4px 8px">{row_label}</td>'
                segment_breakdown_html += f'<td class="num" style="padding:4px 8px">{ccy}{rev/ccy_divisor:.1f}{ccy_unit}</td>'
                segment_breakdown_html += f'<td class="num" style="padding:4px 8px">{pct:.1f}%</td></tr>'
            if _seg_gap_note:
                segment_breakdown_html += _seg_gap_note
            segment_breakdown_html += '</table>'

    # Forecast bridge table
    forecast_html = ""
    if dcf_projections:
        has_da = any(p.get("depreciation", 0) > 0 for p in dcf_projections)
        # BUG-22: Show SBC and NWC columns when they have non-zero values
        # so readers can verify FCFF = NOPAT + D&A - CapEx - SBC - ΔNWC
        has_sbc = any(abs(p.get("sbc", 0)) > 0 for p in dcf_projections)
        has_nwc = any(abs(p.get("change_in_nwc", 0)) > 0 for p in dcf_projections)

        if _currency == "CNY":
            cols = ["营收", "息税前利润"]
            keys = ["revenue", "operating_income"]
            if has_da:
                cols.append("折旧摊销")
                keys.append("depreciation")
            cols += ["税后营业利润", "资本开支"]
            keys += ["nopat", "capex"]
            if has_sbc:
                cols.append("SBC")
                keys.append("sbc")
            if has_nwc:
                cols.append("ΔNWC")
                keys.append("change_in_nwc")
            cols += ["自由现金流", "折现值"]
            keys += ["fcff", "pv_fcff"]
            year_col = "年份"
        else:
            cols = ["Revenue", "EBIT"]
            keys = ["revenue", "operating_income"]
            if has_da:
                cols.append("D&A")
                keys.append("depreciation")
            cols += ["NOPAT", "CapEx"]
            keys += ["nopat", "capex"]
            if has_sbc:
                cols.append("SBC")
                keys.append("sbc")
            if has_nwc:
                cols.append("ΔNWC")
                keys.append("change_in_nwc")
            cols += ["FCFF", "PV(FCFF)"]
            keys += ["fcff", "pv_fcff"]
            year_col = "Year"

        forecast_html += '<table style="font-size:12px"><tr style="background:var(--surface2)">'
        forecast_html += f'<th style="padding:6px 8px;text-align:left">{year_col}</th>'
        for col in cols:
            forecast_html += f'<th style="padding:6px 8px;text-align:right">{col}</th>'
        forecast_html += '</tr>'
        for p in dcf_projections:
            forecast_html += '<tr>'
            forecast_html += f'<td style="padding:4px 8px">{p["year"]}</td>'
            for key in keys:
                val = p.get(key, 0)
                forecast_html += f'<td class="num" style="padding:4px 8px">{ccy}{val/ccy_divisor:.1f}{ccy_unit}</td>'
            forecast_html += '</tr>'
        forecast_html += '</table>'

        # DCF value bridge — make the per-share arithmetic transparent
        try:
            bridge = scenarios_raw.get("dcf_bridge") if isinstance(scenarios_raw, dict) else None
            pv_explicit_sum = (bridge or {}).get("pv_fcff_sum") or sum(p.get("pv_fcff", 0) for p in dcf_projections)
            pv_terminal = (bridge or {}).get("pv_terminal_value")
            nd = (bridge or {}).get("net_debt")
            shr_current = market_data.get("shares_outstanding", 0) if market_data else 0
            future_shares = (bridge or {}).get("future_shares")

            if pv_explicit_sum and pv_terminal is not None and nd is not None and shr_current:
                ev = pv_explicit_sum + pv_terminal
                equity = ev - nd
                # BUG-29/30: use current shares (consistent with DCF engine
                # which now divides equity by current shares, not future)
                shr_used = shr_current
                per_share_calc = equity / shr_used if shr_used else 0
                dilution_note = ""
                if future_shares and shr_current and abs(future_shares - shr_current) / shr_current > 0.005:
                    dil_pct = (future_shares / shr_current - 1) * 100
                    if _currency == "CNY":
                        label = "回购缩减" if dil_pct < 0 else "稀释"
                    else:
                        label = "buyback-adjusted" if dil_pct < 0 else "diluted"
                    dilution_note = f" ({label} {dil_pct:+.1f}%)"
                if _currency == "CNY":
                    forecast_html += (
                        f'<div style="font-size:11px;color:var(--muted);margin-top:10px;padding:8px;background:var(--surface2);border-radius:6px;line-height:1.7">'
                        f'<strong>估值推导：</strong> '
                        f'十年折现现金流合计 {ccy}{pv_explicit_sum/ccy_divisor:.1f}{ccy_unit} '
                        f'+ 永续价值折现 {ccy}{pv_terminal/ccy_divisor:.1f}{ccy_unit} '
                        f'= 企业价值 {ccy}{ev/ccy_divisor:.1f}{ccy_unit}<br>'
                        f'企业价值 − 净负债 {ccy}{nd/ccy_divisor:.1f}{ccy_unit} '
                        f'= 股东权益价值 {ccy}{equity/ccy_divisor:.1f}{ccy_unit} '
                        f'÷ {shr_used/1e8:.2f} 亿股{dilution_note} '
                        f'= {ccy}{per_share_calc:.2f} / 股'
                        f'</div>'
                    )
                else:
                    forecast_html += (
                        f'<div style="font-size:11px;color:var(--muted);margin-top:10px;padding:8px;background:var(--surface2);border-radius:6px;line-height:1.7">'
                        f'<strong>DCF Bridge:</strong> '
                        f'ΣPV(FCFF) {ccy}{pv_explicit_sum/ccy_divisor:.1f}{ccy_unit} '
                        f'+ PV(Terminal) {ccy}{pv_terminal/ccy_divisor:.1f}{ccy_unit} '
                        f'= EV {ccy}{ev/ccy_divisor:.1f}{ccy_unit}<br>'
                        f'EV − Net Debt {ccy}{nd/ccy_divisor:.1f}{ccy_unit} '
                        f'= Equity {ccy}{equity/ccy_divisor:.1f}{ccy_unit} '
                        f'÷ {shr_used/1e9:.2f}B shares{dilution_note} '
                        f'= {ccy}{per_share_calc:.2f}/share'
                        f'</div>'
                    )
        except Exception:
            pass

    # Segment breakdown table
    segment_html = ""
    if segment_projections and len(segment_projections) > 1:
        seg_ids = sorted(segment_projections.keys())
        seg_names = {}
        for sid in seg_ids:
            if segment_projections[sid]:
                raw_name = segment_projections[sid][0].get("segment_name", sid)
                seg_names[sid] = _format_segment_name(raw_name)
            else:
                seg_names[sid] = _format_segment_name(sid)

        segment_html += '<p style="font-size:13px;color:var(--accent);margin:16px 0 8px;font-weight:600">SEGMENT REVENUE BREAKDOWN</p>'
        segment_html += '<table style="font-size:12px"><tr style="background:var(--surface2)">'
        segment_html += '<th style="padding:6px 8px;text-align:left">Year</th>'
        for sid in seg_ids:
            segment_html += f'<th style="padding:6px 8px;text-align:right">{seg_names[sid]}</th>'
        segment_html += '<th style="padding:6px 8px;text-align:right">Total</th></tr>'

        n_years = len(segment_projections[seg_ids[0]]) if seg_ids else 0
        for i in range(n_years):
            segment_html += '<tr>'
            segment_html += f'<td style="padding:4px 8px">{i+1}</td>'
            total = 0
            for sid in seg_ids:
                rev = segment_projections[sid][i]["revenue"] if i < len(segment_projections[sid]) else 0
                total += rev
                segment_html += f'<td class="num" style="padding:4px 8px">{ccy}{rev/ccy_divisor:.1f}{ccy_unit}</td>'
            segment_html += f'<td class="num" style="padding:4px 8px;font-weight:700">{ccy}{total/ccy_divisor:.1f}{ccy_unit}</td>'
            segment_html += '</tr>'
        segment_html += '</table>'

    # Sensitivity rankings table
    sensitivity_html = ""
    if sensitivity_rankings:
        if _currency == "CNY":
            sens_headers = ["假设", "影响", "基准", "冲击值"]
            assumption_zh_map = {
                "revenue_growth_rate": "收入增速",
                "terminal_growth_rate": "永续增长率",
                "operating_margin": "营业利润率",
                "wacc": "加权资本成本",
                "capex_to_revenue": "资本开支率",
                "sbc_to_revenue": "股权激励率",
                "effective_tax_rate": "有效税率",
                "buyback_yield_annual": "回购收益率",
                "nwc_to_revenue_delta": "营运资本率",
            }
        else:
            sens_headers = ["Assumption", "Impact", "Base", "Shocked"]
            assumption_zh_map = {}

        sensitivity_html += '<table style="font-size:12px;margin-top:12px"><tr style="background:var(--surface2)">'
        sensitivity_html += f'<th style="padding:6px 8px;text-align:left">{sens_headers[0]}</th>'
        for hdr in sens_headers[1:]:
            sensitivity_html += f'<th style="padding:6px 8px;text-align:right">{hdr}</th>'
        sensitivity_html += '</tr>'
        for r in sensitivity_rankings:
            assumption_raw = r["assumption"]
            assumption_display = assumption_zh_map.get(assumption_raw, assumption_raw)
            # BUG-FIX (2026-04-15): use signed_impact_pct if available so users
            # see the direction of the shock (e.g. -15.5% for WACC, not 15.5%).
            # Fall back to unsigned impact_pct for older caches.
            signed_pct = r.get("signed_impact_pct")
            if signed_pct is None:
                # Infer sign from base/shocked values
                base = r.get("base_per_share", 0)
                shocked = r.get("shocked_per_share", 0)
                signed_pct = (shocked - base) / base if base else r["impact_pct"]
            sensitivity_html += f'<tr><td style="padding:4px 8px">{assumption_display}</td>'
            sensitivity_html += f'<td class="num" style="padding:4px 8px">{signed_pct:+.1%}</td>'
            sensitivity_html += f'<td class="num" style="padding:4px 8px">{ccy}{r["base_per_share"]:.0f}</td>'
            sensitivity_html += f'<td class="num" style="padding:4px 8px">{ccy}{r["shocked_per_share"]:.0f}</td></tr>'
        sensitivity_html += '</table>'

    # 2-way sensitivity table
    twoway_html = ""
    if sensitivity_table:
        v1_vals = sensitivity_table.get("var1_values", [])
        v2_vals = sensitivity_table.get("var2_values", [])
        matrix = sensitivity_table.get("matrix", [])
        v1_name_raw = sensitivity_table.get("variable_1", "Var1")
        v2_name_raw = sensitivity_table.get("variable_2", "Var2")
        if _currency == "CNY":
            twoway_zh_map = {
                "wacc": "加权资本成本",
                "terminal_growth_rate": "永续增长率",
                "revenue_growth_rate": "收入增速",
                "operating_margin": "营业利润率",
                "capex_to_revenue": "资本开支率",
            }
            v1_name = twoway_zh_map.get(v1_name_raw, v1_name_raw)
            v2_name = twoway_zh_map.get(v2_name_raw, v2_name_raw)
        else:
            v1_name = v1_name_raw
            v2_name = v2_name_raw
        if v1_vals and v2_vals and matrix:
            twoway_html += f'<p style="font-size:12px;color:var(--text2);margin:12px 0 6px">{v1_name} vs {v2_name}</p>'
            twoway_html += '<table style="font-size:11px"><tr style="background:var(--surface2)">'
            twoway_html += f'<th style="padding:4px 6px">{v1_name}\\{v2_name}</th>'
            for v2 in v2_vals:
                twoway_html += f'<th style="padding:4px 6px;text-align:right">{v2:.1%}</th>'
            twoway_html += '</tr>'
            for i, v1 in enumerate(v1_vals):
                twoway_html += f'<tr><td style="padding:4px 6px;font-weight:600">{v1:.1%}</td>'
                for j, v2 in enumerate(v2_vals):
                    val = matrix[i][j]
                    twoway_html += f'<td class="num" style="padding:4px 6px">{ccy}{val:.0f}</td>'
                twoway_html += '</tr>'
            twoway_html += '</table>'

    _page_title_prefix = "Aegis 投研" if _currency == "CNY" else "Aegis Research"
    _html = f'''<!DOCTYPE html>
<html lang="{"zh-CN" if _currency == "CNY" else "en"}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_page_title_prefix} — {entity}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f172a; --surface: #1e293b; --surface2: #334155;
    --text: #e2e8f0; --text2: #94a3b8; --accent: #3b82f6;
    --green: #22c55e; --red: #ef4444; --yellow: #eab308; --orange: #f97316;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: 'Inter', -apple-system, system-ui, sans-serif; background:var(--bg); color:var(--text); line-height:1.6; }}
  .container {{ max-width:1100px; margin:0 auto; padding:24px; }}

  /* Header */
  .header {{ background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%); border-radius:16px; padding:32px; margin-bottom:24px; border:1px solid #334155; }}
  .header h1 {{ font-size:28px; font-weight:700; margin-bottom:4px; }}
  .header .subtitle {{ color:var(--text2); font-size:14px; }}
  .badges {{ display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }}
  .badge {{ padding:4px 12px; border-radius:20px; font-size:12px; font-weight:600; text-transform:uppercase; }}

  /* Grid */
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:24px; }}
  .grid-full {{ grid-column: 1 / -1; }}
  @media (max-width:768px) {{ .grid {{ grid-template-columns:1fr; }} }}

  /* Cards */
  .card {{ background:var(--surface); border-radius:12px; padding:24px; border:1px solid var(--surface2); }}
  .card h3 {{ font-size:16px; color:var(--accent); margin-bottom:16px; text-transform:uppercase; letter-spacing:0.5px; font-weight:600; }}
  .card p {{ color:var(--text2); font-size:14px; margin-bottom:8px; }}

  /* Valuation highlight */
  .val-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; text-align:center; }}
  .val-item {{ padding:16px 8px; border-radius:10px; }}
  .val-item .label {{ font-size:11px; text-transform:uppercase; color:var(--text2); margin-bottom:4px; }}
  .val-item .value {{ font-size:24px; font-weight:700; }}
  .val-bear {{ background:#451a1a; }} .val-bear .value {{ color:var(--red); }}
  .val-base {{ background:#1a2e4a; }} .val-base .value {{ color:var(--accent); }}
  .val-bull {{ background:#14412a; }} .val-bull .value {{ color:var(--green); }}
  .val-price {{ background:#2d2305; }} .val-price .value {{ color:var(--yellow); }}

  /* Charts */
  .chart-container {{ position:relative; height:280px; }}

  /* Chief Analyst Editorial Layer */
  .chief-analyst-summary {{ border-left:4px solid #3b82f6; }}
  .headline-banner {{ margin-bottom:16px; padding-bottom:12px; border-bottom:2px solid var(--surface2); }}
  .report-headline {{ font-size:20px; font-weight:700; line-height:1.3; color:var(--text1); margin:0; }}
  .opening-paragraph {{ font-size:15px; line-height:1.7; color:var(--text1); margin-bottom:16px; font-style:italic; }}
  .salient-chars {{ margin-bottom:16px; }}
  .salient-chars ul {{ margin:8px 0 0 20px; padding:0; }}
  .salient-chars li {{ font-size:13px; color:var(--text2); margin-bottom:4px; }}
  .front-numbers-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:16px; }}
  .front-number {{ background:var(--surface2); padding:14px; border-radius:8px; text-align:center; }}
  .front-number-value {{ font-size:22px; font-weight:700; color:#3b82f6; }}
  .front-number-label {{ font-size:12px; color:var(--text2); text-transform:uppercase; margin-top:4px; }}
  .front-number-context {{ font-size:11px; color:var(--text2); margin-top:6px; font-style:italic; }}
  .executive-summary-text {{ font-size:14px; line-height:1.6; }}

  /* Edge */
  .edge-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  .edge-item {{ background:var(--surface2); padding:12px; border-radius:8px; }}
  .edge-item .label {{ font-size:11px; color:var(--text2); text-transform:uppercase; }}
  .edge-item .val {{ font-size:13px; margin-top:4px; }}

  /* Critics */
  .critic-row {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; }}
  .critic-name {{ width:180px; font-size:13px; color:var(--text2); }}
  .critic-bar-bg {{ flex:1; height:8px; background:var(--surface2); border-radius:4px; overflow:hidden; }}
  .critic-bar {{ height:100%; border-radius:4px; transition:width 0.5s; }}
  .critic-count {{ font-size:12px; color:var(--text2); width:70px; text-align:right; }}

  /* Agent cards */
  .agent-card {{ background:var(--surface2); border-radius:8px; margin-bottom:8px; overflow:hidden; }}
  .agent-header {{ padding:12px 16px; cursor:pointer; display:flex; justify-content:space-between; font-size:14px; font-weight:600; }}
  .agent-header::-webkit-details-marker {{ display:none; }}
  .agent-stats {{ color:var(--text2); font-size:12px; }}
  .agent-body {{ padding:0 16px 16px; }}
  .agent-body h4 {{ font-size:13px; color:var(--accent); margin:12px 0 6px; }}
  .agent-body ul {{ padding-left:16px; font-size:13px; color:var(--text2); }}
  .agent-body li {{ margin-bottom:6px; }}
  .counter-list {{ border-left:3px solid var(--orange); padding-left:12px !important; }}

  /* Tables */
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:8px 12px; font-size:13px; border-bottom:1px solid var(--surface2); }}
  .kill-icon, .monitor-icon {{ width:30px; text-align:center; }}
  .freq {{ color:var(--text2); font-size:12px; }}
  .num {{ text-align:right; font-weight:600; font-variant-numeric:tabular-nums; }}

  /* Footer */
  .footer {{ text-align:center; padding:24px; color:var(--text2); font-size:11px; }}
</style>
</head>
<body>
<div class="container">

  <!-- HEADER -->
  <div class="header">
    <h1>{entity}</h1>
    <div class="subtitle">{"Aegis 投研 OS — 投资研究报告" if _currency == "CNY" else "Aegis Research OS — Investment Research Report"} · {now}</div>
    {_build_timeliness_banner(meta_facts, _currency)}
    {_build_dq_banner(meta_facts, _currency)}
    <div class="badges">
      <span class="badge" style="background:{status_color};color:#000">{_status_label(status, _currency)}</span>
      <span class="badge" style="background:{conf_color};color:#000">{"置信度：" if _currency == "CNY" else "Confidence: "}{_enum_label(confidence, _currency)}</span>
      <span class="badge" style="background:{'#22c55e' if bias_status == 'passed' else '#f97316'};color:#000">{"偏差检查：" if _currency == "CNY" else "Bias: "}{_enum_label(bias_status, _currency)}</span>
      <span class="badge" style="background:var(--surface2)">{"运行编号：" if _currency == "CNY" else "Run: "}{run_id[:20]}</span>
    </div>
  </div>

  <!-- VALUATION HIGHLIGHT -->
  <div class="card" style="margin-bottom:24px">
    <h3>{"估值情景" if _currency == "CNY" else "Scenario Valuation"}{f' <span style="font-size:14px;font-weight:400;color:var(--muted)">| {"概率加权" if _currency == "CNY" else "Probability-Weighted"}: {ccy}{pw_val:.0f}</span>' if pw_val else ''}</h3>
    <div class="val-grid">
      <div class="val-item val-bear">
        <div class="label">{"悲观情景" if _currency == "CNY" else "Bear Case"}{f" ({sc_prob.get('bear', 0.25):.0%})" if sc_prob.get("bear") else ""}</div>
        <div class="value">{ccy}{bear:.0f}</div>
        {f'<div style="font-size:12px;color:var(--muted);margin-top:6px;line-height:1.4">{sc_narr["bear"]}</div>' if sc_narr.get("bear") else ""}
      </div>
      <div class="val-item val-base">
        <div class="label">{"基准情景" if _currency == "CNY" else "Base Case"}{f" ({sc_prob.get('base', 0.50):.0%})" if sc_prob.get("base") else ""}</div>
        <div class="value">{ccy}{base:.0f}</div>
        {f'<div style="font-size:12px;color:var(--muted);margin-top:6px;line-height:1.4">{sc_narr["base"]}</div>' if sc_narr.get("base") else ""}
      </div>
      <div class="val-item val-bull">
        <div class="label">{"乐观情景" if _currency == "CNY" else "Bull Case"}{f" ({sc_prob.get('bull', 0.25):.0%})" if sc_prob.get("bull") else ""}</div>
        <div class="value">{ccy}{bull:.0f}</div>
        {f'<div style="font-size:12px;color:var(--muted);margin-top:6px;line-height:1.4">{sc_narr["bull"]}</div>' if sc_narr.get("bull") else ""}
      </div>
      <div class="val-item val-price">
        <div class="label">{"当前股价" if _currency == "CNY" else "Current Price"}</div>
        <div class="value">{ccy}{price:.0f}</div>
        {f'<div style="font-size:12px;color:var(--muted);margin-top:6px;line-height:1.4">{"核心变量" if _currency == "CNY" else "Swing Factor"}: {swing_factor}</div>' if swing_factor else ""}
      </div>
    </div>
  </div>

  <!-- FORECAST BRIDGE -->
  {f'''<div class="card" style="margin-bottom:24px">
    <h3>Forecast Bridge (DCF Projections)</h3>
    {forecast_html}
    {segment_html}
    {sensitivity_html}
    {twoway_html}
  </div>''' if forecast_html else ''}

  <div class="grid">

    <!-- SCENARIO CHART -->
    <div class="card">
      <h3>Valuation Scenarios vs Price</h3>
      <div class="chart-container"><canvas id="scenarioChart"></canvas></div>
    </div>

    <!-- RADAR CHART -->
    <div class="card">
      <h3>Key Metrics Radar</h3>
      <div class="chart-container"><canvas id="radarChart"></canvas></div>
    </div>

    {f"""<!-- HISTORICAL VALUATION -->
    <div class="card grid-full">
      <h3>{"历史估值区间 (" if _currency == "CNY" else "Historical Valuation Range ("}{_format_hv_window(historical_valuation)}){
        # Methodology badge — only show "(估算)" tag when the P/E history was
        # computed from constant-EPS price scaling (less reliable). When
        # using true_ttm from real quarterly NI, no tag — that's the default
        # quality level. 2026-04-15 added.
        ('<span style="font-size:12px;color:var(--muted);font-weight:400;margin-left:8px">'
         + ("（估算 · 价格缩放）" if _currency == "CNY" else "(estimated · price-scaled)")
         + '</span>') if historical_valuation.get("pe_methodology") == "price_scaled" else ""
      }</h3>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="chart-container" style="height:280px"><canvas id="peChart"></canvas></div>
        <div class="chart-container" style="height:280px"><canvas id="evChart"></canvas></div>
      </div>
      <div style="display:flex;gap:24px;margin-top:12px;font-size:12px;color:var(--muted)">
        <span>{"市盈率" if _currency == "CNY" else "P/E"}: {historical_valuation.get('pe_stats', dict()).get('min', 0):.0f}x - {historical_valuation.get('pe_stats', dict()).get('max', 0):.0f}x ({"中位" if _currency == "CNY" else "median"} {historical_valuation.get('pe_stats', dict()).get('median', 0):.0f}x, {"当前" if _currency == "CNY" else "current"} {historical_valuation.get('pe_stats', dict()).get('current', 0):.0f}x)</span>
        <span>{"企业倍数" if _currency == "CNY" else "EV/EBITDA"}: {historical_valuation.get('ev_ebitda_stats', dict()).get('min', 0):.0f}x - {historical_valuation.get('ev_ebitda_stats', dict()).get('max', 0):.0f}x ({"中位" if _currency == "CNY" else "median"} {historical_valuation.get('ev_ebitda_stats', dict()).get('median', 0):.0f}x)</span>
      </div>
    </div>""" if historical_valuation and historical_valuation.get('dates') else ""}

    <!-- HISTORICAL TREND -->
    {f'<div class="card"><h3>Historical Financials</h3><div class="chart-container"><canvas id="histChart"></canvas></div></div>' if hist_chart_js else ''}

    <!-- EXECUTIVE SUMMARY (Chief Analyst Editorial Layer) -->
    {_build_executive_summary_html(decision, edited_report, research_directive, edge_type, edge_dur, edge_why, edge_decay, edge_source, edge_change, _currency, synthesized_thesis)}

    <!-- SEGMENT BREAKDOWN -->
    {f'<div class="card grid-full"><h3>Segment Breakdown</h3>{segment_breakdown_html}</div>' if segment_breakdown_html else ''}

    <!-- KEY FINANCIALS -->
    <div class="card">
      <h3>Key Financials</h3>
      <table>{fin_html}</table>
      {shareholder_html}
      <table style="margin-top:12px">
        <tr><td>Gross Margin</td><td class="num">{gm:.1f}%</td></tr>
        <tr><td>Operating Margin</td><td class="num">{om:.1f}%</td></tr>
        <tr><td>ROIC</td><td class="num">{min(computed_metrics.get("roic",0)*100,999):.1f}%</td></tr>
        <tr><td>ROE</td><td class="num">{min(computed_metrics.get("roe",0)*100,999):.1f}%</td></tr>
        <tr><td>{"市盈率 (年报静态)" if _currency=="CNY" else "P/E (FY)"}</td><td class="num">{f'{computed_metrics["pe_ratio"]:.1f}x' if computed_metrics.get("pe_ratio") else ("无数据" if _currency == "CNY" else "--")}</td></tr>
        {f'<tr><td>{"市盈率 (滚动12月)" if _currency=="CNY" else "P/E (TTM)"}</td><td class="num">{historical_valuation["pe_stats"]["current"]:.1f}x</td></tr>' if historical_valuation and historical_valuation.get("pe_stats", {}).get("current") else ''}
        <tr><td>{"企业倍数" if _currency == "CNY" else "EV/EBITDA"}</td><td class="num">{(f'{computed_metrics["ev_to_ebitda"]:.1f}x' + (' <span style="color:var(--muted);font-size:10px">' + ("（估算）" if _currency == "CNY" else "(proxy)") + '</span>' if (meta_facts or {}).get('_ebitda_proxy') else '')) if computed_metrics.get("ev_to_ebitda") else ("无数据" if _currency == "CNY" else "n/a")}</td></tr>
      </table>
    </div>

    <!-- CRITIC REVIEW -->
    <div class="card">
      <h3>Critic Review Board</h3>
      {critic_html}
    </div>

    {f"""<!-- OPEN RESEARCH QUESTIONS -->
    <div class="card">
      <h3>{"待研究问题" if _currency == "CNY" else "Open Research Questions"}</h3>
      <p style="color:var(--muted);font-size:13px;margin-bottom:12px">
        {"智能体提出但无法从现有数据回答的问题。解决这些问题将强化论点可信度。" if _currency == "CNY" else "Questions raised by agents that could not be answered from available data. Addressing these would strengthen the thesis."}</p>
      {open_q_html}
    </div>""" if open_q_html else ""}

    {f"""<!-- EARNINGS CALL INSIGHTS -->
    <div class="card grid-full">
      <h3>Earnings Call Insights</h3>
      {ec_html}
    </div>""" if ec_html else ""}

    <!-- CONSENSUS ESTIMATES -->
    {f'''<div class="card grid-full">
      <h3>Consensus Estimates</h3>
      {pt_html}
      {consensus_html}
    </div>''' if (consensus_html or pt_html) else ''}

    <!-- EARNINGS HISTORY -->
    {f'''<div class="card grid-full">
      <h3>Earnings History (Beat / Miss)</h3>
      {earnings_html}
    </div>''' if earnings_html else ''}

    <!-- PEER COMPARISON -->
    {f'''<div class="card grid-full">
      <h3>Peer Relative Valuation</h3>
      {peer_html}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
        <div class="chart-container" style="height:220px"><canvas id="peerPeChart"></canvas></div>
        <div class="chart-container" style="height:220px"><canvas id="peerEvChart"></canvas></div>
      </div>
    </div>''' if peer_html else ''}

    <!-- PREDICTION CALIBRATION DASHBOARD -->
    {_build_calibration_card(_currency)}

    <!-- CATALYST TIMELINE -->
    {_build_catalyst_timeline_card(catalyst_timeline, _currency)}

    <!-- INSIDER TRADING -->
    {_build_insider_trading_card(insider_summary, _currency)}

    <!-- NEWS SENTIMENT -->
    {_build_news_sentiment_card(news_sentiment_insights)}

    <!-- KILL CRITERIA -->
    <div class="card">
      <h3>Kill Criteria</h3>
      <table>{kill_html if kill_html else '<tr><td>No kill criteria defined</td></tr>'}</table>
    </div>

    <!-- AGENT ANALYSIS -->
    <div class="card grid-full">
      <h3>Agent Analysis</h3>
      {agent_html}
    </div>

    <!-- MONITORABLES -->
    <div class="card grid-full">
      <h3>Monitoring Watchlist</h3>
      <table>{monitor_html if monitor_html else '<tr><td>No monitorables</td></tr>'}</table>
    </div>

  </div>

  <div class="footer">
    {"Aegis 投研 OS v2 · 运行编号 " if _currency == "CNY" else "Aegis Research OS v2 · Run "}{run_id} · {"生成于 " if _currency == "CNY" else "Generated "}{now}<br>
    {"本报告仅供研究使用，不构成投资建议。" if _currency == "CNY" else "This report is for research purposes only. Not investment advice."}
  </div>

</div>

<script>
// Scenario Chart
new Chart(document.getElementById('scenarioChart'), {{
  type: 'bar',
  data: {{
    labels: {'["悲观", "基准", "乐观"]' if _currency == "CNY" else "['Bear', 'Base', 'Bull']"},
    datasets: [{{
      label: '{"每股价值 (¥)" if _currency == "CNY" else "Per Share Value ($)"}',
      data: [{bear:.1f}, {base:.1f}, {bull:.1f}],
      backgroundColor: ['rgba(239,68,68,0.7)', 'rgba(59,130,246,0.7)', 'rgba(34,197,94,0.7)'],
      borderRadius: 6,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      annotation: {{ annotations: {{}} }}
    }},
    scales: {{
      y: {{
        beginAtZero: true,
        grid: {{ color: 'rgba(148,163,184,0.1)' }},
        ticks: {{ color: '#94a3b8', callback: v => '{ccy}'+v }}
      }},
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ display: false }} }}
    }}
  }},
  plugins: [{{
    afterDraw: (chart) => {{
      const ctx = chart.ctx;
      const yScale = chart.scales.y;
      const xScale = chart.scales.x;
      const y = yScale.getPixelForValue({price:.1f});
      ctx.save();
      ctx.strokeStyle = '#eab308';
      ctx.lineWidth = 2;
      ctx.setLineDash([6,4]);
      ctx.beginPath();
      ctx.moveTo(xScale.left, y);
      ctx.lineTo(xScale.right, y);
      ctx.stroke();
      ctx.fillStyle = '#eab308';
      ctx.font = '12px Inter, sans-serif';
      ctx.fillText('Current: {ccy}{price:.0f}', xScale.right - 90, y - 6);
      ctx.restore();
    }}
  }}]
}});

// Radar Chart
new Chart(document.getElementById('radarChart'), {{
  type: 'radar',
  data: {{
    labels: ['Gross Margin', 'Op Margin', 'Net Margin', 'ROIC', 'ROE'],
    datasets: [{{
      label: '{entity}',
      data: [{gm:.1f}, {om:.1f}, {nm:.1f}, {roic:.1f}, {roe:.1f}],
      borderColor: 'rgba(59,130,246,0.8)',
      backgroundColor: 'rgba(59,130,246,0.15)',
      pointBackgroundColor: '#3b82f6',
      pointBorderColor: '#fff',
      pointRadius: 4,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    scales: {{
      r: {{
        beginAtZero: true,
        max: 100,
        ticks: {{ display: false }},
        grid: {{ color: 'rgba(148,163,184,0.15)' }},
        pointLabels: {{ color: '#94a3b8', font: {{ size: 11 }} }},
        angleLines: {{ color: 'rgba(148,163,184,0.1)' }},
      }}
    }},
    plugins: {{ legend: {{ display: false }} }}
  }}
}});

// Historical Trend Chart
{hist_chart_js}

// Historical Valuation Charts
{_build_valuation_chart_js(historical_valuation, _currency)}

// Peer Valuation Bar Charts
{_build_peer_chart_js(peer_chart_data, _currency)}
</script>
</body>
</html>'''

    if _currency == "CNY":
        _html = _localize_zh(_html)
    return _html
