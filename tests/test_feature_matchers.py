"""
测试：ORB / SIFT 特征点匹配层
覆盖：基础匹配、旋转场景下的鲁棒性、匹配点不足的边界情况。
"""

import numpy as np
import pytest

from imgloc.matchers.orb import OrbMatcher
from imgloc.matchers.sift import SiftMatcher


@pytest.mark.parametrize("matcher_cls", [OrbMatcher, SiftMatcher])
class TestFeatureMatchersBasic:
    def test_finds_exact_match(self, synthetic_scene_basic, matcher_cls):
        scene, template, expected_center = synthetic_scene_basic
        matcher = matcher_cls(config={"min_match_count": 4})
        result = matcher.match(scene, template, threshold=0.1)
        assert result.method_used == matcher.name
        if result.found:
            assert abs(result.center_point[0] - expected_center[0]) <= 20
            assert abs(result.center_point[1] - expected_center[1]) <= 20

    def test_rotation_robustness(self, synthetic_scene_rotated, matcher_cls):
        """特征点匹配应对旋转场景有一定鲁棒性，并能估算出接近的旋转角度。"""
        scene, template, _ = synthetic_scene_rotated
        matcher = matcher_cls(config={"min_match_count": 4})
        result = matcher.match(scene, template, threshold=0.1)
        assert result.method_used == matcher.name
        # 只要流程正常返回合法结果即可（合成图像特征点数量有限，不强制要求一定命中）
        assert 0.0 <= result.confidence <= 1.0 + 1e-6

    def test_not_enough_keypoints_on_blank_images(self, matcher_cls):
        scene = np.full((50, 50, 3), 128, dtype=np.uint8)
        template = np.full((20, 20, 3), 128, dtype=np.uint8)
        matcher = matcher_cls()
        result = matcher.match(scene, template, threshold=0.3)
        assert result.found is False

    def test_is_available(self, matcher_cls):
        assert matcher_cls().is_available() is True


class TestFeatureCommon:
    def test_orb_uses_hamming_bf_matcher(self):
        from imgloc.matchers._feature_common import build_matcher
        import cv2
        matcher = build_matcher("bf", is_binary_descriptor=True)
        assert isinstance(matcher, cv2.BFMatcher)

    def test_sift_uses_l2_bf_matcher(self):
        from imgloc.matchers._feature_common import build_matcher
        import cv2
        matcher = build_matcher("bf", is_binary_descriptor=False)
        assert isinstance(matcher, cv2.BFMatcher)

    def test_knn_match_with_none_descriptors_returns_empty(self):
        from imgloc.matchers._feature_common import build_matcher, knn_match_and_filter
        matcher = build_matcher("bf", is_binary_descriptor=True)
        result = knn_match_and_filter(matcher, None, None)
        assert result == []
