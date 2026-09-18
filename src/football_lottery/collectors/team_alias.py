"""队名映射（设计文档 §3.4 / 实现计划 T4）。

入口：`match(cn, seed=SEED)` / `confirm(conn, cn, en)` / `resolve(conn, cn, seed=SEED)`。

种子表覆盖足彩常用球队的中英对照（约 60–80 支），再加字段匹配 + 编辑距离模糊匹配；
人工确认后写入 `team_alias` 表，下次直接命中。
"""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher

# ---- 别名表：足彩常用的全称、昵称、旧称 → 种子主名 -----------------------------
# 模糊匹配难命中"全称 vs 简称"的情况，这里显式给出映射。
ALIASES: dict[str, str] = {
    "曼彻斯特城": "曼城", "曼彻斯特联": "曼联", "红魔": "曼联",
    "兵工厂": "阿森纳", "枪手": "阿森纳",
    "红军": "利物浦",
    "蓝军": "切尔西",
    "拜仁慕尼黑": "拜仁",
    "多特": "多特蒙德", "大黄蜂": "多特蒙德",
    "药厂": "勒沃库森", "沃尔夫斯堡": "沃尔夫斯堡",
    "门兴格拉德巴赫": "门兴",
    "云达不来梅": "不莱梅", "云达不莱梅": "不莱梅",
    "沙尔克04": "沙尔克",
    "国际": "国际米兰", "国米": "国际米兰", "米兰": "AC米兰",
    "尤文": "尤文图斯", "老妇人": "尤文图斯",
    "那波利": "那不勒斯",
    "红狼": "罗马",
    "蓝鹰": "拉齐奥",
    "女神": "亚特兰大", "真蓝黑": "亚特兰大",
    "紫百合": "佛罗伦萨",
    "银河战舰": "皇马", "皇家马德里": "皇马",
    "巴萨": "巴塞罗那", "红蓝军团": "巴塞罗那",
    "床单军团": "马竞", "马德里竞技": "马竞",
    "黄色潜水艇": "比利亚雷亚尔",
    "大巴黎": "巴黎圣日耳曼", "巴黎": "巴黎圣日耳曼", "PSG": "巴黎圣日耳曼",
    "荷兰豪门": "阿贾克斯", "贾府": "阿贾克斯",
    "埃因霍温": "埃因霍温",
    "葡超本菲卡": "本菲卡", "葡超波尔图": "波尔图",
}


# ---- 种子：足彩常见队伍的中文名 → football-data 英文名 -----------------------------
SEED: dict[str, str] = {
    # 英超
    "曼城": "Man City", "曼联": "Man United", "阿森纳": "Arsenal",
    "利物浦": "Liverpool", "切尔西": "Chelsea", "热刺": "Tottenham",
    "纽卡斯尔": "Newcastle", "阿斯顿维拉": "Aston Villa", "布莱顿": "Brighton",
    "西汉姆": "West Ham", "水晶宫": "Crystal Palace", "埃弗顿": "Everton",
    "伯恩茅斯": "Bournemouth", "狼队": "Wolves", "诺丁汉森林": "Nott'm Forest",
    "富勒姆": "Fulham", "布伦特福德": "Brentford", "伊普斯维奇": "Ipswich",
    "南安普顿": "Southampton", "莱斯特": "Leicester", "利兹联": "Leeds",
    # 英冠
    "伯明翰": "Birmingham", "利兹": "Leeds", "谢菲尔德联": "Sheffield United",
    "谢菲尔德星期三": "Sheffield Weds", "桑德兰": "Sunderland",
    "诺维奇": "Norwich", "西布罗姆维奇": "West Brom", "斯托克城": "Stoke",
    "斯旺西": "Swansea", "加迪夫城": "Cardiff", "哈德斯菲尔德": "Huddersfield",
    "女王公园巡游者": "QPR", "米尔沃尔": "Millwall", "普雷斯顿": "Preston",
    # 德甲
    "拜仁": "Bayern Munich", "多特蒙德": "Dortmund", "莱比锡": "RB Leipzig",
    "勒沃库森": "Leverkusen", "法兰克福": "Frankfurt", "沃尔夫斯堡": "Wolfsburg",
    "斯图加特": "Stuttgart", "门兴": "M'gladbach", "不莱梅": "Werder Bremen",
    "沙尔克": "Schalke 04", "柏林赫塔": "Hertha", "科隆": "Koln",
    "霍芬海姆": "Hoffenheim", "弗赖堡": "Freiburg", "奥格斯堡": "Augsburg",
    "美因茨": "Mainz", "柏林联合": "Union Berlin",
    # 意甲
    "国际米兰": "Inter", "AC米兰": "Milan", "尤文图斯": "Juventus",
    "那不勒斯": "Napoli", "罗马": "Roma", "拉齐奥": "Lazio",
    "亚特兰大": "Atalanta", "佛罗伦萨": "Fiorentina", "都灵": "Torino",
    "博洛尼亚": "Bologna", "乌迪内斯": "Udinese", "萨索洛": "Sassuolo",
    "恩波利": "Empoli", "蒙扎": "Monza", "维罗纳": "Verona",
    "热那亚": "Genoa", "莱切": "Lecce", "斯佩齐亚": "Spezia",
    "卡利亚里": "Cagliari", "克雷莫内塞": "Cremonese",
    # 西甲
    "皇马": "Real Madrid", "巴塞罗那": "Barcelona", "马竞": "Ath Madrid",
    "塞维利亚": "Sevilla", "皇家社会": "Real Sociedad", "比利亚雷亚尔": "Villarreal",
    "贝蒂斯": "Betis", "毕尔巴鄂": "Ath Bilbao", "瓦伦西亚": "Valencia",
    "赫塔费": "Getafe", "奥萨苏纳": "Osasuna", "塞尔塔": "Celta",
    "马略卡": "Mallorca", "拉斯帕尔马斯": "Las Palmas",
    # 法甲
    "巴黎圣日耳曼": "Paris SG", "马赛": "Marseille", "里昂": "Lyon",
    "摩纳哥": "Monaco", "里尔": "Lille", "雷恩": "Rennes",
    "尼斯": "Nice", "朗斯": "Lens", "斯特拉斯堡": "Strasbourg",
    "蒙彼利埃": "Montpellier",
    # 荷甲
    "阿贾克斯": "Ajax", "埃因霍温": "PSV", "费耶诺德": "Feyenoord",
    # 葡超
    "本菲卡": "Benfica", "波尔图": "Porto", "葡萄牙体育": "Sporting",
}


