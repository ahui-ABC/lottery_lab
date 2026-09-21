from lottery_lab.collectors import team_alias


SEED = {
    "曼城": "Man City",
    "曼联": "Man United",
    "阿森纳": "Arsenal",
    "利物浦": "Liverpool",
    "切尔西": "Chelsea",
}


def test_seed_lookup_exact():
    tid, conf = team_alias.match("曼城", seed=SEED)
    assert tid == "Man City"
    assert conf == 1.0


def test_fuzzy_alias():
    """'曼彻斯特城' 应模糊命中 '曼城' 对应的英文名。"""
    tid, conf = team_alias.match("曼彻斯特城", seed=SEED)
    assert tid == "Man City"
    assert 0.5 <= conf < 1.0


def test_unknown_returns_none():
    tid, conf = team_alias.match("不存在的队", seed=SEED)
    assert tid is None
    assert conf == 0.0


def test_confirm_persists(tmp_path):
    from lottery_lab.db import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.upsert(conn, "teams", {"name_en": "Arsenal", "name_cn": "阿森纳"}, ["name_en"])
    team_alias.confirm(conn, "阿仙奴", "Arsenal")
    assert team_alias.resolve(conn, "阿仙奴", seed=SEED) == "Arsenal"


def test_resolve_uses_db_first(tmp_path):
    """DB 中已确认的别名应优先于 seed 模糊匹配。"""
    from lottery_lab.db import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.upsert(conn, "teams", {"name_en": "Tottenham", "name_cn": "热刺"}, ["name_en"])
    team_alias.confirm(conn, "热仔", "Tottenham")  # 用户强制把"热仔"映射到热刺
    # 即使 seed 没"热仔"，resolve 应返回 Tottenham
    assert team_alias.resolve(conn, "热仔", seed=SEED) == "Tottenham"
