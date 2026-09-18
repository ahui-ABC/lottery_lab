"""GBDT 训练与持久化（设计 §4.3 / 实现计划 T15）。

后端选择：默认 lightgbm；config.yaml `use_lightgbm: false` 改 sklearn.HistGradientBoostingClassifier。
"""
from __future__ import annotations

import joblib
import numpy as np


class GBDTModel:
    """统一封装 lightgbm 与 sklearn HGBDT；接口 fit / predict / predict_proba / save / load。"""

    def __init__(self, backend: str = "lightgbm", **kw):
        self.backend = backend
        self.kw = kw
        self.model = None
        self.classes_ = np.array([0, 1, 2])

    def fit(self, X: np.ndarray, y: np.ndarray) -> "GBDTModel":
        if self.backend == "lightgbm":
            from lightgbm import LGBMClassifier
            self.model = LGBMClassifier(
                objective="multiclass", num_class=3,
                n_estimators=self.kw.get("n_estimators", 200),
                learning_rate=self.kw.get("learning_rate", 0.05),
                random_state=self.kw.get("random_state", 0),
                verbosity=-1,
            )
        else:
            from sklearn.ensemble import HistGradientBoostingClassifier
            self.model = HistGradientBoostingClassifier(
                max_iter=self.kw.get("n_estimators", 200),
                learning_rate=self.kw.get("learning_rate", 0.05),
                random_state=self.kw.get("random_state", 0),
            )
        self.model.fit(X, y)
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        proba = self.model.predict_proba(X)
        cols = list(self.model.classes_)
        # 强制按 [0,1,2] 列序输出
        out = np.zeros((X.shape[0], 3))
        for i, c in enumerate(cols):
            out[:, int(c)] = proba[:, i]
        return out

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict(X)

    def save(self, path: str) -> None:
        joblib.dump({"backend": self.backend, "model": self.model}, path)

    def load(self, path: str) -> "GBDTModel":
        """就地装载；返回 self 便于链式使用 model = GBDTModel().load(path)。"""
        blob = joblib.load(path)
        self.backend = blob["backend"]
        self.model = blob["model"]
        return self
