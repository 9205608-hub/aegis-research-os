const { useState, useEffect, useRef, useMemo } = React;

// ---------- Data ----------
// REPORT is either injected via window.REPORT (production pipeline output)
// or falls back to the 湖南裕能 301358 mock (offline preview / standalone open).

const MOCK_REPORT = {
  company: "湖南裕能",
  code: "301358",
  exchange: "SZ",
  sector: "新能源 · 磷酸铁锂正极材料",
  reportDate: "2026-04-16 15:18 UTC",
  runId: "run_20260416_144312",
  period: "FY2024 年报",
  tickerMark: "裕",
  confidence: "高",
  bias: "告警",
  pipelineDuration: "24m 37s",
  model: "deepseek-v4-pro",
  staleBanner: "最新可得财报为 FY2024（截至 2024-12-31），距今约 16 个月；本分析的价格动态基于实时行情，但财务基数落后约 1 个财年。",

  price: { last: 85.22, change: -1.34, changePct: -1.55, currency: "¥", asOf: "15:00 CST · 4/18", market: "CN" },
  rating: { word: "回避", tone: "avoid", target: 46.02, weighted: "概率加权", timeHorizon: "12 个月", riskLevel: "中高" },

  headline: "122 倍市盈率定价极端乐观复苏，但产能过剩与折旧刚性将利润率锁死在 3%–4%",
  lede: "一家 2024 年收入暴跌 45.4%、经营利润率压缩至 3.2%、自由现金流为 −23.4 亿元的周期型制造业公司，正在以 122 倍市盈率被市场交易。当前 ¥85.22 对应的估值，需要 2025–2026 年营收连续两年维持 46% 以上的高增长，并要求营业利润率从谷底 V 型反弹至 8% 以上——然而 DCF 概率加权价值仅 ¥46.02。",

  executiveParagraphs: [
    "公司当前估值隐含三个同时需要成立的极端假设：2025–2026 两年营收连续 45%+ 高增长、营业利润率从 3.2% 跃升至 8%+、资本成本显著下行。",
    "然而 2022–2024 年逆周期扩产导致非流动资产两年激增 78%、营收同期腰斩 45.4%，产能利用率显著低于 75%，<strong>¥16.1 亿年折旧摊销</strong>是压制利润率修复的结构性原因，而非短期周期变量。",
  ],
  coreCalloutHtml: "<strong>核心判断：</strong>即便在最乐观（bull）情景下，模型内在价值上限也仅 ¥56.32；概率加权 DCF 为 ¥46.02，较现价存在约 <strong>−46%</strong> 估值回归空间。市场定价已推至模型分布尾部之外。",

  quick: [
    { lbl: "市值", val: "¥518 亿", sub: "流通 ¥346 亿" },
    { lbl: "市盈率 (TTM)", val: "121.8×", sub: "行业中位 32×" },
    { lbl: "EV / EBITDA", val: "33.0×", sub: "历史均值 18×" },
    { lbl: "营业利润率", val: "3.2%", sub: "较周期高点 −680 bp" },
    { lbl: "自由现金流", val: "¥−23.4 亿", sub: "CFO / NI = −1.77" },
  ],

  scenarios: [
    { key: "bear", tag: "悲观情景", prob: 0.30, px: 40, narrative: "产能过剩恶化，宁德、比亚迪去库周期延长，价格战加剧；产能利用率长期低于 60%，Y1 营收负增长，¥45 亿债务的偿付与再融资压力上升。" },
    { key: "base", tag: "基准情景", prob: 0.45, px: 45, narrative: "国内新能源车与储能需求稳健，2025 年复苏；Y1 营收 +35%，产能利用率回升至 75%–80%，毛利率在 7%–8% 企稳，营业利润率缓慢修复至 3.5%–3.8%。" },
    { key: "bull", tag: "乐观情景", prob: 0.25, px: 56, narrative: "15–20 万车型对 LFP 需求旺盛，落后产能加速出清；公司凭头部客户绑定与规模成本优势份额提升，高压实密度产品拉动均价，营业利润率回升至 4.0%–4.5%。" },
  ],

  valuationPullquote: {
    text: "市场在押注 margin 修复，而非营收增长。即便输入共识预期 50% 营收增速，标准 DCF 乐观情景上限也仅 ¥56.32。",
    attrib: "变体分析师 · 估值缺口分解",
  },

  macro: {
    title: "LFP 正极：需求 α 化，供给仍在出清",
    subtitle: "Macro Analyst",
    paragraphs: [
      "中国新能源车渗透率已突破 50%，行业由 β 驱动转为 α 驱动——增量空间收窄，结构分化加剧。储能装机仍高增，但车用/储能毛利率存在结构差。",
      "LFP 正极材料行业名义产能规划已超 <strong>400 万吨/年</strong>，2026 年名义供给过剩率预计仍 &gt;35%。落后产能出清节奏由两个变量决定：锂源价格中枢、下游客户的价格条款传导窗口。",
    ],
    kpisTitle: "关键宏观变量",
    kpis: [
      { label: "10Y 国债收益率", value: "2.34%" },
      { label: "1Y LPR", value: "3.10%" },
      { label: "LFP 正极均价 (吨)", value: "¥3.88 万" },
      { label: "电池级碳酸锂", value: "¥7.5 万/吨" },
      { label: "行业产能利用率 (3 月)", value: "62%" },
    ],
    sharesTitle: "行业格局 · 份额",
    shares: [
      { name: "湖南裕能", value: 31, highlighted: true },
      { name: "德方纳米", value: 18 },
      { name: "万润新能", value: 12 },
      { name: "龙蟠科技", value: 9 },
      { name: "富临精工", value: 7 },
      { name: "其他", value: 23 },
    ],
  },

  financials: {
    title: "周期之上的失衡：利润表 vs 现金流表",
    subtitle: "Accounting Analyst · Business Analyst",
    paragraphs: [
      "2021–2022 年营收同比 +639% 和 +505%——典型锂电超级周期。2024 年同比 −45.4%，呈现大宗化工品过山车走势。",
    ],
    revTitle: "年度营收 · 亿元人民币",
    revHistory: [
      { y: "2020", v: 9.55 }, { y: "2021", v: 70.68 }, { y: "2022", v: 427.9 }, { y: "2023", v: 413.6 }, { y: "2024", v: 225.97 },
    ],
    revHighlightYear: "2024",
    revFootnote: "峰值 FY2022 ¥427.9 亿 · 谷底 FY2024 ¥226.0 亿 · 峰谷回撤 −47.2%",
    kpisTitle: "FY2024 关键指标",
    kpis: [
      { label: "营收", value: "¥226.0 亿" },
      { label: "净利润", value: "¥5.9 亿" },
      { label: "EBITDA", value: "¥23.2 亿" },
      { label: "经营现金流", value: "¥−10.4 亿", tone: "down" },
      { label: "自由现金流", value: "¥−23.4 亿", tone: "down" },
      { label: "毛利率", value: "7.8%", total: true },
      { label: "营业利润率", value: "3.2%" },
      { label: "ROIC", value: "3.5%" },
      { label: "ROE", value: "5.1%" },
      { label: "市盈率 (静态)", value: "121.8×" },
      { label: "EV / EBITDA", value: "33.0×" },
    ],
    calloutHtml: "<strong>会计分析师提示 · </strong>CFO / NI = <span class='mono'>−1.77</span>，应收账款 ¥60.5 亿叠加存货 ¥28.0 亿，\"有利润无现金\"的状态已持续两个报告期，盈利质量需以谨慎方式折现。",
  },

  dcf: {
    title: "十年现金流桥接与企业价值拆解",
    subtitle: "DCF Engine · FCFF 两阶段模型",
    unit: "亿元人民币",
    waccBase: 9.0,
    gBase: 2.5,
    sharesBase: 8.43,
    paragraphHtml: "FCFF 两阶段模型：Y1–Y10 明细期 + 永续期（g = 2.5%, WACC = 9.0%）。下表为基准情景的十年现金流投影与折现值。",
    projection: [
      [1, 305.1, 9.7, 16.1, 7.6, -17.6, 0.8, 5.3, 4.9],
      [2, 390.5, 12.6, 19.6, 10.0, -22.5, 0.9, 6.2, 5.3],
      [3, 499.8, 16.5, 24.1, 13.0, -28.8, 1.1, 7.2, 5.7],
      [4, 619.4, 20.9, 29.8, 16.5, -35.7, 1.2, 9.4, 6.8],
      [5, 742.2, 25.5, 37.0, 20.1, -42.8, 1.2, 13.1, 8.7],
      [6, 859.1, 30.1, 45.5, 23.8, -49.5, 1.2, 18.6, 11.4],
      [7, 959.4, 34.3, 51.9, 27.1, -55.3, 1.0, 22.7, 12.8],
      [8, 1032.1, 37.5, 58.5, 29.6, -59.5, 0.7, 27.9, 14.5],
      [9, 1068.2, 39.5, 64.6, 31.2, -61.5, 0.4, 33.9, 16.3],
      [10, 1105.6, 41.7, 69.8, 32.9, -63.7, 0.4, 38.6, 17.1],
    ],
    summary: {
      pvCashflows: 103.4, pvTerminal: 319.9, ev: 423.4, netDebt: 47.2, equity: 376.2, shares: 8.43, perShare: 44.61,
    },
  },

  agents: [
    { role: "会计分析师", name: "财报质量与现金流真实性", stance: "bear", score: 3.2, thesis: "尽管账面净利润 ¥5.9 亿，<strong>经营现金流为 −10.4 亿</strong>，CFO/NI = −1.77，呈现典型的\"有利润无现金\"特征；应收账款 ¥60.5 亿叠加存货 ¥28.0 亿吞噬了绝大多数账面利润。", pros: ["存货跌价准备计提可能不足", "收入确认时点与回款节奏脱节"], cons: ["2025 Q1 预计税费预缴下降可改善现金流", "客户结构稳定使坏账率保守"] },
    { role: "业务分析师", name: "产能利用率与资产错配", stance: "bear", score: 2.9, thesis: "<strong>非流动资产两年增幅 +78%，同期 2024 年营收暴跌 45.4%</strong>，资产周转率急剧恶化。¥16.1 亿年折旧摊销在产能利用率 <75% 时将持续压制毛利率与营业利润率修复空间。", pros: ["高压实密度等高端 LFP 产品占比提升预期", "与宁德、比亚迪长期合作关系稳固"], cons: ["行业产能出清节奏慢于预期", "逆周期扩产负反馈持续"] },
    { role: "估值分析师", name: "DCF 与多组估值三角", stance: "bear", score: 3.5, thesis: "基准 DCF 公允价值 ¥44.61；十年折现现金流 ¥103.4 亿 + 永续价值 ¥319.9 亿 = EV ¥423.4 亿。<strong>¥85.22 股价需营业利润率反弹至 8%+ 才能自洽</strong>，而模型显示此情景概率不足 25%。", pros: ["可比公司 PE 中位数 32×，本公司溢价 280%", "EV/EBITDA 33× 为 A 股 LFP 板块最高"], cons: ["永续增长率若上调 50bp，公允价值升至 ¥47", "WACC 下行 100bp 可贡献约 ¥18/股"] },
    { role: "宏观分析师", name: "行业周期与政策环境", stance: "neutral", score: 3.0, thesis: "新能源车渗透率已逾 50%，增速由 β 转入 α；储能装机虽高增但毛利率远低于车用。LFP 行业产能规划超 400 万吨/年，<strong>2026 年名义供给过剩率预计仍 >35%</strong>。", pros: ["国家以旧换新政策对 A00–A0 级车 LFP 渗透利好", "电化学储能容量电价机制逐步明朗"], cons: ["海外需求存在地缘摩擦不确定性", "锂源价格 2025 年企稳但 2026 年方向未定"] },
    { role: "风险分析师", name: "客户集中度与偿付能力", stance: "bear", score: 2.7, thesis: "<strong>前五大客户收入占比预计 >80%</strong>，宁德时代、比亚迪任一客户订单价格条款调整即可左右全年毛利率。有息负债 ¥45 亿，短期债务占比需验证。", pros: ["大客户长期框架合同提供下限保护", "有息负债加权利率低于行业"], cons: ["客户议价权极强", "再融资成本随估值回归上升"] },
    { role: "管理层分析师", name: "资本配置与战略纪律", stance: "bear", score: 3.1, thesis: "2022–2024 年密集扩产，资本开支 / 营收一度超 25%；当前估值水位下管理层未披露明确的 2025–2026 年 capex 节奏指引，<strong>资本配置与行业周期的逆向程度高于可比公司</strong>。", pros: ["核心团队在 LFP 技术路径上具长期积累", "股权激励方案未采用激进业绩条件"], cons: ["缺乏回购与减资信号", "高管薪酬结构信息披露有限"] },
    { role: "变体分析师", name: "市场共识与错价机制", stance: "bear", score: 3.8, thesis: "股价 ¥85.22 与基准 ¥44.61 之间约 ¥40.6 偏差中，<strong>营业利润率溢价占 ¥15–20，WACC/终值溢价占 ¥15–18，营收增长溢价仅占 ¥4–6</strong>。市场讨论的是\"V 型收入\"，真正在赌的是 margin 修复。", pros: ["margin 修复叙事在卖方研报中高度一致", "散户情绪指标处于 90 分位"], cons: ["一季报可能先行验证 margin 预期", "境外对冲基金近期净空头仓位上升"] },
  ],

  critics: [
    { name: "逻辑批评员", issues: 0 },
    { name: "财务批评员", issues: 0 },
    { name: "证据批评员", issues: 0 },
    { name: "行业批评员", issues: 0 },
    { name: "认知偏差批评员", issues: 1 },
    { name: "宏观一致性批评员", issues: 3 },
    { name: "市场批评员", issues: 1 },
    { name: "数值一致性批评员", issues: 0 },
    { name: "叙述事实核查员", issues: 0 },
    { name: "LLM 数值审核员", issues: 2 },
  ],

  thesis: {
    core: "公司当前股价 ¥85.22 对应 122× 市盈率和 33× EV/EBITDA，已将 2025–2026 年营收 V 型反弹与营业利润率从 3.2% 修复至 8% 以上的极端乐观情景充分定价；在行业结构性产能过剩、¥16.1 亿年折旧摊销刚性与负 FCF 背景下，DCF 概率加权价值仅 ¥46.02，股价存在约 45% 的估值回归空间。",
    variant: "市场定价隐含营业利润率将快速修复至 6%–8%（约 ¥15–20/股 估值贡献），但我们认为行业产能尚未出清，2025–2026 年营业利润率大概率徘徊在 3.5%–4.5%。",
    whyNow: "2026 年一季报将于 5 月 10 日披露，若营收恢复但毛利率/营业利润率仍低位，市场将被迫修正 margin 双击的极端乐观假设。",
    marketStory: "当前股价隐含：2025–2026 两年营收维持 45%+ 高增长 + 营业利润率跃升至 8%+ + 资本成本显著下行。三者需同时成立。",
    divergence: "我们认为营业利润率不会在销量回升后 V 型反弹至 8%+，而是因产能利用率长期 <75%、价格战持续与 ¥16.1 亿折旧刚性，结构性压缩在 3.5%–4.5%。",
    counter: "若 2025 H2 落后产能加速出清，LFP 价格企稳回升，公司产能利用率快速提升至 80%+，高压实密度产品占比提高，营业利润率反弹至 6%–8%，则 50%+ 营收高增长将快速稀释 122× 估值。",
  },

  sensitivity: {
    paragraphs: ["下表为每个单因子 ±1σ 冲击对基准每股价值的弹性。WACC 与营业利润率为估值主导变量，对应模型分布的尾部风险。"],
    driverTitle: "驱动因子弹性（冲击后每股值 vs 基准 ¥44.61）",
    baseValue: 44.61,
    rows: [8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0],
    cols: [2.0, 2.5, 3.0, 3.5, 4.0],
    matrix: [
      [43, 47, 51, 56, 62],
      [38, 41, 45, 49, 53],
      [34, 37, 40, 43, 47],
      [31, 33, 35, 38, 41],
      [28, 30, 32, 34, 36],
      [26, 27, 28, 30, 32],
      [23, 24, 26, 27, 29],
    ],
    footnote: "红色表示高于基准（更乐观），绿色反之。",
  },

  driverSensitivity: [
    { k: "加权资本成本 WACC", delta: -18.3, shock: 36 },
    { k: "营业利润率", delta: +10.6, shock: 49 },
    { k: "资本开支率", delta: -5.4, shock: 42 },
    { k: "永续增长率", delta: +5.2, shock: 47 },
    { k: "收入增速", delta: +4.7, shock: 47 },
    { k: "有效税率", delta: -2.8, shock: 43 },
  ],

  conclusion: {
    title: "等待行业产能出清与盈利质量改善的信号",
    subtitle: "Chief Analyst · 终稿",
    paragraphs: [
      "湖南裕能的看空逻辑建立在相互印证的证据链之上：周期型制造业公司被按照超级成长股定价，而支撑这一高估值的核心假设——<strong>营业利润率 V 型反弹至 8% 以上</strong>——与行业产能过剩、¥16.1 亿折旧刚性以及持续为负的自由现金流直接矛盾。",
      "DCF 概率加权价值 ¥46.02 为股价提供了下行锚点；即便最乐观情景下内在价值上限也仅 ¥56.32。随着 2026 年一季报将验证营收恢复与 margin 修复的真实力度，<strong>市场几乎没有容错空间的 122 倍市盈率将面临严峻考验</strong>。",
      "建议：<strong>回避当前估值</strong>，等待行业产能出清与盈利质量改善的信号明确后再做评估。",
    ],
    catalystsTitle: "未来 6 个月催化剂",
  },

  catalysts: [
    { date: "2026-05-10", title: "FY2026 Q1 业绩公告", impact: "高", note: "验证营收恢复节奏与 margin 修复初步信号" },
    { date: "2026-05-25", title: "股东大会 / 分红方案", impact: "中", note: "资本开支指引与产能投放计划问询窗口" },
    { date: "2026-07-15", title: "中报预告", impact: "高", note: "Q2 毛利率环比趋势将决定共识是否下修" },
    { date: "2026-09-30", title: "行业产能利用率高频数据", impact: "中", note: "LFP 产能利用率 75% 为关键门槛" },
  ],

  rail: {
    priceHistory: [82.1, 83.4, 84.0, 83.9, 85.2, 86.7, 87.1, 86.3, 85.8, 86.2, 85.9, 85.22],
    marketKvs: [
      { k: "今开", v: "85.90" },
      { k: "最高", v: "87.12" },
      { k: "最低", v: "85.04" },
      { k: "成交额", v: "8.72 亿" },
      { k: "换手率", v: "2.52%" },
      { k: "北向持股", v: "+0.14%", tone: "up" },
    ],
    openQuestions: [
      "各生产基地当前产能利用率",
      "2025 前五大客户价格条款",
      "有息负债期限结构与加权利率",
    ],
    biasStatus: "告警",
  },
};

