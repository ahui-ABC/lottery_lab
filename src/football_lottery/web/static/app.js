// 全站公共前端工具。页面自己的逻辑仍写在模板的 {% block scripts %} 里内联。
//
// 这里的东西是因为**重复了三份**才抽出来的：手绘盈亏曲线原本在 jc.html 与
// overview.html 各有一份逐行相同的拷贝，money/pct 也散在多个模板里。
// 数字彩页面还要再画一条曲线，不抽就是第三份。
//
// sparkPath 刻意做成**纯函数**（不碰 DOM），这样能用 node 直接跑单测 ——
// 这个重构动到几个没法用浏览器肉眼验证的页面，必须有自动化兜底。
// 见 tests/test_web_assets.py。
window.__fl = window.__fl || {};

/** 金额格式化：null → "—"，负数写成 "-¥12.00" 而不是 "¥-12.00"。 */
window.__fl.money = function (v) {
  if (v == null) return "—";
  const n = Number(v);
  if (!isFinite(n)) return "—";
  return (n < 0 ? "-¥" : "¥") + Math.abs(n).toFixed(2);
};

/** 比率格式化：0.753 → "75.3%"；null → "—"。 */
window.__fl.pct = function (v, digits) {
  if (v == null) return "—";
  const n = Number(v);
  if (!isFinite(n)) return "—";
  return (n * 100).toFixed(digits == null ? 1 : digits) + "%";
};

/** 保留两位小数，去掉浮点尾巴（0.30000000000000004 → 0.3）。 */
function _r2(v) {
  return Math.round(v * 100) / 100;
}

/**
 * 把序列换算成 SVG 路径。纯函数，不碰 DOM。
 *
 * @param {Array<{label: string, value: number}>} points 时序升序，至少 2 个点
 * @param {number} w viewBox 宽
 * @param {number} h viewBox 高
 * @param {number} pad 内边距
 * @returns {object|null} 点不足 2 个时返回 null（调用方负责显示占位文案）
 */
window.__fl.sparkPath = function (points, w, h, pad) {
  if (!Array.isArray(points) || points.length < 2) return null;
  const vals = points.map((p) => Number(p.value) || 0);
  // 零轴一定要留在可视范围内：只按数据范围取 min/max 的话，
  // 全正或全负的曲线会把零轴顶出画面，读者就分不清「赚」和「亏」了
  const min = Math.min(0, ...vals);
  const max = Math.max(0, ...vals);
  const span = (max - min) || 1;
  const innerW = w - pad * 2;
  const innerH = h - pad * 2;
  const x = (i) => _r2(pad + (i * innerW) / (points.length - 1));
  const y = (v) => _r2(h - pad - ((v - min) / span) * innerH);

  const line = points
    .map((p, i) => `${i ? "L" : "M"}${x(i)},${y(vals[i])}`)
    .join(" ");
  const area = `${line} L${x(points.length - 1)},${y(0)} L${x(0)},${y(0)} Z`;
  const last = vals[vals.length - 1];

  return {
    line: line,
    area: area,
    zeroY: y(0),
    x1: x(0),
    x2: x(points.length - 1),
    last: last,
    color: last >= 0 ? "var(--ok)" : "var(--bad)",
    firstLabel: String(points[0].label == null ? "" : points[0].label),
    lastLabel: String(points[points.length - 1].label == null
      ? "" : points[points.length - 1].label),
  };
};

/**
 * 画盈亏曲线（零轴 + 面积 + 折线 + 首尾图例）。
 *
 * @param {SVGElement} svgEl
 * @param {HTMLElement} legendEl
 * @param {Array} points 同 sparkPath
 * @param {{emptyText?: string}} [opts]
 */
window.__fl.renderSpark = function (svgEl, legendEl, points, opts) {
  const o = opts || {};
  const W = 600;
  const H = 96;
  const PAD = 6;
  svgEl.replaceChildren();
  const geo = window.__fl.sparkPath(points, W, H, PAD);
  if (!geo) {
    legendEl.textContent = o.emptyText || "数据不足，还画不出曲线";
    return;
  }
  const NS = "http://www.w3.org/2000/svg";
  const add = function (tag, attrs) {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    svgEl.appendChild(el);
  };
  add("line", { x1: geo.x1, y1: geo.zeroY, x2: geo.x2, y2: geo.zeroY,
                class: "spark-zero" });
  add("path", { d: geo.area, fill: geo.color, class: "spark-area" });
  add("path", { d: geo.line, stroke: geo.color, class: "spark-line" });
  legendEl.innerHTML = `<span>${geo.firstLabel}</span>` +
    `<span>累计 ${window.__fl.money(geo.last)}</span>` +
    `<span>${geo.lastLabel}</span>`;
};
