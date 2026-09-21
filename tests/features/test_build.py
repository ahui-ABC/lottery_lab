"""特征构建测试（实现计划 T14）。防泄漏测试是核心验收。"""
from lottery_lab.features.build import FEATURE_COLUMNS, FeatureBuilder


def test_no_leakage_by_truncation(all_matches):
    """同一场比赛：全量构建 vs 截断到该场之前的构建，特征必须完全一致。"""
    fb = FeatureBuilder()
    full = fb.build(all_matches)
    k = len(all_matches) // 2
    prefix = FeatureBuilder().build(all_matches[: k + 1])
    mid = all_matches[k]["id"]
    row_full = full.get(mid)
    row_pref = prefix.get(mid)
    assert row_full is not None and row_pref is not None
    for col in FEATURE_COLUMNS:
        v1 = row_full[col]
        v2 = row_pref[col]
        if isinstance(v1, float):
            assert abs(v1 - v2) < 1e-9, f"特征 {col} 疑似使用了未来数据"
        else:
            assert v1 == v2, f"特征 {col} 疑似使用了未来数据"


def test_feature_columns_present(all_matches):
    feats = FeatureBuilder().build(all_matches)
    one = next(iter(feats.values()))
    for col in FEATURE_COLUMNS:
        assert col in one, f"缺少特征列 {col}"


def test_build_forward_for_unplayed(all_matches):
    """未赛对阵也能出特征行——当期预测依赖此入口。"""
    history, tail = all_matches[:-3], all_matches[-3:]
    upcoming = [{**m, "result": None, "home_goals": None, "away_goals": None}
                for m in tail]
    out = FeatureBuilder().build_forward(history, upcoming)
    assert len(out) == 3
    one = next(iter(out.values()))
    for col in FEATURE_COLUMNS:
        assert col in one