_SUFFIX_RE = re.compile(
    r"(足球俱乐部|足球会|俱乐部|俱乐部足球|FC|CF|F\.?C\.?|CF)$", re.IGNORECASE
)


def normalize(name: str) -> str:
    """统一比较形态：去前后空白、常见后缀、全角 → 半角。"""
    s = name.strip()
    s = s.replace("　", "").replace(" ", "")
    # 全角字母 / 数字 → 半角
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF21 <= code <= 0xFF3A:  # Ａ–Ｚ
            out.append(chr(code - 0xFEE0))
        elif 0xFF41 <= code <= 0xFF5A:  # ａ–ｚ
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    s = "".join(out)
    # 去除尾部 "足球俱乐部" / "FC" 等
    while True:
        new = _SUFFIX_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    return s


_THRESHOLD = 0.86


def match(name_cn: str, seed: dict[str, str] | None = None):
    """返回 (english_name, confidence) 或 (None, 0.0)。

    流程：
    1) normalize 后查 seed → (en, 1.0)
    2) normalize 后查 ALIASES → 找到后跳到对应种子 → (en, 0.95)
    3) 否则对 seed 全键算编辑距离相似度，最高且 >= THRESHOLD → (en, score)
    """
    if seed is None:
        seed = SEED
    norm = normalize(name_cn)
    if norm in seed:
        return seed[norm], 1.0
    if norm in ALIASES:
        primary = ALIASES[norm]
        if primary in seed:
            return seed[primary], 0.95
    if not seed:
        return None, 0.0
    best_en, best_score = None, 0.0
    for cn, en in seed.items():
        s = SequenceMatcher(None, norm, cn).ratio()
        if s > best_score:
            best_en, best_score = en, s
    if best_score >= _THRESHOLD:
        return best_en, best_score
    return None, 0.0


def confirm(conn: sqlite3.Connection, name_cn: str, name_en: str) -> None:
    """人工确认队名映射：写 `team_alias(confirmed=1)`。"""
    row = conn.execute(
        "SELECT id FROM teams WHERE name_en=?", (name_en,)
    ).fetchone()
    if not row:
        cur = conn.execute(
            "INSERT INTO teams(name_en, name_cn) VALUES(?, NULL)", (name_en,)
        )
        team_id = cur.lastrowid
    else:
        team_id = row["id"]
    conn.execute(
        """INSERT INTO team_alias(name_cn, team_id, confirmed, updated_at)
           VALUES(?, ?, 1, datetime('now'))
           ON CONFLICT(name_cn) DO UPDATE SET
             team_id=excluded.team_id,
             confirmed=1,
             updated_at=datetime('now')""",
        (name_cn, team_id),
    )
    conn.commit()


def resolve(conn: sqlite3.Connection, name_cn: str, seed: dict[str, str] | None = None) -> str | None:
    """先查 DB 的 confirmed=1 别名 → 没命中再用 match() 兜底（不写入 DB）。"""
    row = conn.execute(
        """SELECT t.name_en FROM team_alias a
           JOIN teams t ON t.id = a.team_id
           WHERE a.name_cn=? AND a.confirmed=1""",
        (name_cn,),
    ).fetchone()
    if row:
        return row["name_en"]
    en, _ = match(name_cn, seed=seed)
    return en


def list_pending(conn: sqlite3.Connection, candidates: list[str], seed: dict[str, str] | None = None
                 ) -> list[tuple[str, str | None, float]]:
    """返回 [(中文名, 命中英文名 or None, confidence), ...] 供人工确认界面使用。"""
    out: list[tuple[str, str | None, float]] = []
    for cn in candidates:
        en, conf = match(cn, seed=seed)
        out.append((cn, en, conf))
    return out
