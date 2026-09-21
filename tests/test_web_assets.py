"""前端公共工具的单测（用 node 跑）。

`web/static/app.js` 里的 `sparkPath` 被三个页面共用（竞彩 / 概览 / 数字彩），
而我无法用浏览器肉眼逐个验证 —— 所以把几何计算做成纯函数，在这里用 node 兜底。
没装 node 就跳过，不让 Python 侧的测试环境被前端工具链绑死。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "src" / "lottery_lab" / \
    "web" / "static" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="需要 node 才能测前端纯函数")


def _run(expr: str):
    """在 node 里加载 app.js（用一个假的 window）后求值 expr，返回 JSON。"""
    script = (
        "global.window = {};\n"
        f"require({json.dumps(str(APP_JS))});\n"
        f"console.log(JSON.stringify((() => {{ return ({expr}); }})()));\n"
    )
    # 必须显式指定 utf-8：Windows 上 subprocess 默认按 gbk 解码，
    # node 输出的 ¥ / — 会直接炸在解码这一步
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         encoding="utf-8", errors="replace", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_app_js_loads_without_dom():
    """纯逻辑部分（money/pct/sparkPath）不能依赖 document —— 否则没法单测。"""
    assert _run("window.__fl.money(12.5)") == "¥12.50"


@pytest.mark.parametrize("value,expected", [
    (0, "¥0.00"),
    (12.5, "¥12.50"),
    (-12.5, "-¥12.50"),
    (-0.01, "-¥0.01"),
])
def test_money_formats_negative_sign_outside_the_symbol(value, expected):
    """负号要在 ¥ 外面（-¥12.50），不能是 ¥-12.50。"""
    assert _run(f"window.__fl.money({value})") == expected


def test_money_and_pct_handle_null():
    assert _run("window.__fl.money(null)") == "—"
    assert _run("window.__fl.pct(null)") == "—"


def test_pct_formatting():
    assert _run("window.__fl.pct(0.7531)") == "75.3%"
    assert _run("window.__fl.pct(1)") == "100.0%"
    assert _run("window.__fl.pct(0)") == "0.0%"


@pytest.mark.parametrize("points", ["[]", "[{label:'a', value:1}]", "null"])
def test_spark_path_needs_at_least_two_points(points):
    """少于 2 个点画不出线，返回 null 让调用方显示占位文案，而不是画出个假的。"""
    assert _run(f"window.__fl.sparkPath({points}, 600, 96, 6)") is None


def test_spark_path_keeps_zero_axis_inside_the_chart():
    """全正的数据也要把零轴留在画面里 —— 否则分不清赚和亏。"""
    geo = _run("window.__fl.sparkPath("
               "[{label:'a',value:100},{label:'b',value:200}], 600, 96, 6)")
    assert geo["zeroY"] == 90.0          # h - pad，零轴贴底但仍在画面内
    assert geo["last"] == 200
    assert geo["color"] == "var(--ok)"


def test_spark_path_all_negative_stays_in_frame():
    geo = _run("window.__fl.sparkPath("
               "[{label:'a',value:-100},{label:'b',value:-50}], 600, 96, 6)")
    assert geo["color"] == "var(--bad)"
    assert 0 <= geo["zeroY"] <= 96


def test_spark_path_all_zero_does_not_divide_by_zero():
    geo = _run("window.__fl.sparkPath("
               "[{label:'a',value:0},{label:'b',value:0}], 600, 96, 6)")
    assert "NaN" not in geo["line"]
    assert "NaN" not in geo["area"]
    assert geo["line"].startswith("M")


def test_spark_path_line_and_area_shapes():
    geo = _run("window.__fl.sparkPath("
               "[{label:'a',value:0},{label:'b',value:10},{label:'c',value:-10}],"
               " 600, 96, 6)")
    assert geo["line"].startswith("M")
    assert geo["line"].count("L") == 2          # 三个点 → 两段线
    assert geo["area"].endswith("Z")            # 面积要闭合
    assert geo["firstLabel"] == "a"
    assert geo["lastLabel"] == "c"
    assert geo["color"] == "var(--bad)"         # 末值为负


def test_spark_path_x_spans_the_padding():
    geo = _run("window.__fl.sparkPath("
               "[{label:'a',value:0},{label:'b',value:1}], 600, 96, 6)")
    assert geo["x1"] == 6.0
    assert geo["x2"] == 594.0


def test_spark_path_missing_values_are_treated_as_zero():
    geo = _run("window.__fl.sparkPath("
               "[{label:'a'},{label:'b',value:5}], 600, 96, 6)")
    assert "NaN" not in geo["line"]
