"""GBDT 训练与持久化测试（实现计划 T15）。"""
import numpy as np

from football_lottery.features.build import FEATURE_COLUMNS
from football_lottery.models import gbdt


def _synth_dataset(n: int = 300, seed: int = 0):
    """三簇可分的人造数据 → 三分类。"""
    rng = np.random.default_rng(seed)
    n_per = n // 3
    nf = len(FEATURE_COLUMNS)
    c1 = rng.normal(loc=-3.0, scale=0.7, size=(n_per, nf))
    c2 = rng.normal(loc=0.0, scale=0.7, size=(n_per, nf))
    c3 = rng.normal(loc=3.0, scale=0.7, size=(n_per, nf))
    X = np.vstack([c1, c2, c3])
    y = np.array([0] * n_per + [1] * n_per + [2] * n_per)
    perm = rng.permutation(len(y))
    return X[perm], y[perm]


def test_fit_predict_shape_and_sum():
    X, y = _synth_dataset()
    model = gbdt.GBDTModel(backend="sklearn")
    model.fit(X, y)
    probs = model.predict_proba(X[:5])
    assert probs.shape == (5, 3)
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert (probs >= 0).all()


def test_predict_accuracy_above_chance():
    X, y = _synth_dataset(n=600)
    model = gbdt.GBDTModel(backend="sklearn")
    model.fit(X, y)
    preds = model.predict(X)
    acc = (preds == y).mean()
    # 对三簇可分数据，准确率应明显优于 1/3
    assert acc > 0.6


def test_save_load_roundtrip(tmp_path):
    X, y = _synth_dataset(n=120)
    model = gbdt.GBDTModel(backend="sklearn")
    model.fit(X, y)
    p = tmp_path / "m.joblib"
    model.save(str(p))
    model2 = gbdt.GBDTModel(backend="sklearn")
    model2.load(str(p))
    p1 = model.predict_proba(X[:3])
    p2 = model2.predict_proba(X[:3])
    assert np.allclose(p1, p2)
