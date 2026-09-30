"""
tests/test_locate.py — locate 包纯逻辑单测
覆盖:意图解析、坐标转换、OCR 模糊匹配、融合打分(不依赖网络/adb)。
"""

import os
import sys

import numpy as np
import pytest

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, os.path.abspath(_SRC))

from locate.intent import parse_intent  # noqa: E402
from locate.coords import (  # noqa: E402
    norm1000_bbox_to_pixel, normalize_bbox, bbox_center, clamp_bbox,
)
from locate.ocr import OcrLocator, _normalize_text  # noqa: E402
from locate.config import OCRConfig, FusionConfig  # noqa: E402
from locate.fusion import iou, CandidateFusion  # noqa: E402
from locate.types import Candidate  # noqa: E402


# ----------------------------------------------------------------------
# 意图解析
# ----------------------------------------------------------------------
class TestIntent:
    @pytest.mark.parametrize("query,expected", [
        ("点击kof图标", "kof"),
        ("点击 KOF:Legend 图标", "KOF:Legend"),
        ("点击「微信」图标", "微信"),
        ("点一下登录按钮", "登录"),
        ("点击右上角的设置", "设置"),
        ("找Play商店", "Play商店"),
        ('点击"相机"图标', "相机"),
    ])
    def test_target_text_extraction(self, query, expected):
        intent = parse_intent(query)
        assert intent.target_text == expected

    def test_icon_type_detected(self):
        assert parse_intent("点击kof图标").target_type == "icon_with_label"
        assert parse_intent("点击登录按钮").target_type == "text"

    def test_empty_target_no_crash(self):
        intent = parse_intent("点一下那个")
        # 没有具体目标时不报错
        assert intent.target_text == ""


# ----------------------------------------------------------------------
# 坐标转换
# ----------------------------------------------------------------------
class TestCoords:
    def test_norm1000_bbox(self):
        # 1000x1000 归一化,图 1280x720
        bbox = norm1000_bbox_to_pixel([500, 250, 600, 350], 1280, 720)
        assert bbox == (640, 180, 768, 252)

    def test_normalize_roundtrip(self):
        bbox = (100, 200, 300, 400)
        n = normalize_bbox(bbox, 1000, 800)
        assert n == pytest.approx((0.1, 0.25, 0.3, 0.5))

    def test_bbox_center(self):
        assert bbox_center((10, 20, 30, 60)) == (20, 40)

    def test_clamp_bbox(self):
        assert clamp_bbox((-10, -5, 2000, 900), 1280, 720) == (0, 0, 1280, 720)


# ----------------------------------------------------------------------
# OCR 模糊匹配
# ----------------------------------------------------------------------
class TestOcrMatch:
    def test_normalize_text(self):
        assert _normalize_text("  KOF : Legend! ") == "koflegend"
        assert _normalize_text("Appium Set...") == "appiumset"

    def test_exact_match(self):
        loc = OcrLocator(OCRConfig())
        score, exact, prefix = loc._match_score("koflegend", "koflegend")
        assert score == 1.0 and exact is True

    def test_prefix_match_truncated(self):
        # 文字被截断:appiumset 是 appiumsettings 的前缀
        loc = OcrLocator(OCRConfig())
        score, exact, prefix = loc._match_score("appiumsettings", "appiumset")
        assert prefix is True and score >= 0.9

    def test_fuzzy_match_threshold(self):
        loc = OcrLocator(OCRConfig(fuzzy_threshold=0.8))
        score, exact, prefix = loc._match_score("legend", "legcnd")  # 1 字符之差
        assert score >= 0.8

    def test_no_match(self):
        loc = OcrLocator(OCRConfig(fuzzy_threshold=0.8))
        score, _, _ = loc._match_score("kof", "settings")
        assert score < 0.8


# ----------------------------------------------------------------------
# 融合
# ----------------------------------------------------------------------
class TestFusion:
    def test_iou_identical(self):
        b = (0, 0, 100, 100)
        assert iou(b, b) == pytest.approx(1.0)

    def test_iou_disjoint(self):
        assert iou((0, 0, 10, 10), (100, 100, 110, 110)) == 0.0

    def test_iou_half_overlap(self):
        # 两个 100x100 框重叠 50x100
        v = iou((0, 0, 100, 100), (50, 0, 150, 100))
        assert v == pytest.approx(1 / 3, abs=0.01)

    def test_multi_source_scores_higher(self):
        """OCR+VLM 互相印证的簇,分数应高于单一 OCR 簇。"""
        cfg = FusionConfig()
        fuser = CandidateFusion(cfg)
        # 同一位置两个来源
        multi = [
            Candidate((100, 100, 200, 200), (150, 150), 1.0, "ocr", "KOF"),
            Candidate((102, 102, 198, 198), (150, 150), 0.9, "vlm", "KOF icon"),
        ]
        single = [
            Candidate((300, 300, 400, 400), (350, 350), 1.0, "ocr", "Other"),
        ]
        fused_multi = fuser.fuse(multi)
        fused_single = fuser.fuse(single)
        assert fused_multi[0].score > fused_single[0].score

    def test_top_k_limit(self):
        cfg = FusionConfig()
        fuser = CandidateFusion(cfg)
        cands = [
            Candidate((i * 300, i * 300, i * 300 + 10, i * 300 + 10),
                      (i * 300 + 5, i * 300 + 5), 1.0, "ocr", f"t{i}")
            for i in range(5)
        ]
        assert len(fuser.fuse(cands)) <= 3

    def test_template_source_wins(self):
        """模板命中 + OCR 印证的簇应高于纯 VLM 簇。"""
        cfg = FusionConfig()
        fuser = CandidateFusion(cfg)
        with_template = fuser.fuse([
            Candidate((0, 0, 50, 50), (25, 25), 1.0, "template", "KOF"),
            Candidate((0, 0, 50, 50), (25, 25), 1.0, "ocr", "KOF:Legend"),
        ])
        vlm_only = fuser.fuse([
            Candidate((500, 500, 560, 560), (530, 530), 0.9, "vlm", "x"),
        ])
        assert with_template[0].score > vlm_only[0].score