const REPORT = (typeof window !== "undefined" && window.REPORT) ? window.REPORT : MOCK_REPORT;

const SECTIONS_ZH = [
  { id: "sec-summary",     num: "01", title: "执行摘要" },
  // Aegis 2.0 Phase 0：预期前沿是第一公民，排在 DCF 情景区块之前
  { id: "sec-pricedin",    num: "02", title: "市场在定价什么" },
  { id: "sec-valuation",   num: "03", title: "估值情景与结论" },
  { id: "sec-macro",       num: "04", title: "宏观与行业语境" },
  { id: "sec-financial",   num: "05", title: "财务解剖" },
  { id: "sec-dcf",         num: "06", title: "DCF 推导" },
  { id: "sec-agents",      num: "07", title: "七位专家观点" },
  { id: "sec-sensitivity", num: "08", title: "敏感性分析" },
  { id: "sec-conclusion",  num: "09", title: "结论与催化剂" },
];
const SECTIONS_EN = [
  { id: "sec-summary",     num: "01", title: "Executive summary" },
  { id: "sec-pricedin",    num: "02", title: "What the market is pricing" },
  { id: "sec-valuation",   num: "03", title: "Scenarios & fair value" },
  { id: "sec-macro",       num: "04", title: "Macro & industry context" },
  { id: "sec-financial",   num: "05", title: "Financial dissection" },
  { id: "sec-dcf",         num: "06", title: "DCF derivation" },
  { id: "sec-agents",      num: "07", title: "Seven-agent views" },
  { id: "sec-sensitivity", num: "08", title: "Sensitivity analysis" },
  { id: "sec-conclusion",  num: "09", title: "Conclusion & catalysts" },
];

