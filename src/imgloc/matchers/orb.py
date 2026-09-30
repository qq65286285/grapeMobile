"""
ORB 特征点匹配层 (ORB Feature Matching Layer)
================================================
ORB (Oriented FAST and Rotated BRIEF) 是速度快、无专利限制的特征点检测算法，
适合作为特征点匹配层的默认首选：对旋转、轻微缩放、部分遮挡有较好的鲁棒性，
且比 SIFT 快很多，适合高频轮询场景。

流程：ORB 检测关键点+描述子 -> BFMatcher/FLANN 做 KNN 匹配 -> ratio test 过滤
-> RANSAC 估计单应性矩阵 -> 计算目标框/中心点/角度/缩放。

参考设计思路：Airtest aircv 模块的特征匹配核心逻辑（独立重写，非直接依赖）。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from imgloc.base import Matcher
from imgloc.types import MatchResult
from imgloc.matchers._feature_common import (
    build_matcher,
    detect_and_compute,
    estimate_homography_and_build_result,
    knn_match_and_filter,
)


class OrbMatcher(Matcher):
    """ORB 特征点匹配层：速度快、无专利问题，作为特征点匹配的默认选择。"""

    name = "orb"

    def is_available(self) -> bool:
        # ORB 是 opencv-python 核心模块自带，始终可用
        return True

    def _match(
        self,
        image_source: np.ndarray,
        image_target: np.ndarray,
        threshold: float,
        **kwargs: Any,
    ) -> MatchResult:
        cfg = {**self.config, **kwargs}
        n_features = cfg.get("n_features", 500)
        matcher_type = cfg.get("matcher_type", "bf")
        min_match_count = cfg.get("min_match_count", 8)
        ransac_reproj_threshold = cfg.get("ransac_reproj_threshold", 5.0)
        knn_ratio = cfg.get("knn_ratio", 0.75)

        detector = cv2.ORB_create(nfeatures=n_features)

        kp_target, des_target = detect_and_compute(detector, image_target)
        kp_source, des_source = detect_and_compute(detector, image_source)

        if des_target is None or des_source is None or len(kp_target) < 2 or len(kp_source) < 2:
            return MatchResult.not_found(
                method_used=self.name,
                extra={"reason": "not_enough_keypoints"},
            )

        matcher = build_matcher(matcher_type, is_binary_descriptor=True)
        good_matches = knn_match_and_filter(matcher, des_target, des_source, knn_ratio)

        result = estimate_homography_and_build_result(
            kp_target=kp_target,
            kp_source=kp_source,
            good_matches=good_matches,
            target_shape=image_target.shape[:2],
            min_match_count=min_match_count,
            ransac_reproj_threshold=ransac_reproj_threshold,
            method_name=self.name,
        )

        if result.found and result.confidence < threshold:
            result.found = False
        return result
