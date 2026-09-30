"""
src/locate/coords.py — 坐标转换工具
=====================================
统一处理归一化坐标 ↔ 像素坐标、不同 bbox 格式转换。
"""

from __future__ import annotations

from typing import Tuple

BBox = Tuple[int, int, int, int]


def norm1000_to_pixel(x: float, y: float, width: int, height: int) -> Tuple[int, int]:
    """0~1000 归一化坐标 → 像素坐标。"""
    return (int(round(x / 1000 * width)), int(round(y / 1000 * height)))


def norm1000_bbox_to_pixel(bbox_norm, width: int, height: int) -> BBox:
    """
    0~1000 归一化 bbox → 像素 bbox。
    输入格式: [x1, y1, x2, y2] (norm1000_xyxy)
    """
    x1, y1, x2, y2 = bbox_norm
    return (
        int(round(x1 / 1000 * width)),
        int(round(y1 / 1000 * height)),
        int(round(x2 / 1000 * width)),
        int(round(y2 / 1000 * height)),
    )


def gemini_bbox_to_pixel(bbox_norm, width: int, height: int) -> BBox:
    """
    Gemini 格式 bbox [ymin, xmin, ymax, xmax] (0~1 归一化) → 像素 bbox。
    """
    ymin, xmin, ymax, xmax = bbox_norm
    return (
        int(round(xmin * width)),
        int(round(ymin * height)),
        int(round(xmax * width)),
        int(round(ymax * height)),
    )


def normalize_bbox(bbox: BBox, width: int, height: int) -> Tuple[float, float, float, float]:
    """像素 bbox → 0~1 归一化。"""
    x1, y1, x2, y2 = bbox
    return (x1 / width, y1 / height, x2 / width, y2 / height)


def bbox_center(bbox: BBox) -> Tuple[int, int]:
    """bbox 中心点。"""
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def scale_bbox(bbox: BBox, scale: float) -> BBox:
    """按比例缩放 bbox（用于图片缩放后还原坐标）。"""
    x1, y1, x2, y2 = bbox
    return (
        int(round(x1 / scale)),
        int(round(y1 / scale)),
        int(round(x2 / scale)),
        int(round(y2 / scale)),
    )


def clamp_bbox(bbox: BBox, width: int, height: int) -> BBox:
    """将 bbox 限制在图片范围内。"""
    x1, y1, x2, y2 = bbox
    return (
        max(0, min(x1, width - 1)),
        max(0, min(y1, height - 1)),
        max(0, min(x2, width)),
        max(0, min(y2, height)),
    )