// ---------- Helpers ----------

const CURR = () => REPORT.price.currency || "$";
const isCN = () => (REPORT.price && REPORT.price.market === "CN") || REPORT.exchange === "SH" || REPORT.exchange === "SZ";
// i18n shorthand: pick Chinese for A-share reports, English otherwise.
const L = (zh, en) => isCN() ? zh : en;

// Section definitions are a full catalog; the rendered TOC filters down
// to sections whose underlying data block exists in REPORT. Prevents
// "dead" TOC links (e.g. `03 · Macro` when REPORT.macro is null).
const _SECTIONS_FULL = isCN() ? SECTIONS_ZH : SECTIONS_EN;
const _SECTION_DATA_GUARD = {
  "sec-summary":     () => REPORT.headline || REPORT.thesis,
  "sec-pricedin":    () => REPORT.pricedIn && (REPORT.pricedIn.frontier || REPORT.pricedIn.regime || REPORT.pricedIn.events),
  "sec-valuation":   () => Array.isArray(REPORT.scenarios) && REPORT.scenarios.length > 0,
  "sec-macro":       () => REPORT.macro && (REPORT.macro.paragraphs?.length || REPORT.macro.kpis?.length),
  "sec-financial":   () => REPORT.financials && (REPORT.financials.revHistory?.length || REPORT.financials.kpis?.length),
  "sec-dcf":         () => REPORT.dcf && REPORT.dcf.projection?.length,
  "sec-agents":      () => Array.isArray(REPORT.agents) && REPORT.agents.length > 0,
  "sec-sensitivity": () => REPORT.sensitivity && REPORT.sensitivity.matrix?.length,
  "sec-conclusion":  () => REPORT.conclusion && REPORT.conclusion.paragraphs?.length,
};
const SECTIONS = _SECTIONS_FULL.filter(s => {
  const guard = _SECTION_DATA_GUARD[s.id];
  return guard ? guard() : true;
});

const pct = (n, d = 1) => `${n > 0 ? "+" : ""}${n.toFixed(d)}%`;
// AUDIT (2026-07): REPORT.price.last can legitimately be 0 (all quote
// sources down, or a stale replay cache without market data) — dividing
// by it rendered an unbounded "∞%" in the scenario cards / rating spread /
// valuation gap. The server-side inf/nan sanitizer (BUG-Y39) can't catch
// these because they're computed in the browser. Guard every px/base
// ratio through these helpers: base <= 0 → null / "—" (fmtNum's n/m
// semantics).
const safePctNum = (px, base) =>
  (typeof px === "number" && typeof base === "number" && base > 0 && Number.isFinite(px))
    ? ((px / base) - 1) * 100 : null;
const safePct = (px, base, d = 1, signed = false) => {
  const v = safePctNum(px, base);
  return v === null ? "—" : `${signed && v > 0 ? "+" : ""}${v.toFixed(d)}%`;
};
const fmtNum = (n, digits = 2) =>
  (n === null || n === undefined || (typeof n === "number" && !Number.isFinite(n)))
    ? "n/m"
    : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const fmtPx = (n) => `${CURR()}${fmtNum(n, n < 100 ? 2 : 2)}`;

// ---------- Small building blocks ----------

function StaleBanner() {
  if (!REPORT.staleBanner) return null;
  return (
    <div className="stale-banner" role="note">
      <div className="icon">!</div>
      <div>
        <div className="title">{L("数据时效提醒", "Data timeliness notice")}</div>
        <div className="body">{REPORT.staleBanner}</div>
      </div>
    </div>
  );
}

function TopBar() {
  return (
    <div className="topbar">
      <a href="search.html" className="brand" style={{textDecoration:"none", color:"inherit"}}>Aegis <span className="dim">{L("投研 OS", "Research OS")}</span></a>
      <div className="path">{L("研究 · 深度 · ", "Research · Deep · ")}{REPORT.company} · {REPORT.period}</div>
      <div className="right">
        <span>{L("运行", "Run")} <span className="mono" style={{color:"var(--text-2)"}}>{REPORT.runId}</span></span>
        <span>{L("置信度", "Confidence")} · {REPORT.confidence}</span>
        <span className="kbd">⌘ K</span>
      </div>
    </div>
  );
}

function TableOfContents({ active }) {
  return (
    <nav className="toc" aria-label={L("目录", "Contents")}>
      <div className="toc-label">{L("报告目录", "Contents")}</div>
      {SECTIONS.map(s => (
        <a key={s.id} href={`#${s.id}`} className={active === s.id ? "active" : ""}>
          <span className="num">{s.num}</span><span>{s.title}</span>
        </a>
      ))}

      <div className="toc-label" style={{marginTop: 32}}>{L("元信息", "Meta")}</div>
      <div className="meta">
        {L("生成时间", "Generated")} <span className="mono" style={{color:"var(--text-2)"}}>{REPORT.reportDate}</span><br/>
        {L("数据期", "Period")} <span className="mono" style={{color:"var(--text-2)"}}>{REPORT.period}</span><br/>
        {L("Pipeline 用时", "Pipeline runtime")} <span className="mono" style={{color:"var(--text-2)"}}>{REPORT.pipelineDuration || "—"}</span><br/>
        {L("模型", "Model")} <span className="mono" style={{color:"var(--text-2)"}}>{REPORT.model || "—"}</span>
      </div>

      <div className="toc-label" style={{marginTop: 32}}>{L("免责声明", "Disclaimer")}</div>
      <div className="meta">
        {L(`本报告由 Aegis 自动生成，不构成投资建议。数据源 akshare，模型输出经 10 位 critic 审核。`, `Auto-generated by Aegis. Not investment advice. Data source: EDGAR. Outputs reviewed by the critic panel.`)}
      </div>
    </nav>
  );
}

