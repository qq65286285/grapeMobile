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
    def test_match_returns_not_found_when_deps_missing(self, monkeypatch):
        """
        依赖缺失时调用 match()（而非 _match()）不应抛出异常:
        基类 Matcher.match() 会捕获 MatcherNotAvailableError 并转换为 not_found 结果,
        这正是 MultiStrategyLocator 能安全跳过本层的关键保障。

        用 monkeypatch 强制模拟"依赖未安装"环境,使本测试在任何机器上都
        不真实初始化模型(避免 torch.hub 联网下载 SuperPoint 权重导致卡死),
        也不依赖本机是否安装了 imgloc[deep]。
        """
        import imgloc.matchers.lightglue_matcher as lg_mod

        monkeypatch.setattr(lg_mod, "_check_deps_available", lambda: False)

        matcher = LightGlueMatcher()
        assert matcher.is_available() is False
        scene = np.zeros((100, 100, 3), dtype=np.uint8)
        template = np.zeros((20, 20, 3), dtype=np.uint8)
        result = matcher.match(scene, template, threshold=0.3)

        assert result.found is False
        assert result.method_used == "lightglue"
        assert "error" in result.extra or "error_type" in result.extra
