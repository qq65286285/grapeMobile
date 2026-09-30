"""
测试：MultiStrategyLocator 责任链路由器 与 Locator 对外API
覆盖：
    - 按顺序尝试各算法层，命中即返回
    - 某层置信度不足时自动降级到下一层
    - 某层依赖缺失/异常时自动跳过（不中断整条链路）
    - 单一策略模式（strategy="template"等）只运行指定层
    - 全部算法层不可用时抛出 NoMatcherAvailableError
"""

from unittest.mock import patch

import numpy as np
import pytest

from imgloc import Locator, MultiStrategyLocator, MatchResult
from imgloc.exceptions import ConfigError, NoMatcherAvailableError
from imgloc.base import Matcher


class _FakeMatcher(Matcher):
    """测试专用的可编程假匹配器，用于精确控制责任链每一层的行为。"""

    def __init__(self, name: str, found: bool, confidence: float, available: bool = True, raise_exc=None, config=None):
        super().__init__(config)
        self.name = name
        self._found = found
        self._confidence = confidence
        self._available = available
        self._raise_exc = raise_exc
        self.call_count = 0

    def is_available(self) -> bool:
        return self._available

    def _match(self, image_source, image_target, threshold, **kwargs):
        self.call_count += 1
        if self._raise_exc:
            raise self._raise_exc
        return MatchResult(
            found=self._found and self._confidence >= threshold,
            center_point=(1, 1) if self._found else None,
            confidence=self._confidence,
            method_used=self.name,
        )


def _make_scene_and_template():
    scene = np.zeros((100, 100, 3), dtype=np.uint8)
    template = np.zeros((20, 20, 3), dtype=np.uint8)
    return scene, template


class TestMultiStrategyLocatorChainOrder:
    def test_first_layer_hit_stops_chain(self):
        """第一层命中时，后续层不应被调用（责任链短路）。"""
        router = MultiStrategyLocator()
        fake_template = _FakeMatcher("template", found=True, confidence=0.9)
        fake_shape = _FakeMatcher("shape", found=True, confidence=0.9)
        router.matchers = [fake_template, fake_shape]

        scene, template = _make_scene_and_template()
        result = router.find(scene, template, threshold=0.8)

        assert result.found is True
        assert result.method_used == "template"
        assert fake_template.call_count == 1
        assert fake_shape.call_count == 0  # 未被调用，验证短路生效

    def test_degrades_to_next_layer_when_confidence_too_low(self):
        """第一层置信度不足阈值时，应自动降级到下一层。"""
        router = MultiStrategyLocator()
        fake_template = _FakeMatcher("template", found=True, confidence=0.5)  # 低于阈值
        fake_orb = _FakeMatcher("orb", found=True, confidence=0.9)
        router.matchers = [fake_template, fake_orb]

        scene, template = _make_scene_and_template()
        result = router.find(scene, template, threshold=0.8)

        assert result.found is True
        assert result.method_used == "orb"
        assert fake_template.call_count == 1
        assert fake_orb.call_count == 1

    def test_skips_unavailable_layer(self):
        """依赖缺失(is_available=False)的层应被静默跳过，不调用其 _match。"""
        router = MultiStrategyLocator()
        fake_unavailable = _FakeMatcher("lightglue", found=True, confidence=0.99, available=False)
        fake_fallback = _FakeMatcher("sift", found=True, confidence=0.9)
        router.matchers = [fake_unavailable, fake_fallback]

        scene, template = _make_scene_and_template()
        result = router.find(scene, template, threshold=0.8)

        assert result.found is True
        assert result.method_used == "sift"
        assert fake_unavailable.call_count == 0

    def test_skips_layer_that_raises_exception(self):
        """某层执行时抛出异常，应被捕获并自动跳到下一层，不中断整体流程。"""
        router = MultiStrategyLocator()
        fake_broken = _FakeMatcher("shape", found=False, confidence=0.0, raise_exc=RuntimeError("boom"))
        fake_fallback = _FakeMatcher("orb", found=True, confidence=0.9)
        router.matchers = [fake_broken, fake_fallback]

        scene, template = _make_scene_and_template()
        result = router.find(scene, template, threshold=0.8)

        assert result.found is True
        assert result.method_used == "orb"

    def test_all_layers_fail_returns_last_not_found_result(self):
        """全部层都未命中时，返回最后一层的 not_found 结果而非抛异常。"""
        router = MultiStrategyLocator()
        fake_a = _FakeMatcher("template", found=False, confidence=0.1)
        fake_b = _FakeMatcher("orb", found=False, confidence=0.2)
        router.matchers = [fake_a, fake_b]

        scene, template = _make_scene_and_template()
        result = router.find(scene, template, threshold=0.8)

        assert result.found is False
        assert result.method_used == "orb"  # 最后尝试的一层

    def test_all_layers_unavailable_raises(self):
        """所有层都不可用时应抛出 NoMatcherAvailableError。"""
        router = MultiStrategyLocator()
        router.matchers = [
            _FakeMatcher("template", found=False, confidence=0.0, available=False),
            _FakeMatcher("orb", found=False, confidence=0.0, available=False),
        ]
        scene, template = _make_scene_and_template()
        with pytest.raises(NoMatcherAvailableError):
            router.find(scene, template, threshold=0.8)


class TestSingleStrategyMode:
    def test_specific_strategy_runs_only_that_layer(self):
        router = MultiStrategyLocator()
        fake_template = _FakeMatcher("template", found=True, confidence=0.9)
        fake_orb = _FakeMatcher("orb", found=True, confidence=0.9)
        router.matchers = [fake_template, fake_orb]

        scene, template = _make_scene_and_template()
        result = router.find(scene, template, threshold=0.5, strategy="orb")

        assert result.method_used == "orb"
        assert fake_template.call_count == 0
        assert fake_orb.call_count == 1

    def test_unknown_strategy_name_raises_config_error(self):
        router = MultiStrategyLocator()
        scene, template = _make_scene_and_template()
        with pytest.raises(ConfigError):
            router.find(scene, template, strategy="not_a_real_strategy")


class TestLocatorPublicAPI:
    def test_default_auto_strategy_builds_full_chain(self):
        locator = Locator(strategy="auto")
        names = [m.name for m in locator._router.matchers]
        assert names == ["template", "shape", "orb", "sift", "lightglue"]

    def test_find_on_real_synthetic_scene(self, synthetic_scene_basic):
        """端到端测试：用真实合成场景验证 Locator 对外API可正常工作。"""
        scene, template, expected_center = synthetic_scene_basic
        locator = Locator(strategy="auto", config={"template": {"threshold": 0.5}})
        result = locator.find(scene, template, threshold=0.5)
        assert isinstance(result, MatchResult)
        # 模板匹配层应该在这个简单场景下就能命中
        if result.found:
            assert abs(result.center_point[0] - expected_center[0]) <= 10

    def test_custom_router_order_via_config(self):
        locator = Locator(strategy="auto", config={"router": {"order": ["template", "orb"]}})
        names = [m.name for m in locator._router.matchers]
        assert names == ["template", "orb"]
