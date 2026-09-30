"""
SIFT 特征点匹配层 (SIFT Feature Matching Layer)
=================================================
SIFT (Scale-Invariant Feature Transform) 精度通常高于 ORB，尤其在
纹理复杂、缩放/旋转幅度较大的场景下表现更稳健，但速度较慢，
因此在责任链中作为 ORB 失败后的"精度更高的备选层"。

自 OpenCV 4.4+ 起，SIFT 的专利已到期，cv2.SIFT_create() 在标准
opencv-python 包中即可直接使用，无需 opencv-contrib-python。

流程与 ORB 一致：SIFT 检测关键点+描述子 -> FLANN/BF 做 KNN 匹配 ->
ratio test 过滤 -> RANSAC 估计单应性矩阵 -> 计算目标框/中心点/角度/缩放。
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


class SiftMatcher(Matcher):
    """SIFT 特征点匹配层：精度更高，作为 ORB 失败后的备选算法。"""

    name = "sift"

    def is_available(self) -> bool:
        # cv2.SIFT_create 在 opencv-python>=4.4 中已内置（专利到期），始终可用
        return hasattr(cv2, "SIFT_create")

    def _match(
        self,
        image_source: np.ndarray,
        image_target: np.ndarray,
        threshold: float,
        **kwargs: Any,
    ) -> MatchResult:
        cfg = {**self.config, **kwargs}
        n_features = cfg.get("n_features", 0)
        matcher_type = cfg.get("matcher_type", "flann")
        min_match_count = cfg.get("min_match_count", 8)
        ransac_reproj_threshold = cfg.get("ransac_reproj_threshold", 5.0)
        knn_ratio = cfg.get("knn_ratio", 0.75)

        detector = cv2.SIFT_create(nfeatures=n_features)

        kp_target, des_target = detect_and_compute(detector, image_target)
        kp_source, des_source = detect_and_compute(detector, image_source)

        if des_target is None or des_source is None or len(kp_target) < 2 or len(kp_source) < 2:
            return MatchResult.not_found(
                method_used=self.name,
                extra={"reason": "not_enough_keypoints"},
            )

        matcher = build_matcher(matcher_type, is_binary_descriptor=False)
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
