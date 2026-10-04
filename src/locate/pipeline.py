"""
src/locate/pipeline.py — 定位主入口(Step E)
==============================================
串联意图解析 → OCR + VLM + 模板(并行) → 融合 → 验证 → 输出。

用法:
    from locate import pipeline
    result = pipeline.locate(image, "点击kof图标")
    if result.ok:
        print(result.point, result.confidence, result.grid_cell)
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

import cv2
import numpy as np

import cvio
from .config import LocateConfig, load_config
from .coords import bbox_center
from .types import Candidate, Intent, LocateResult
from .viz import draw_candidates, draw_result, save_debug_image

logger = logging.getLogger("imgloc.locate.pipeline")

# 项目根目录
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def locate(
    image: np.ndarray,
    query: str,
    config: Optional[LocateConfig] = None,
    image_shape: Optional[tuple] = None,
) -> LocateResult:
    """
    AI 辅助定位主入口。

    Args:
        image: 设备截图(BGR numpy 数组)。
        query: 用户自然语言描述(如 "点击kof图标")。
        config: 定位配置;None 用默认。
        image_shape: 截图形状(用于格子编号换算);None 自动取 image.shape。

    Returns:
        LocateResult: ok/point/bbox/confidence/sources/grid_cell/debug_images。
    """
    if config is None:
        config = load_config()

    if image_shape is None:
        image_shape = image.shape

    h, w = image.shape[:2]
    debug_dir = os.path.join(_PROJECT_ROOT, config.debug.dir) \
        if not os.path.isabs(config.debug.dir) else config.debug.dir
    debug_images: dict = {}

    logger.info("定位开始: query=%r, 图片 %dx%d, 模式=%s", query, w, h, config.mode)

    # ---- Step A: 意图解析 ----
    t0 = time.monotonic()
    intent = _parse_intent(query, config)
    logger.info("意图解析: %s (%.2fs)", intent, time.monotonic() - t0)

    # ---- Step B: 候选生成(并行/条件分派) ----
    candidates: List[Candidate] = []

    # B1: OCR
    if config.mode in ("full", "ocr_only"):
        t1 = time.monotonic()
        ocr_cands = _run_ocr(image, intent, config)
        logger.info("OCR: %d 个候选 (%.2fs)", len(ocr_cands), time.monotonic() - t1)
        candidates.extend(ocr_cands)

    # B2: VLM grounding
    if config.mode in ("full", "vlm_only"):
        t2 = time.monotonic()
        vlm_cands = _run_vlm(image, intent, config)
        logger.info("VLM: %d 个候选 (%.2fs)", len(vlm_cands), time.monotonic() - t2)
        candidates.extend(vlm_cands)

    # B3: 模板匹配
    t3 = time.monotonic()
    tpl_cands = _run_template(image, intent, config)
    logger.info("模板: %d 个候选 (%.2fs)", len(tpl_cands), time.monotonic() - t3)
    candidates.extend(tpl_cands)

    if not candidates:
        return LocateResult(
            ok=False, reason="所有策略均未找到候选", query=query,
            debug_images=debug_images,
        )

    # 调试图: 所有候选
    if config.debug.save_images:
        img_all = draw_candidates(image, candidates)
        p = save_debug_image(img_all, os.path.join(debug_dir, "all_candidates.png"))
        debug_images["all_candidates"] = p

    # ---- Step C: 融合 ----
    t4 = time.monotonic()
    fused = _run_fusion(candidates, config)
    logger.info("融合: %d 个候选 → Top-%d (%.2fs)",
                len(candidates), len(fused), time.monotonic() - t4)

    if not fused:
        return LocateResult(
            ok=False, reason="融合后无候选", query=query,
            debug_images=debug_images, candidates=candidates,
        )

    # ---- Step D: 验证 ----
    verified_fused = None
    if config.verify.enabled:
        t5 = time.monotonic()
        for cand in fused[:config.verify.top_k]:
            ok, reason = _run_verify(image, cand, intent, config)
            cand.verified = ok
            cand.verify_reason = reason
            # 同步验证结论到与之重叠的原始候选,便于调试展示
            for orig in candidates:
                if _overlap(cand.bbox, orig.bbox):
                    orig.verified = ok
                    orig.verify_reason = reason
            if ok:
                verified_fused = cand
                break
        logger.info("验证: %.2fs", time.monotonic() - t5)

    # 选最佳: 验证通过的优先,否则取融合最高分
    best = verified_fused
    # 候选排序:验证通过的在前,其余按融合分顺序(fused 已排序)
    ranked = sorted(fused, key=lambda c: (not c.verified, -c.score))
    if best is None and not config.verify.enabled:
        # 关闭验证时(调试模式)直接用融合最高分
        best = ranked[0]

    # ---- Step E: 输出 ----
    if best is None:
        # 全部未通过验证:不强行输出,但保留候选供用户人工裁决
        return LocateResult(
            ok=False, reason="候选均未通过验证", query=query,
            debug_images=debug_images, candidates=candidates,
            top_candidates=ranked,
        )

    grid_cell = _point_to_cell(best.point, image_shape, cell_size=30)

    # 调试图: 最终结果
    if config.debug.save_images:
        img_result = draw_result(image, best.bbox, best.point,
                                 best.confidence, best.text)
        p = save_debug_image(img_result, os.path.join(debug_dir, "result.png"))
        debug_images["result"] = p

    result = LocateResult(
        ok=True,
        point=best.point,
        bbox=best.bbox,
        confidence=best.confidence,
        sources=list(set(c.source for c in candidates)),
        grid_cell=grid_cell,
        debug_images=debug_images,
        query=query,
        candidates=candidates,
        top_candidates=ranked,
        intent=intent,
    )
    logger.info("定位完成: point=%s, cell=%s, conf=%.2f, sources=%s",
                result.point, result.grid_cell, result.confidence, result.sources)
    return result


def _parse_intent(query: str, config: LocateConfig) -> Intent:
    """Step A: 意图解析。"""
    from .intent import parse_intent

    ai_client = None
    # 仅在需要 LLM 时创建 client
    if config.vlm.api_key:
        try:
            from aiclient import AIClient
            ai_client = AIClient()
        except Exception:
            pass

    return parse_intent(query, ai_client=ai_client)


def _run_ocr(image: np.ndarray, intent: Intent, config: LocateConfig) -> List[Candidate]:
    """Step B1: OCR 定位。"""
    try:
        from .ocr import OcrLocator
        locator = OcrLocator(config.ocr)
        return locator.locate(image, intent)
    except Exception as exc:
        logger.warning("OCR 定位失败: %s", exc)
        return []


def _run_vlm(image: np.ndarray, intent: Intent, config: LocateConfig) -> List[Candidate]:
    """Step B2: VLM grounding。"""
    try:
        from .vlm import VlmLocator
        locator = VlmLocator(config.vlm)
        return locator.locate(image, intent)
    except Exception as exc:
        logger.warning("VLM 定位失败: %s", exc)
        return []


def _run_template(image: np.ndarray, intent: Intent, config: LocateConfig) -> List[Candidate]:
    """Step B3: 模板匹配。"""
    try:
        from .template import TemplateLocator
        locator = TemplateLocator()
        return locator.locate(image, intent)
    except Exception as exc:
        logger.warning("模板匹配失败: %s", exc)
        return []


def _run_fusion(candidates: List[Candidate], config: LocateConfig) -> List[Candidate]:
    """Step C: 候选融合。"""
    from .fusion import CandidateFusion
    fuser = CandidateFusion(config.fusion)
    return fuser.fuse(candidates)


def _run_verify(image: np.ndarray, candidate: Candidate,
                intent: Intent, config: LocateConfig) -> tuple:
    """Step D: 裁剪验证。"""
    from .vlm import VlmLocator
    locator = VlmLocator(config.vlm)
    return locator.verify(image, candidate, intent)


def _overlap(a, b) -> bool:
    """两个像素 bbox 是否重叠(任意交集即视为同一目标)。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return max(ax1, bx1) < min(ax2, bx2) and max(ay1, by1) < min(ay2, by2)


def _point_to_cell(point: tuple, image_shape: tuple, cell_size: int = 30) -> str:
    """
    像素坐标 → 网格格子编号(兼容旧流程和 GUI 显示)。
    """
    try:
        import sys
        src_dir = os.path.join(os.path.dirname(__file__), "..")
        if src_dir not in sys.path:
            sys.path.insert(0, os.path.abspath(src_dir))
        from grid import GridMarker, col_name

        marker = GridMarker(cell_size=cell_size)
        cols, rows = marker.grid_dims(image_shape)
        x, y = point
        col = max(0, min(int(x // cell_size), cols - 1))
        row = max(0, min(int(y // cell_size), rows - 1))
        return f"{col_name(col)}{row + 1}"
    except Exception:
        return ""