function RightRail() {
  const rail = REPORT.rail || {};
  const priceHistory = rail.priceHistory || [REPORT.price.last];
  const max = Math.max(...priceHistory), min = Math.min(...priceHistory);
  const chg = REPORT.price.change;
  const chgPct = REPORT.price.changePct;
  const hasChange = (chg !== 0 || chgPct !== 0);
  const up = chg > 0;
  // Prefer trend color when we have a real multi-point history (first vs last).
  // Fall back to intraday-change color, then neutral.
  const trendUp = priceHistory.length >= 2
    ? priceHistory[priceHistory.length - 1] > priceHistory[0]
    : null;
  const sparkColor = trendUp === null
    ? (hasChange ? (up ? "var(--up)" : "var(--down)") : "var(--text-3)")
    : (trendUp ? "var(--up)" : "var(--down)");
  const pts = priceHistory.map((p, i) => [i / Math.max(1, priceHistory.length - 1) * 100, (1 - (p - min) / Math.max(0.0001, max - min)) * 100]);
  const pathD = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]} ${p[1]}`).join(" ");

  return (
    <aside className="rail">
      <div className="rail-block">
        <div className="lbl">{L("实时行情", "Live quote")}</div>
        <div style={{display:"flex", alignItems:"baseline", justifyContent:"space-between", marginBottom: 4}}>
          <span className="mono" style={{fontSize: 22, color:"var(--text)"}}>{CURR()}{fmtNum(REPORT.price.last)}</span>
          {hasChange && (
            <span className="mono" style={{fontSize: 12, color: up ? "var(--up)" : "var(--down)"}}>
              {up ? "+" : ""}{chg.toFixed(2)} {up ? "+" : ""}{chgPct.toFixed(2)}%
            </span>
          )}
        </div>
        <div style={{fontSize: 11.5, color:"var(--text-4)", fontFamily:"var(--mono)", marginBottom: 10}}>{REPORT.price.asOf || "—"} · {L("延迟", "Delay")} &lt;3s</div>
        <svg viewBox="0 0 100 36" preserveAspectRatio="none" style={{width:"100%", height: 52}}>
          <defs>
            <linearGradient id="spark" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={sparkColor} stopOpacity="0.28"/>
              <stop offset="100%" stopColor={sparkColor} stopOpacity="0"/>
            </linearGradient>
          </defs>
          <path d={`${pathD} L 100 36 L 0 36 Z`} fill="url(#spark)"/>
          <path d={pathD} fill="none" stroke={sparkColor} strokeWidth="0.9"/>
        </svg>
        {(rail.marketKvs || []).map(kv => (
          <div className="rail-kv" key={kv.k}>
            <span className="k">{kv.k}</span>
            <span className={`v ${kv.tone || ""}`}>{kv.v}</span>
          </div>
        ))}
      </div>

      <div className="rail-block">
        <div className="lbl">{L("批评审核委员会", "Critic review panel")}</div>
        {REPORT.critics.map(c => {
          const dot = c.issues === 0 ? "ok" : c.issues <= 1 ? "warn" : "err";
          return (
            <div className="critic-row" key={c.name}>
              <span className={`dot ${dot}`}></span>
              <span className="cn">{c.name}</span>
              <span className="cc">{c.issues === 0 ? L("通过", "Pass") : `${c.issues} ${L("项", "issues")}`}</span>
            </div>
          );
        })}
        {rail.biasStatus && (
          <div style={{fontSize: 11, color: "var(--text-4)", fontFamily:"var(--mono)", marginTop: 8}}>{L("总体偏差检查", "Bias check")} · {rail.biasStatus}</div>
        )}
      </div>

      {(rail.openQuestions && rail.openQuestions.length > 0) && (
        <div className="rail-block">
          <div className="lbl">{L("待研究缺口", "Open questions")} · {L("前", "top")} {rail.openQuestions.length}</div>
          <div style={{fontSize: 12.5, color:"var(--text-2)", lineHeight: 1.6}}>
            {rail.openQuestions.map((q, i) => (
              <div key={i} style={{marginBottom: i === rail.openQuestions.length - 1 ? 0 : 8}}>· {q}</div>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}

function Verdict() {
  const wordClass = REPORT.rating.tone;
  // When DCF is n/m the target is null — guard the implied-return math so
  // we render a dash instead of crashing on `null / number`.
  const _hasTarget = (typeof REPORT.rating.target === "number") && Number.isFinite(REPORT.rating.target);
  // safePctNum guards price.last <= 0 (dead quote sources) → "—" not "∞%".
  const spread = _hasTarget ? safePctNum(REPORT.rating.target, REPORT.price.last) : null;
  const spreadColor = (spread !== null && spread >= 0) ? "var(--up)" : "var(--down)";
  return (
    <div className="verdict">
      <div className="rating">
        <span className="tag">{L("评级", "Rating")}</span>
        <span className={`word ${wordClass}`}>{REPORT.rating.word}</span>
      </div>
      <div style={{display:"flex", gap: 36, flexWrap:"wrap", alignItems:"center"}}>
        <div className="tgt">
          <span className="lbl">{L("目标价", "Target")} · {REPORT.rating.weighted || L("概率加权", "Probability-weighted")}</span>
          <span className="val">{_hasTarget ? `${CURR()}${fmtNum(REPORT.rating.target)}` : "n/m"}</span>
        </div>
        <div className="tgt">
          <span className="lbl">{L("隐含回报", "Implied return")}</span>
          <span className="val" style={{color: spreadColor}}>
            {spread === null ? "—" : `${spread > 0 ? "+" : ""}${spread.toFixed(1)}%`}
          </span>
        </div>
        <div className="tgt">
          <span className="lbl">{L("时间跨度", "Time horizon")}</span>
          <span className="val">{REPORT.rating.timeHorizon || L("12 个月", "12 months")}</span>
        </div>
        <div className="tgt">
          <span className="lbl">{L("风险等级", "Risk level")}</span>
          <span className="val">{REPORT.rating.riskLevel || L("中", "Medium")}</span>
        </div>
      </div>
    </div>
  );
}

function Hero() {
  // Only render a colored day-change pill when we actually have tick data.
  // Cached snapshots from replay don't carry day_change/day_change_pct, so
  // both fall back to 0 — showing "0.00 +0.00%" in red (false downside).
  const chg = REPORT.price.change;
  const chgPct = REPORT.price.changePct;
  const hasChange = (chg !== 0 || chgPct !== 0);
  const up = chg > 0;
  return (
    <header className="hero">
      <div className="ticker-row">
        <div className="ticker-mark">{REPORT.tickerMark || (REPORT.company || "·").slice(0, 1)}</div>
        <div className="ticker-meta">
          <div className="eyebrow">Aegis · {L("深度研究", "Deep Research")}</div>
          <div className="code">
            <span>{REPORT.code}{REPORT.exchange ? `.${REPORT.exchange}` : ""}</span>
            <span className="dot">·</span>
            <span>{REPORT.sector}</span>
            <span className="dot">·</span>
            <span>{REPORT.period}</span>
          </div>
        </div>
      </div>
      <h1 className="h-display" style={{maxWidth: 880, marginBottom: 18}}>
        {REPORT.riskWarning ? (
          <span style={{
            display: "inline-block",
            marginRight: 12,
            padding: "2px 10px",
            borderRadius: 4,
            background: "var(--down-soft, #fbe9e9)",
            color: "var(--down, #c0392b)",
            fontSize: "0.55em",
            fontWeight: 700,
            verticalAlign: "middle",
            letterSpacing: 1,
          }}>{REPORT.riskWarning}</span>
        ) : null}
        {REPORT.company}
      </h1>
      <div className="price-row">
        <div className="price"><span className="curr">{CURR()}</span>{fmtNum(REPORT.price.last)}</div>
        {hasChange && (
          <div className={`price-change ${up ? "up" : "down"}`}>
            {up ? "+" : ""}{chg.toFixed(2)} &nbsp; {pct(chgPct, 2)}
          </div>
        )}
        <div className="price-meta mono">{REPORT.price.asOf}</div>
      </div>
      <StaleBanner/>

      <div className="stat-strip">
        {REPORT.quick.map(s => (
          <div className="stat" key={s.lbl}>
            <div className="lbl">{s.lbl}</div>
            <div className="val">{s.val}</div>
            <div className="sub">{s.sub}</div>
          </div>
        ))}
      </div>

      <Verdict/>
    </header>
  );
}

function SectionHead({ idx, title, subtitle }) {
  return (
    <div className="section-head">
      <span className="idx">{idx}</span>
      <div>
        <h2 className="h-section">{title}</h2>
        {subtitle && <p style={{margin: "4px 0 0", color:"var(--text-3)", fontSize: 14}}>{subtitle}</p>}
      </div>
    </div>
  );
}

function ExecutiveSummary() {
  return (
    <section id="sec-summary">
      <SectionHead idx={L("01 · 执行摘要", "01 · Executive summary")} title={REPORT.headline} subtitle={L("首席分析师编辑层 · 合成器 → 编辑器", "Chief analyst · Synthesizer → Editor")} />
      <div className="reading wide">
        <p className="lede" style={{marginBottom: 28}}>{REPORT.lede}</p>

        <div className="prose">
          {(REPORT.executiveParagraphs || []).map((html, i) => (
            <p key={i} dangerouslySetInnerHTML={{__html: html}}/>
          ))}
        </div>

        {REPORT.coreCalloutHtml && (
          <div className="callout" dangerouslySetInnerHTML={{__html: REPORT.coreCalloutHtml}}/>
        )}

        <div className="thesis-grid">
          {(() => {
            const t = REPORT.thesis || {};
            return [
              ["core", L("核心论点", "Core thesis"), t.core],
              ["variant", L("我们的变体", "Our variant view"), t.variant],
              ["whyNow", L("为何是现在", "Why now"), t.whyNow],
              ["divergence", L("关键分歧", "Key divergence"), t.divergence],
              ["marketStory", L("市场正在讲的故事", "What the market is pricing"), t.marketStory],
              ["counter", L("反向论点", "Counter-thesis"), t.counter],
            ].filter(([_k, _h, v]) => v && v.trim()).map(([k, h, v]) => (
              <div className="thesis-item" key={k}><h5>{h}</h5><p>{v}</p></div>
            ));
          })()}
        </div>
      </div>
    </section>
  );
}

// ---------- Aegis 2.0 Phase 0: 市场在定价什么（第一公民区块） ----------
// 条件化预期前沿表 + 定价体制 + 验证点清单 + 近事件摘要。
// 设计红线 1：本区块只提供叙事框架——DCF 情景与差值在下一节照旧完整展示。
function PricedIn() {
  const p = REPORT.pricedIn;
  if (!p || (!p.frontier && !p.regime && !p.events)) return null;
  const f = p.frontier;
  const r = p.regime;
  const ev = p.events;
  const none = L("暂无", "n/a");
  return (
    <section id="sec-pricedin">
      <SectionHead
        idx={L("02 · 市场在定价什么", "02 · What the market is pricing")}
        title={p.title || L("市场在定价什么", "What the market is pricing")}
        subtitle={p.subtitle || L("条件化预期前沿 · 定价体制 · 验证点", "Expectations frontier · Pricing regime · Verification")}
      />

      {/* ── 条件化预期前沿表 ── */}
      <div style={{marginTop: 8}}>
        <div className="eyebrow" style={{marginBottom: 10}}>
          {L("条件化预期前沿", "Conditional expectations frontier")}
        </div>
        {f && (f.rows || []).length > 0 ? (
          <div style={{overflowX: "auto"}}>
            <p style={{margin: "0 0 10px", color: "var(--text-3)", fontSize: 13.5}}>{f.priceLine}</p>
            <table className="data">
              <thead>
                <tr>
                  <th>{L("利润率情景", "Margin scenario")}</th>
                  <th className="num">{L("终年利润率", "Terminal margin")}</th>
                  {(f.waccCols || []).map((c, i) => <th className="num" key={i}>{c}</th>)}
                </tr>
              </thead>
              <tbody>
                {f.rows.map((row, ri) => (
                  <tr key={ri}>
                    <td>{row.label}</td>
                    <td className="num">{row.margin}</td>
                    {(row.cells || []).map((cell, ci) => {
                      const noSol = (cell.flags || []).includes("no_solution");
                      const extreme = (cell.flags || []).includes("extreme");
                      const multi = (cell.flags || []).includes("multiple");
                      return (
                        <td className="num" key={ci} title={cell.diag || ""}
                            style={noSol ? {color: "var(--text-4)"} : (ci === 1 ? {color: "var(--accent)"} : {})}>
                          {extreme ? "⚠ " : ""}{cell.text}{multi ? L("（多解）", " (multi)") : ""}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="footnote">{f.note}</p>
          </div>
        ) : (
          <p className="footnote">{L("暂无（前沿求解不可用）", "n/a (frontier unavailable)")}</p>
        )}
      </div>

      {/* ── 定价体制标签 + 权重条 ── */}
      <div style={{marginTop: 28}}>
        <div className="eyebrow" style={{marginBottom: 10}}>{L("定价体制", "Pricing regime")}</div>
        {r ? (
          <div style={{padding: "18px 22px", background: "var(--bg-elev)", border: "1px solid var(--hairline)", borderRadius: 8}}>
            <div style={{display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap"}}>
              <span style={{fontSize: 17, color: "var(--text)", fontWeight: 600}}>{r.dominantLabel}</span>
              {r.mixed && (
                <span style={{fontSize: 12, color: "var(--text-3)", fontFamily: "var(--mono)"}}>
                  {L("混合体制 · 单一框架不足以解释现价", "mixed regime")}
                </span>
              )}
            </div>
            <div style={{marginTop: 14, display: "grid", gap: 6}}>
              {(r.weights || []).map(w => (
                <div key={w.key} style={{display: "grid", gridTemplateColumns: "110px 1fr 52px", alignItems: "center", gap: 10}}>
                  <span style={{fontSize: 12.5, color: "var(--text-3)"}}>{w.label}</span>
                  <div style={{height: 6, background: "var(--hairline)", borderRadius: 3, overflow: "hidden"}}>
                    <div style={{width: `${Math.min(100, w.pct)}%`, height: "100%", background: "var(--accent)"}}/>
                  </div>
                  <span className="mono" style={{fontSize: 12, color: "var(--text-3)", textAlign: "right"}}>{w.pct.toFixed(1)}%</span>
                </div>
              ))}
            </div>
            {r.narrative && (
              <p style={{margin: "14px 0 0", color: "var(--text-2)", fontSize: 13.5, lineHeight: 1.7}}>{r.narrative}</p>
            )}
          </div>
        ) : (
          <p className="footnote">{none}</p>
        )}
      </div>

      {/* ── 验证点清单（Phase 0：未核验 — 核验能力为 Phase 1 交付物） ── */}
      {(p.verification || []).length > 0 && (
        <div style={{marginTop: 28}}>
          <div className="eyebrow" style={{marginBottom: 10}}>{L("验证点清单", "Verification checklist")}</div>
          <div style={{display: "grid", gap: 8}}>
            {p.verification.map((v, i) => (
              <div key={i} style={{display: "flex", gap: 10, alignItems: "baseline"}}>
                <span style={{fontSize: 11, fontFamily: "var(--mono)", color: "var(--text-4)", border: "1px solid var(--hairline)", borderRadius: 4, padding: "1px 6px", whiteSpace: "nowrap"}}>
                  {v.status || L("未核验", "Unverified")}
                </span>
                <span style={{fontSize: 13.5, color: "var(--text-2)"}}>{v.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── 近事件摘要（近 90 天预告 / 公告） ── */}
      <div style={{marginTop: 28}}>
        <div className="eyebrow" style={{marginBottom: 10}}>
          {L("最新披露事件", "Recent disclosed events")}
          {ev && ev.asOf ? <span style={{marginLeft: 8, color: "var(--text-4)", textTransform: "none", letterSpacing: 0}}>{L("截至 ", "as of ")}{ev.asOf}</span> : null}
        </div>
        {ev ? (
          <div style={{display: "grid", gap: 18}}>
            <div>
              <div style={{fontSize: 12.5, color: "var(--text-3)", marginBottom: 6}}>{L("业绩预告", "Earnings forecasts")}</div>
              {(ev.forecasts || []).length > 0 ? (
                (ev.forecasts || []).map((fc, i) => (
                  <div key={i} style={{fontSize: 13.5, color: "var(--text-2)", padding: "4px 0"}}>
                    {L(`报告期 ${fc.period} · ${fc.type} · ${fc.indicator}：${fc.range}（公告日 ${fc.noticeDate}）`,
                       `${fc.period} · ${fc.type} · ${fc.indicator}: ${fc.range} (disclosed ${fc.noticeDate})`)}
                  </div>
                ))
              ) : (
                <div style={{fontSize: 13, color: "var(--text-4)"}}>{L("暂无业绩预告", "No forecasts")}</div>
              )}
            </div>
            <div>
              <div style={{fontSize: 12.5, color: "var(--text-3)", marginBottom: 6}}>{L("公告标题（按日期倒序）", "Announcements (date desc)")}</div>
              {(ev.announcements || []).length > 0 ? (
                (ev.announcements || []).map((a, i) => (
                  <div key={i} style={{fontSize: 13, color: "var(--text-2)", padding: "3px 0", display: "flex", gap: 10}}>
                    <span className="mono" style={{color: "var(--text-4)", whiteSpace: "nowrap"}}>{a.date}</span>
                    <span>{a.title}{a.category ? <span style={{color: "var(--text-4)"}}>（{a.category}）</span> : null}</span>
                  </div>
                ))
              ) : (
                <div style={{fontSize: 13, color: "var(--text-4)"}}>{L("暂无公告", "No announcements")}</div>
              )}
            </div>
            {ev.consensusLine && (
              <p className="footnote" style={{margin: 0}}>{L("一致预期（旁证口径）：", "Consensus (secondary evidence): ")}{ev.consensusLine}</p>
            )}
          </div>
        ) : (
          <p className="footnote">{none}</p>
        )}
      </div>
    </section>
  );
}

function ValuationBand() {
  const segs = REPORT.scenarios || [];
  const pq = REPORT.valuationPullquote;
  const curr = CURR();
  if (segs.length === 0) return null;
  return (
    <section id="sec-valuation">
      <SectionHead idx={L("03 · 估值情景与结论", "03 · Scenarios & fair value")} title={L("三情景分布与概率加权公允价值", "Three-scenario distribution & probability-weighted fair value")} subtitle={L("情景架构师 → 估值分析师", "Scenario Architect → Valuation Analyst")}/>
      <div className="scen-grid">
        {segs.map(s => (
          <div key={s.key} className={`scen-cell ${s.key}`}>
            <div className="hd">
              <span className="tag">{s.tag}</span>
              <span className="prob">{(s.prob * 100).toFixed(0)}%</span>
            </div>
            <div className="px">{curr}{(s.px || 0).toFixed(2)}</div>
            <div className="delta">{L("vs 现价", "vs spot")} {safePct(s.px, REPORT.price.last)}</div>
            <p>{s.narrative}</p>
          </div>
        ))}
      </div>
      <div className="prob-bar">
        {segs.map(s => <div key={s.key} className={`seg ${s.key}`} style={{width: `${s.prob * 100}%`}}/>)}
      </div>
      <div className="prob-legend">
        {segs.map(s => <span key={s.key}>{s.tag} · {(s.prob*100).toFixed(0)}%</span>)}
      </div>

      <div className="weighted">
        {(() => {
          const _t = REPORT.rating.target;
          const _hasT = (typeof _t === "number") && Number.isFinite(_t);
          // n/m branch: surface the asset-floor anchor (book value/share)
          // when DCF can't price the equity. Better than "n/m" alone — it
          // gives readers a defensible lower bound to anchor on.
          const _bv = REPORT.rating.bookValuePerShare;
          if (!_hasT) {
            return (
              <>
                <div>
                  <div className="k">{L("概率加权公允价值", "Probability-weighted fair value")}</div>
                  <div className="v">n/m</div>
                </div>
                <div>
                  <div className="k">{L("当前股价", "Current price")}</div>
                  <div className="v" style={{color:"var(--text-3)"}}>{curr}{REPORT.price.last.toFixed(2)}</div>
                </div>
                {_bv ? (
                  <div className="tgt">
                    <span className="lbl">{L("账面每股净资产", "Book value/share")}</span>
                    <span className="val">{curr}{_bv.toFixed(2)}</span>
                  </div>
                ) : null}
              </>
            );
          }
          const gap = safePctNum(_t, REPORT.price.last); // null when price.last <= 0
          return (
            <>
              <div>
                <div className="k">{L("概率加权公允价值", "Probability-weighted fair value")}</div>
                <div className="v">{curr}{_t.toFixed(2)}</div>
              </div>
              <div>
                <div className="k">{L("当前股价", "Current price")}</div>
                <div className="v" style={{color:"var(--text-3)"}}>{curr}{REPORT.price.last.toFixed(2)}</div>
              </div>
              <div className="delta" style={{color: (gap !== null && gap >= 0) ? "var(--up)" : "var(--down)"}}>
                {L("估值回归空间", "Valuation gap")} · {gap === null ? "—" : `${gap >= 0 ? "+" : ""}${gap.toFixed(1)}%`}
              </div>
            </>
          );
        })()}
      </div>

      {pq && (
        <div className="pullquote">
          {pq.text}
          <span className="attrib">{pq.attrib}</span>
        </div>
      )}
    </section>
  );
}

function Macro() {
  const m = REPORT.macro;
  if (!m) return null;
  const sharesMax = Math.max(...(m.shares || []).map(s => s.value), 1);

  return (
    <section id="sec-macro">
      <SectionHead idx={L("04 · 宏观与行业", "04 · Macro & industry")} title={m.title} subtitle={m.subtitle}/>
      <div className="reading wide prose">
        {(m.paragraphs || []).map((html, i) => (
          <p key={i} dangerouslySetInnerHTML={{__html: html}}/>
        ))}
      </div>

      <div style={{display:"grid", gridTemplateColumns: (m.shares && m.shares.length) ? "1fr 1fr" : "1fr", gap: 24, marginTop: 24}}>
        {(m.kpis && m.kpis.length > 0) && (
          <div style={{padding: "20px 22px", background: "var(--bg-elev)", border: "1px solid var(--hairline)", borderRadius: 8}}>
            <div className="eyebrow" style={{marginBottom: 10}}>{m.kpisTitle || L("关键宏观变量", "Key macro variables")}</div>
            <table className="data">
              <tbody>
                {m.kpis.map(k => (
                  <tr key={k.label}><td>{k.label}</td><td className="num">{k.value}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {(m.shares && m.shares.length > 0) && (
          <div style={{padding: "20px 22px", background: "var(--bg-elev)", border: "1px solid var(--hairline)", borderRadius: 8}}>
            <div className="eyebrow" style={{marginBottom: 10}}>{m.sharesTitle || L("行业格局 · 份额", "Industry share")}</div>
            <div className="barlist">
              {m.shares.map((r) => (
                <div className="barlist-row" key={r.name} style={{gridTemplateColumns: "110px 1fr 50px"}}>
                  <span className="label">{r.name}</span>
                  <div className="track" style={{position:"relative"}}>
                    <div style={{position:"absolute", left: 0, top: 0, height:"100%", width: `${r.value/sharesMax*100}%`, background: r.highlighted ? "var(--accent)" : "var(--surface-2)"}}/>
                  </div>
                  <span className="v" style={{textAlign:"right", fontFamily:"var(--mono)", color:"var(--text-2)"}}>{r.value}%</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function Financials() {
  const f = REPORT.financials;
  if (!f) return null;
  const rev = f.revHistory || [];
  // Y-axis baseline: when the series is tightly clustered (min/max > 0.5,
  // e.g. mature companies like AAPL where revenue varies within a narrow
  // band), a zero-based axis crushes all bars to roughly equal heights.
  // Use a compressed baseline padded 25 % below min so relative differences
  // are legible. For wide-range hockey-stick series (NVDA, 301358) keep
  // the zero baseline.
  const values = rev.map(r => r.v);
  const maxV = values.length ? Math.max(...values) : 1;
  const minV = values.length ? Math.min(...values) : 0;
  const useCompressed = maxV > 0 && minV / maxV > 0.5;
  const baseline = useCompressed ? Math.max(0, minV - 0.25 * (maxV - minV)) : 0;
  const span = (maxV - baseline) || 1;

  return (
    <section id="sec-financial">
      <SectionHead idx={L("05 · 财务解剖", "05 · Financial dissection")} title={f.title || L("财务解剖", "Financial dissection")} subtitle={f.subtitle}/>
      {(f.paragraphs && f.paragraphs.length > 0) && (
        <div className="reading wide prose">
          {f.paragraphs.map((html, i) => (
            <p key={i} dangerouslySetInnerHTML={{__html: html}}/>
          ))}
        </div>
      )}

      <div style={{display:"grid", gridTemplateColumns: rev.length > 0 ? "1.4fr 1fr" : "1fr", gap: 24, marginTop: 24, alignItems: "start"}}>
        {rev.length > 0 && (
          <div style={{padding: "24px 24px 28px", background: "var(--bg-elev)", border: "1px solid var(--hairline)", borderRadius: 8}}>
            <div className="eyebrow" style={{marginBottom: 18}}>{f.revTitle || L("年度营收", "Annual revenue")}</div>
            <div style={{display:"flex", alignItems:"flex-end", gap: 14, height: 180, borderBottom: "1px solid var(--hairline)", paddingBottom: 8}}>
              {rev.map(r => {
                const h = Math.max(2, (r.v - baseline) / span * 100);
                const highlight = r.y === f.revHighlightYear;
                return (
                  <div key={r.y} style={{flex: 1, height: "100%", display:"flex", flexDirection: "column", justifyContent: "flex-end", alignItems:"center", gap: 6}}>
                    <span style={{fontFamily:"var(--mono)", fontSize: 11, color: "var(--text-3)"}}>{r.v}</span>
                    <div style={{width: "100%", height: `${h}%`, background: highlight ? "var(--accent-bg)" : "var(--surface-2)", borderTop: `1px solid ${highlight ? "var(--accent)" : "var(--hairline-strong)"}`, borderRadius: "2px 2px 0 0"}}/>
                  </div>
                );
              })}
            </div>
            <div style={{display:"flex", gap: 14, marginTop: 6}}>
              {rev.map(r => <div key={r.y} style={{flex: 1, textAlign:"center", fontFamily:"var(--mono)", fontSize: 11, color: "var(--text-4)"}}>{r.y}</div>)}
            </div>
            {f.revFootnote && <p className="footnote" style={{marginTop: 14}}>{f.revFootnote}</p>}
          </div>
        )}

        {(f.kpis && f.kpis.length > 0) && (
          <div style={{padding: "18px 22px", background: "var(--bg-elev)", border: "1px solid var(--hairline)", borderRadius: 8}}>
            <div className="eyebrow" style={{marginBottom: 10}}>{f.kpisTitle || L("关键指标", "Key metrics")}</div>
            <table className="data">
              <tbody>
                {f.kpis.map((k, i) => {
                  const toneStyle = k.tone === "down" ? {color: "var(--down)"} : k.tone === "up" ? {color: "var(--up)"} : {};
                  return (
                    <tr key={i} className={k.total ? "total" : ""}>
                      <td style={k.tone ? {color:"var(--text-2)"} : {}}>{k.label}</td>
                      <td className="num" style={toneStyle}>{k.value}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {f.calloutHtml && (
        <div className="callout" style={{marginTop: 24}} dangerouslySetInnerHTML={{__html: f.calloutHtml}}/>
      )}
    </section>
  );
}

function DcfSection() {
  const d = REPORT.dcf;
  if (!d) return null;
  const rows = d.projection || [];
  const s = d.summary || {};
  const curr = CURR();
  return (
    <section id="sec-dcf">
      <SectionHead idx={L("06 · DCF 推导", "06 · DCF derivation")} title={d.title || L("DCF 推导", "DCF derivation")} subtitle={d.subtitle}/>
      {d.paragraphHtml && (
        <div className="reading wide prose">
          <p dangerouslySetInnerHTML={{__html: d.paragraphHtml}}/>
        </div>
      )}
      <div style={{marginTop: 20, overflowX:"auto"}}>
        <table className="data">
          <thead>
            <tr>
              <th>{L("年份", "Year")}</th>
              <th className="num">{L("营收", "Revenue")}</th>
              <th className="num">EBIT</th>
              <th className="num">D&A</th>
              <th className="num">{L("税后营业利润", "NOPAT")}</th>
              <th className="num">{L("资本开支", "Capex")}</th>
              <th className="num">ΔNWC</th>
              <th className="num">FCFF</th>
              <th className="num">{L("现值", "PV")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r[0]}>
                <td style={{color: "var(--text-3)"}}>Y{r[0]}</td>
                <td className="num">{r[1].toFixed(1)}</td>
                <td className="num">{r[2].toFixed(1)}</td>
                <td className="num">{r[3].toFixed(1)}</td>
                <td className="num">{r[4].toFixed(1)}</td>
                <td className="num" style={{color:"var(--text-3)"}}>{r[5].toFixed(1)}</td>
                <td className="num" style={{color:"var(--text-3)"}}>{r[6].toFixed(1)}</td>
                <td className="num">{r[7].toFixed(1)}</td>
                <td className="num" style={{color:"var(--accent)"}}>{r[8].toFixed(1)}</td>
              </tr>
            ))}
            <tr className="total">
              <td>{L("合计", "Total")}</td>
              <td className="num">—</td><td className="num">—</td><td className="num">—</td>
              <td className="num">—</td><td className="num">—</td><td className="num">—</td>
              <td className="num">—</td>
              <td className="num">{(s.pvCashflows || 0).toFixed(1)}</td>
            </tr>
          </tbody>
        </table>
        <p className="footnote">
          {isCN()
            ? `单位：${d.unit || "亿元人民币"}。基准情景 · WACC = ${d.waccBase}% · g∞ = ${d.gBase}% · ${d.sharesBase || s.shares} 亿股`
            /* TODO-Y9: even on the English branch, fall back to CNY unit text
               when CURR() reports ¥ — guards the edge case where market/
               exchange metadata is missing but currency is set. */
            : `Unit: ${d.unit || (CURR() === "¥" ? "CNY 亿" : "USD billions")}. Base case · WACC = ${d.waccBase}% · g∞ = ${d.gBase}% · ${(d.sharesBase || s.shares || 0).toFixed(2)}B shares`}
        </p>
      </div>

      <div style={{marginTop: 32, display:"grid", gridTemplateColumns:"1fr", gap: 0, padding: "24px 28px", background:"var(--bg-elev)", border:"1px solid var(--hairline)", borderRadius: 8}}>
        <div className="eyebrow" style={{marginBottom: 16}}>{L("企业价值推导", "Enterprise value bridge")}</div>
        <div style={{display:"grid", gridTemplateColumns:"repeat(6, 1fr)", gap: 0, fontFamily:"var(--mono)"}}>
          {(() => {
            const unit = isCN() ? "亿" : "B";
            return [
              [L("PV 明细期", "PV Explicit"),   `${curr}${(s.pvCashflows || 0).toFixed(1)}${unit}`],
              [L("＋", "+"),                      ""],
              [L("PV 永续价值", "PV Terminal"), `${curr}${(s.pvTerminal || 0).toFixed(1)}${unit}`],
              [L("＝ 企业价值", "= EV"),          `${curr}${(s.ev || 0).toFixed(1)}${unit}`],
              [L("− 净负债", "− Net Debt"),      `${curr}${(s.netDebt || 0).toFixed(1)}${unit}`],
              [L("÷ 股本 → 每股", "÷ Shares → per share"), `${curr}${(s.perShare || 0).toFixed(2)}`],
            ];
          })().map((kv, i) => (
            <div key={i} style={{padding: "14px 0", borderRight: i < 5 ? "1px solid var(--hairline)" : "none", paddingLeft: i === 0 ? 0 : 18}}>
              <div style={{fontSize: 11, color:"var(--text-4)", letterSpacing: "0.1em", textTransform:"uppercase", marginBottom: 6, fontFamily:"var(--mono)"}}>{kv[0]}</div>
              <div style={{fontSize: 17, color: i === 5 ? "var(--accent)" : "var(--text)"}}>{kv[1]}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function AgentsSection() {
  return (
    <section id="sec-agents">
      <SectionHead idx={L("07 · 专家观点", "07 · Expert views")} title={L("七位 Agent 的独立分析与分歧", "Seven independent agents — analyses & disagreements")} subtitle={L("Agent 小组 · 经 Critic 审核", "Agent Panel · Critic-Reviewed")}/>
      <div className="reading wide prose">
        <p>
          {(() => {
            const n = (REPORT.critics || []).length || 10;
            return isCN()
              ? `七位独立 LLM agent 从不同专业视角并行分析，每条输出经 ${n} 位 critic 审核后方可入库。下方 thesis 段为经合成器编辑层后的摘要。`
              : `Seven independent LLM agents analyze in parallel from distinct professional angles; every output is reviewed by ${n} critics before being admitted. Theses below are post-synthesizer summaries.`;
          })()}
        </p>
      </div>
      <div className="agent-list" style={{marginTop: 24}}>
        {REPORT.agents.map((a, i) => (
          <div className="agent" key={a.role + i}>
            <div className="hd">
              <span className="role">Agent · {String(i+1).padStart(2,"0")}</span>
              <span className="name">{a.role} — <span style={{color:"var(--text-3)", fontWeight: 400}}>{a.name}</span></span>
              <span className={`stance ${a.stance}`}>{a.stance === "bear" ? L("看空", "Bear") : a.stance === "bull" ? L("看多", "Bull") : L("中性", "Neutral")} · {a.score.toFixed(1)}</span>
            </div>
            {/* AUDIT-C1: a.thesis is raw LLM text (never HTML) — rendering it
                via dangerouslySetInnerHTML made any "<" (e.g. "ROIC<WACC")
                swallow the rest of the sentence and opened an injection
                surface. Text node, same as pros/cons/narrative. */}
            <p className="thesis">{a.thesis}</p>
            <div className="agent-points">
              <div className="col">
                <h5>{L("支持证据", "Supporting evidence")}</h5>
                <ul>{(a.pros || []).map((p, j) => <li key={j}>{p}</li>)}</ul>
              </div>
              <div className="col">
                <h5>{L("反向信号 / 可能反转", "Counter-signals / reversal risks")}</h5>
                <ul>{(a.cons || []).map((p, j) => <li key={j}>{p}</li>)}</ul>
              </div>
            </div>
            {/* BUG-Y29: deep-mode agents emit a free-form narrative
                supplement (~1500-2800 chars) BEYOND the structured thesis +
                pros/cons. Surface it as a collapsible block so readers who
                want the full reasoning can expand it. */}
            {a.narrative && a.narrative.length > 80 && (
              <details className="agent-narrative" style={{
                marginTop: 12,
                paddingTop: 12,
                borderTop: "1px dashed var(--hairline)",
              }}>
                <summary style={{
                  cursor: "pointer",
                  fontFamily: "var(--mono)",
                  fontSize: 11,
                  letterSpacing: "0.06em",
                  textTransform: "uppercase",
                  color: "var(--text-3)",
                }}>
                  {L("深度分析（展开）", "Deep analysis (expand)")} · {a.narrative.length} {L("字符", "chars")}
                </summary>
                <div className="prose" style={{marginTop: 10, whiteSpace: "pre-wrap"}}>
                  {a.narrative}
                </div>
              </details>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function SensitivitySection() {
  const s = REPORT.sensitivity;
  if (!s) return null;
  const flat = (s.matrix || []).flat();
  const hi = Math.max(...flat, 1), lo = Math.min(...flat, 0);
  const base = s.baseValue || (REPORT.dcf && REPORT.dcf.summary ? REPORT.dcf.summary.perShare : 0);
  const curr = CURR();

  const heatColor = (v) => {
    if (v >= base) {
      const k = Math.min(1, (v - base) / Math.max(0.0001, hi - base));
      return `color-mix(in oklab, var(--up) ${Math.round(k*42)}%, transparent)`;
    } else {
      const k = Math.min(1, (base - v) / Math.max(0.0001, base - lo));
      return `color-mix(in oklab, var(--down) ${Math.round(k*42)}%, transparent)`;
    }
  };

  return (
    <section id="sec-sensitivity">
      <SectionHead idx={L("08 · 敏感性", "08 · Sensitivity")} title={L("驱动因子弹性与 WACC × g 热力矩阵", "Driver elasticities & WACC × g heat-map")} subtitle={L("敏感性分析器", "Sensitivity Analyzer")}/>
      {(s.paragraphs || []).length > 0 && (
        <div className="reading wide prose">
          {s.paragraphs.map((html, i) => (
            <p key={i} dangerouslySetInnerHTML={{__html: html}}/>
          ))}
        </div>
      )}

      {(REPORT.driverSensitivity && REPORT.driverSensitivity.length > 0) && (
        <div style={{padding: "22px 24px", background: "var(--bg-elev)", border:"1px solid var(--hairline)", borderRadius: 8, marginTop: 20}}>
          <div className="eyebrow" style={{marginBottom: 14}}>{s.driverTitle || (isCN() ? `驱动因子弹性（冲击后每股值 vs 基准 ${curr}${base.toFixed(2)}）` : `Driver elasticity (per-share value after shock vs base ${curr}${base.toFixed(2)})`)}</div>
          <div className="barlist">
            {REPORT.driverSensitivity.map(d => {
              const w = Math.min(25, Math.abs(d.delta)) / 25 * 50;
              const pos = d.delta >= 0;
              return (
                <div className="barlist-row" key={d.k}>
                  <span className="label">{d.k}</span>
                  <div className="track">
                    <div className="midline"/>
                    {pos
                      ? <div className="fill pos" style={{left: "50%", width: `${w}%`}}/>
                      : <div className="fill neg" style={{right: "50%", width: `${w}%`}}/>
                    }
                  </div>
                  <span className={`v ${pos ? "up" : "down"}`}>
                    {pct(d.delta)} · {curr}{d.shock}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {(s.matrix && s.matrix.length > 0) && (
        <div style={{marginTop: 28}}>
          <div className="eyebrow" style={{marginBottom: 12}}>{L("WACC (行) × 永续增长率 g (列) · 每股公允价值", "WACC (rows) × terminal growth g (cols) · per-share fair value")}</div>
          <table className="heat">
            <thead>
              <tr>
                <th></th>
                {s.cols.map(c => <th key={c}>g = {c.toFixed(1)}%</th>)}
              </tr>
            </thead>
            <tbody>
              {s.matrix.map((row, i) => (
                <tr key={i}>
                  <td className="rh">WACC {s.rows[i].toFixed(1)}%</td>
                  {row.map((v, j) => (
                    <td key={j} className="hot" style={{background: heatColor(v), color: "var(--text)"}}>
                      {curr}{v}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="footnote">{s.footnote || (isCN() ? `基准每股 ${curr}${base.toFixed(2)}；红色表示高于基准（更乐观），绿色反之。` : `Base per-share ${curr}${base.toFixed(2)}; shading intensity = deviation from base.`)}</p>
        </div>
      )}
    </section>
  );
}

function Conclusion() {
  const c = REPORT.conclusion || {};
  return (
    <section id="sec-conclusion">
      <SectionHead idx={L("09 · 结论", "09 · Conclusion")} title={c.title || L("结论", "Conclusion")} subtitle={c.subtitle || L("首席分析师 · 终稿", "Chief Analyst · Final")}/>
      {(c.paragraphs && c.paragraphs.length > 0) && (
        <div className="reading wide prose">
          {c.paragraphs.map((html, i) => (
            <p key={i} dangerouslySetInnerHTML={{__html: html}}/>
          ))}
        </div>
      )}

      {(REPORT.catalysts && REPORT.catalysts.length > 0) && (
        <div style={{marginTop: 40}}>
          <div className="eyebrow" style={{marginBottom: 14}}>{c.catalystsTitle || L("未来催化剂", "Upcoming catalysts")}</div>
          <div style={{display:"grid", gridTemplateColumns:"1fr", gap: 1, background:"var(--hairline)", border:"1px solid var(--hairline)", borderRadius: 8, overflow:"hidden"}}>
            {REPORT.catalysts.map(ev => {
              const impactColor = (ev.impact === "高" || ev.impact === "High") ? "var(--up)" : "var(--warn)";
              const impactBg = (ev.impact === "高" || ev.impact === "High") ? "var(--up-soft)" : "var(--warn-soft)";
              return (
                <div key={ev.date + ev.title} style={{background:"var(--bg-elev)", padding: "16px 22px", display:"grid", gridTemplateColumns:"130px 1fr auto", gap: 24, alignItems:"center"}}>
                  <div className="mono" style={{color:"var(--text-3)", fontSize: 13}}>{ev.date}</div>
                  <div>
                    <div style={{fontFamily:"var(--serif)", fontSize: 16, color:"var(--text)", marginBottom: 2}}>{ev.title}</div>
                    <div style={{fontSize: 12.5, color:"var(--text-3)"}}>{ev.note}</div>
                  </div>
                  <div className="mono" style={{fontSize: 11, color: impactColor, padding: "3px 9px", background: impactBg, borderRadius: 3, letterSpacing: "0.06em"}}>
                    {L("影响", "Impact")} · {ev.impact}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div style={{marginTop: 56, paddingTop: 28, borderTop: "1px solid var(--hairline)", display:"flex", justifyContent:"space-between", gap: 20, fontFamily:"var(--mono)", fontSize: 11.5, color: "var(--text-4)", flexWrap:"wrap"}}>
        <span>Aegis · {L("投研 OS", "Research OS")} · v0.9.3</span>
        <span>{L("生成于", "Generated on")} {REPORT.reportDate}</span>
        <span>{L("Pipeline: 数据抓取 → 归一化 → DCF → 7 Agent → Critic → 合成 → 编辑", "Pipeline: Fetch → Normalize → DCF → 7 Agents → Critics → Synthesize → Edit")}</span>
        <span>{L("本报告不构成投资建议", "Not investment advice")}</span>
      </div>
    </section>
  );
}

function Dock({ onToggleTweaks }) {
  // Kick off a fresh pipeline run for this ticker via the local server.
  // Falls back to alert + no-op when the page is opened from file:// and
  // no server is reachable.
  const onRegenerate = async () => {
    const ticker = (REPORT.code || "").toString();
    if (!ticker) return;
    try {
      const r = await fetch("/api/run", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ticker}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const state = await r.json();
      window.location.href = `/progress?run_id=${encodeURIComponent(state.run_id)}&ticker=${encodeURIComponent(ticker)}`;
    } catch (e) {
      alert(L(`无法启动重新生成：${e.message}。请确认本地服务已启动。`,
              `Unable to start regeneration: ${e.message}. Local server must be running.`));
    }
  };
  const onShare = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      // Minimal feedback via button title toggle (avoid heavy toast UI)
      const btn = event?.currentTarget;
      if (btn) { const old = btn.title; btn.title = L("已复制链接", "Link copied"); setTimeout(() => { btn.title = old; }, 1500); }
    } catch {
      alert(L("复制失败，请手动复制浏览器地址栏", "Copy failed; please copy the URL manually"));
    }
  };
  return (
    <div className="dock">
      <button onClick={onRegenerate} title={L("重新生成报告", "Regenerate")}><Icon path="M21 12a9 9 0 1 1-3-6.7M21 4v5h-5"/>{L("重新生成", "Regenerate")}</button>
      <button onClick={onShare} title={L("分享", "Share")}><Icon path="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7 M16 6l-4-4-4 4 M12 2v14"/>{L("分享", "Share")}</button>
      <button onClick={onToggleTweaks} title="Tweaks"><Icon path="M3 6h18 M3 12h18 M3 18h18 M7 3v6 M12 9v6 M17 15v6"/>Tweaks</button>
      <button className="primary" title={L("导出 PDF", "Export PDF")} onClick={() => window.print()}><Icon path="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M9 14l3 3 3-3 M12 17V9"/>{L("导出 PDF", "Export PDF")}</button>
    </div>
  );
}
function Icon({ path }) {
  return <svg viewBox="0 0 24 24"><path d={path}/></svg>;
}

function TweaksPanel({ state, setState, on }) {
  const hues = [210, 175, 260, 45, 0];
  return (
    <div className={`tweaks ${on ? "on" : ""}`}>
      <h4>Tweaks</h4>
      <div className="tweak-row">
        <span>{L("强调色", "Accent")}</span>
        <div className="swatches">
          {hues.map(h => (
            <div key={h} className={`swatch ${state.accentHue === h ? "on" : ""}`}
                 style={{background: `oklch(0.72 0.075 ${h})`}}
                 onClick={() => setState({...state, accentHue: h})}/>
          ))}
        </div>
      </div>
      <div className="tweak-row">
        <span>{L("正文宽度", "Reading width")}</span>
        <div className="seg-ctl">
          {["tight", "comfortable", "wide"].map(w => (
            <button key={w} className={state.readingWidth === w ? "on" : ""}
                    onClick={() => setState({...state, readingWidth: w})}>
              {w === "tight" ? L("紧凑", "Tight") : w === "comfortable" ? L("舒适", "Comfortable") : L("宽松", "Wide")}
            </button>
          ))}
        </div>
      </div>
      <div className="tweak-row">
        <span>{L("右侧信息栏", "Right rail")}</span>
        <div className="seg-ctl">
          <button className={state.showRail ? "on" : ""} onClick={() => setState({...state, showRail: true})}>{L("显示", "Show")}</button>
          <button className={!state.showRail ? "on" : ""} onClick={() => setState({...state, showRail: false})}>{L("隐藏", "Hide")}</button>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [active, setActive] = useState("sec-summary");
  const [tweakState, setTweakState] = useState(typeof TWEAK_DEFAULTS !== "undefined" ? TWEAK_DEFAULTS : {accentHue: 210, readingWidth: "comfortable", showRail: false});
  const [tweaksVisible, setTweaksVisible] = useState(false);

  // Drive market-specific color semantics (涨跌颜色) from CSS.
  // US/global defaults: green = up, red = down. A-share flips them.
  useEffect(() => {
    document.body.dataset.market = isCN() ? "CN" : "US";
  }, []);

  useEffect(() => {
    const onScroll = () => {
      const p = document.getElementById("scroll-progress");
      const h = document.documentElement;
      const pct = (h.scrollTop / (h.scrollHeight - h.clientHeight)) * 100;
      if (p) p.style.width = `${pct}%`;

      const pos = window.scrollY + 140;
      let cur = SECTIONS[0].id;
      for (const s of SECTIONS) {
        const el = document.getElementById(s.id);
        if (el && el.offsetTop <= pos) cur = s.id;
      }
      setActive(cur);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    document.documentElement.style.setProperty("--accent", `oklch(0.72 0.075 ${tweakState.accentHue})`);
    document.documentElement.style.setProperty("--accent-dim", `oklch(0.52 0.055 ${tweakState.accentHue})`);
    document.documentElement.style.setProperty("--accent-bg", `oklch(0.28 0.04 ${tweakState.accentHue} / 0.35)`);
  }, [tweakState]);

  const mainWidthStyle = {
    padding: tweakState.readingWidth === "tight" ? "36px 40px 160px" :
             tweakState.readingWidth === "wide"  ? "36px 72px 160px" : "36px 56px 160px"
  };

  return (
    <>
      <TopBar/>
      <div className="layout" style={{gridTemplateColumns: tweakState.showRail ? "260px minmax(0,1fr) 280px" : "260px minmax(0,1fr)"}}>
        <TableOfContents active={active}/>
        <main className="main" style={mainWidthStyle}>
          <Hero/>
          <ExecutiveSummary/>
          {/* Aegis 2.0 Phase 0：预期前沿第一公民——放在 DCF 情景之前 */}
          <PricedIn/>
          <ValuationBand/>
          <Macro/>
          <Financials/>
          <DcfSection/>
          <AgentsSection/>
          <SensitivitySection/>
          <Conclusion/>
        </main>
        {tweakState.showRail && <RightRail/>}
      </div>
      <Dock onToggleTweaks={() => setTweaksVisible(v => !v)}/>
      <TweaksPanel state={tweakState} setState={setTweakState} on={tweaksVisible}/>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
