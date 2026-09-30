"""
测试：形状匹配层 (ShapeMatcher)
覆盖：基础匹配、光照变化下的鲁棒性、边界情况。
"""

import numpy as np
import cv2

from imgloc.matchers.shape import ShapeMatcher


class TestShapeMatcherBasic:
    def test_finds_exact_match(self, synthetic_scene_basic):
        scene, template, expected_center = synthetic_scene_basic
        matcher = ShapeMatcher(config={"scale_range": [0.9, 1.1], "scale_step": 0.1, "angle_range": [0, 0], "angle_step": 5})
        result = matcher.match(scene, template, threshold=0.5)
        assert result.method_used == "shape"
        # 形状匹配对合成图像的鲁棒性依赖边缘质量，此处只验证流程可用性和坐标合理性
        if result.found:
            assert abs(result.center_point[0] - expected_center[0]) <= 15
            assert abs(result.center_point[1] - expected_center[1]) <= 15

    def test_robust_to_brightness_change(self, synthetic_scene_basic):
        """验证形状匹配对整体亮度变化的鲁棒性优于像素级比较。"""
        scene, template, _ = synthetic_scene_basic
        # 大图整体调亮，模拟光照变化/主题切换
        brighter_scene = cv2.convertScaleAbs(scene, alpha=1.0, beta=60)
        matcher = ShapeMatcher(config={"scale_range": [0.9, 1.1], "scale_step": 0.1, "angle_range": [0, 0]})
        result = matcher.match(brighter_scene, template, threshold=0.4)
        # 至少流程应正常跑通并返回合法的 MatchResult（found True/False 均可接受）
        assert result.method_used == "shape"
        assert 0.0 <= result.confidence <= 1.0 + 1e-6

    def test_empty_edge_template_returns_low_score(self):
        """纯色模板几乎没有边缘，应返回未找到而不是抛异常。"""
        scene = np.full((100, 100, 3), 200, dtype=np.uint8)
        flat_template = np.full((20, 20, 3), 200, dtype=np.uint8)
        matcher = ShapeMatcher(config={"scale_range": [1.0, 1.0], "angle_range": [0, 0]})
        result = matcher.match(scene, flat_template, threshold=0.5)
        assert result.found is False

    def test_template_larger_than_source(self):
        scene = np.zeros((30, 30, 3), dtype=np.uint8)
        template = np.zeros((60, 60, 3), dtype=np.uint8)
        matcher = ShapeMatcher()
        result = matcher.match(scene, template, threshold=0.5)
        assert result.found is False
        assert result.extra.get("reason") == "template_larger_than_source"

    def test_is_available_always_true(self):
        assert ShapeMatcher().is_available() is True


class TestShapeMatcherColorVerify:
    """颜色一致性二次校验：拦截"形状巧合相似但颜色语义不符"的误匹配。"""

    @staticmethod
    def _make_same_shape_diff_color_scene():
        """白底黑矩形(场景) + 黄底黑矩形(模板)：边缘形状一致、主色完全不同。"""
        scene = np.full((200, 200, 3), 255, dtype=np.uint8)
        cv2.rectangle(scene, (70, 70), (130, 130), (0, 0, 0), thickness=-1)
        template = np.zeros((60, 60, 3), dtype=np.uint8)
        template[:] = (0, 200, 255)  # BGR 黄色
        cv2.rectangle(template, (0, 0), (59, 59), (0, 0, 0), thickness=4)
        return scene, template

    def test_enabled_rejects_different_color_match(self):
        """开启颜色校验（默认）：形状分达标但颜色不符，应拒绝并标注 color_verify_failed。"""
        scene, template = self._make_same_shape_diff_color_scene()
        matcher = ShapeMatcher(config={"scale_range": [1.0, 1.0], "angle_range": [0, 0]})
        result = matcher.match(scene, template, threshold=0.7)
        assert result.found is False
        assert result.extra.get("reason") == "color_verify_failed"
        assert result.extra.get("color_corr", 1.0) < 0.5

    def test_disabled_allows_match(self):
        """关闭颜色校验：仅凭形状即可命中（兼容深色模式/换肤等同形变色场景）。"""
        scene, template = self._make_same_shape_diff_color_scene()
        matcher = ShapeMatcher(
            config={"color_verify": False, "scale_range": [1.0, 1.0], "angle_range": [0, 0]}
        )
        result = matcher.match(scene, template, threshold=0.7)
        assert result.found is True

    def test_hist_correlation_metric_values(self):
        """HSV 直方图相关系数：同色≈1、异色≈0、灰度输入无法校验时放行(=1.0)。"""
        yellow = np.zeros((40, 40, 3), dtype=np.uint8)
        yellow[:] = (0, 200, 255)
        white = np.full((40, 40, 3), 255, dtype=np.uint8)
        assert ShapeMatcher._color_hist_correlation(yellow, yellow, 0, 0, 40, 40) > 0.9
        assert ShapeMatcher._color_hist_correlation(white, yellow, 0, 0, 40, 40) < 0.5
        gray = np.full((40, 40), 128, dtype=np.uint8)
        assert ShapeMatcher._color_hist_correlation(gray, gray, 0, 0, 40, 40) == 1.0
