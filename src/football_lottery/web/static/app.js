// 仅占位（页面级 JS 已通过 {% block scripts %} 内联）。这里放未来全局工具函数。
window.__fl = window.__fl || {};
window.__fl.formatPct = function(p) { return p == null ? "-" : (p * 100).toFixed(2) + "%"; };
