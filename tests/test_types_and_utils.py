"""
测试：MatchResult 数据类与 utils 工具函数
"""

import numpy as np
import cv2
import pytest

from imgloc.types import MatchResult
from imgloc.utils import load_image, to_gray, ensure_bgr, clip_rect_to_bounds
from imgloc.exceptions import ImageLoadError


class TestMatchResult:
    def test_not_found_factory(self):
        result = MatchResult.not_found(method_used="template", elapsed_ms=1.5)
        assert result.found is False
        assert result.method_used == "template"
        assert result.center_point is None

    def test_to_dict_rounds_values(self):
        result = MatchResult(
            found=True, rect=(1, 2, 3, 4), center_point=(2, 4),
            confidence=0.87654, angle=12.345, scale=1.0001,
            method_used="orb", elapsed_ms=3.14159,
        )
        d = result.to_dict()
        assert d["confidence"] == 0.8765
        assert d["method_used"] == "orb"

    def test_repr_found_and_not_found(self):
        found = MatchResult(found=True, center_point=(1, 1), confidence=0.9, method_used="sift")
        not_found = MatchResult.not_found(method_used="shape")
        assert "found=True" in repr(found)
        assert "found=False" in repr(not_found)


class TestLoadImage:
    def test_load_from_numpy(self):
        arr = np.zeros((10, 10, 3), dtype=np.uint8)
        loaded = load_image(arr)
        assert loaded is arr

    def test_load_from_empty_numpy_raises(self):
        with pytest.raises(ImageLoadError):
            load_image(np.array([]))

    def test_load_from_bytes(self):
        arr = np.zeros((20, 20, 3), dtype=np.uint8)
        arr[:, :] = (10, 20, 30)
        ok, encoded = cv2.imencode(".png", arr)
        assert ok
        loaded = load_image(encoded.tobytes())
        assert loaded.shape[:2] == (20, 20)

    def test_load_from_bad_bytes_raises(self):
        with pytest.raises(ImageLoadError):
            load_image(b"not an image")

    def test_load_from_nonexistent_path_raises(self):
        with pytest.raises(ImageLoadError):
            load_image("this/path/does/not/exist.png")

    def test_load_unsupported_type_raises(self):
        with pytest.raises(ImageLoadError):
            load_image(12345)  # type: ignore[arg-type]


class TestColorConversion:
    def test_to_gray_from_bgr(self):
        arr = np.zeros((5, 5, 3), dtype=np.uint8)
        gray = to_gray(arr)
        assert gray.ndim == 2

    def test_to_gray_already_gray(self):
        arr = np.zeros((5, 5), dtype=np.uint8)
        assert to_gray(arr) is arr

    def test_ensure_bgr_from_gray(self):
        arr = np.zeros((5, 5), dtype=np.uint8)
        bgr = ensure_bgr(arr)
        assert bgr.shape == (5, 5, 3)

    def test_ensure_bgr_from_bgra(self):
        arr = np.zeros((5, 5, 4), dtype=np.uint8)
        bgr = ensure_bgr(arr)
        assert bgr.shape == (5, 5, 3)


class TestClipRect:
    def test_within_bounds_unchanged(self):
        assert clip_rect_to_bounds(10, 10, 20, 20, 100, 100) == (10, 10, 20, 20)

    def test_clips_when_exceeding_bounds(self):
        x, y, w, h = clip_rect_to_bounds(90, 90, 50, 50, 100, 100)
        assert x == 90 and y == 90
        assert x + w <= 100 and y + h <= 100

    def test_negative_origin_clipped(self):
        x, y, w, h = clip_rect_to_bounds(-5, -5, 20, 20, 100, 100)
        assert x == 0 and y == 0
