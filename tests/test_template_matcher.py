"""
测试：模板匹配层 (TemplateMatcher)
覆盖：基础匹配、多尺度金字塔匹配、未找到场景、模板大于大图的边界情况。
"""

import numpy as np
import cv2
import pytest

from imgloc.matchers.template import TemplateMatcher


class TestTemplateMatcherBasic:
    def test_finds_exact_match(self, synthetic_scene_basic):
        scene, template, expected_center = synthetic_scene_basic
        matcher = TemplateMatcher(config={"scale_range": [0.9, 1.1], "scale_step": 0.05})
        result = matcher.match(scene, template, threshold=0.7)
        assert result.found is True
        assert result.method_used == "template"
        # 中心点应接近预期位置（允许若干像素误差）
        assert abs(result.center_point[0] - expected_center[0]) <= 5
        assert abs(result.center_point[1] - expected_center[1]) <= 5
        assert result.confidence >= 0.7

    def test_confidence_in_valid_range(self, synthetic_scene_basic):
        scene, template, _ = synthetic_scene_basic
        matcher = TemplateMatcher()
        result = matcher.match(scene, template, threshold=0.5)
        assert 0.0 <= result.confidence <= 1.0 + 1e-6

    def test_not_found_with_high_threshold_on_mismatched_template(self, synthetic_scene_basic):
        scene, _, _ = synthetic_scene_basic
        random_template = np.random.randint(0, 255, (40, 40, 3), dtype=np.uint8)
        matcher = TemplateMatcher(config={"scale_range": [0.9, 1.1], "scale_step": 0.1})
        result = matcher.match(scene, random_template, threshold=0.95)
        assert result.found is False
        assert result.method_used == "template"


class TestTemplateMatcherMultiScale:
    def test_finds_match_at_different_scale(self, synthetic_scene_scaled):
        """验证传统单遍多尺度遍历能应对模板与大图中目标尺寸不一致的情况。"""
        scene, template, _ = synthetic_scene_scaled
        # 显式关闭两级搜索，使用 scale_range/scale_step 的单遍遍历模式
        matcher = TemplateMatcher(
            config={"two_pass": False, "scale_range": [0.5, 1.6], "scale_step": 0.05}
        )
        result = matcher.match(scene, template, threshold=0.6)
        assert result.found is True
        # scale 应该接近 1/1.3（因为 template 是原始尺寸，scene 中图标放大了1.3倍）
        assert result.scale > 1.0  # 需要放大模板才能匹配放大后的图标


class TestTemplateMatcherTwoPass:
    """两级尺度搜索（two_pass，默认开启）：pass1 原始尺度速中，失败后粗搜+精搜。"""

    def test_pass1_hits_exact_match_at_scale_1(self, synthetic_scene_basic):
        """同分辨率精确匹配应在第1级以 scale=1.0 直接命中，不进入 fallback。"""
        scene, template, expected_center = synthetic_scene_basic
        matcher = TemplateMatcher()  # 默认 two_pass=True
        result = matcher.match(scene, template, threshold=0.7)
        assert result.found is True
        assert result.scale == pytest.approx(1.0)
        assert abs(result.center_point[0] - expected_center[0]) <= 5
        assert abs(result.center_point[1] - expected_center[1]) <= 5

    def test_fallback_finds_cross_resolution_scale(self):
        """
        跨分辨率场景：随机纹理图标放大 1.8 倍贴入大图。
        随机纹理在错误尺度下不自相似（1.0 尺度得分低），可可靠触发 fallback，
        粗搜定位尺度峰 + 邻域精搜后应以 scale≈1.8 且坐标精确命中。
        """
        rng = np.random.RandomState(42)
        icon = rng.randint(0, 256, (60, 60, 3), dtype=np.uint8)
        big_icon = cv2.resize(icon, (108, 108), interpolation=cv2.INTER_LINEAR)
        scene = np.full((300, 400, 3), 240, dtype=np.uint8)
        scene[96:204, 146:254] = big_icon  # 中心 (200, 150)

        matcher = TemplateMatcher()  # fallback_scale_range 默认 [0.5, 3.0]
        result = matcher.match(scene, icon, threshold=0.8)
        assert result.found is True
        assert result.scale == pytest.approx(1.8, abs=0.05)
        assert abs(result.center_point[0] - 200) <= 3
        assert abs(result.center_point[1] - 150) <= 3


class TestTemplateMatcherEdgeCases:
    def test_template_larger_than_source_returns_not_found(self):
        scene = np.zeros((50, 50, 3), dtype=np.uint8)
        template = np.zeros((100, 100, 3), dtype=np.uint8)
        matcher = TemplateMatcher()
        result = matcher.match(scene, template, threshold=0.5)
        assert result.found is False
        assert result.extra.get("reason") == "template_larger_than_source"

    def test_is_available_always_true(self):
        assert TemplateMatcher().is_available() is True

    def test_grayscale_mode_when_use_color_false(self, synthetic_scene_basic):
        scene, template, _ = synthetic_scene_basic
        matcher = TemplateMatcher(config={"use_color": False, "scale_range": [0.9, 1.1], "scale_step": 0.1})
        result = matcher.match(scene, template, threshold=0.6)
        assert result.found is True
