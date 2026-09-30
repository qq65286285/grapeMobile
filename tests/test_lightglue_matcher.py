"""
测试：LightGlue 深度学习兜底层
由于该层依赖可选包 (torch/lightglue)，在未安装环境下：
    - is_available() 应返回 False
    - match() 内部应捕获 MatcherNotAvailableError 并转为 not_found 结果
      （因为 Matcher.match() 基类会 catch 所有异常）
"""

import numpy as np

from imgloc.matchers.lightglue_matcher import LightGlueMatcher, _check_deps_available


class TestLightGlueAvailability:
    def test_check_deps_available_returns_bool(self):
        assert isinstance(_check_deps_available(), bool)

    def test_is_available_matches_dep_check(self):
        matcher = LightGlueMatcher()
        assert matcher.is_available() == _check_deps_available()


class TestLightGlueGracefulDegradation:
    def test_match_returns_not_found_when_deps_missing(self):
        """
        即使依赖缺失，调用 match()（而非 _match()）也不应抛出异常，
        因为基类 Matcher.match() 会捕获内部异常并转换为 not_found 结果，
        这正是 MultiStrategyLocator 能安全跳过本层的关键保障。
        """
        matcher = LightGlueMatcher()
        scene = np.zeros((100, 100, 3), dtype=np.uint8)
        template = np.zeros((20, 20, 3), dtype=np.uint8)
        result = matcher.match(scene, template, threshold=0.3)

        if not matcher.is_available():
            assert result.found is False
            assert result.method_used == "lightglue"
            assert "error" in result.extra or "error_type" in result.extra
