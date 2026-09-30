"""
benchmark/scenarios.py - 基准测试用的合成场景生成器
=====================================================
为了让基准测试可复现、不依赖真实App截图素材，本模块程序化生成一系列
"大图+小图"测试对，覆盖 App 自动化测试中常见的挑战场景：
    - 不同分辨率/缩放比例（模拟 DPI 差异）
    - 不同旋转角度（模拟轻微形变/UI动画中的旋转帧）
    - 部分遮挡（模拟弹窗/其他UI元素遮挡图标)
    - 光照/主题变化（模拟深色模式 vs 浅色模式）
    - 噪声干扰（模拟不同渲染引擎的抗锯齿差异）

每个场景返回统一结构 BenchmarkCase，供 run_benchmark.py 批量跑分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np


@dataclass
class BenchmarkCase:
    """一个基准测试用例。"""

    name: str  # 场景名称，如 "scale_1.3x"
    category: str  # 场景分类，如 "scale" / "rotation" / "occlusion" / "lighting" / "noise"
    scene: np.ndarray  # 大图
    template: np.ndarray  # 小图模板
    expected_center: Optional[Tuple[int, int]]  # 预期中心点（用于计算准确率），None表示不做坐标校验


def _draw_icon(canvas: np.ndarray, x: int, y: int, w: int, h: int, color=(0, 128, 255)):
    """
    在画布指定位置绘制一个带有丰富且不对称内部结构特征的"图标"。

    注意：避免使用规则棋盘格等具有平移/旋转对称性的纹理——对称图案会让
    ORB/SIFT 产生大量"看起来都差不多"的歧义特征点，反而降低匹配质量、
    甚至导致 RANSAC 用错误的少数点拟合出荒谬的单应性矩阵。
    这里改用不同大小、不同位置的圆点+矩形组合，打破对称性，
    为特征点算法提供足够多且彼此可区分的角点/斑点特征，
    同时保留清晰边缘供形状匹配层使用。
    """
    cv2.rectangle(canvas, (x, y), (x + w, y + h), color, thickness=-1)
    cv2.rectangle(canvas, (x, y), (x + w, y + h), (20, 20, 20), thickness=3)

    blocks = [
        (0.10, 0.10, 0.28, 0.22, (30, 30, 30)),
        (0.55, 0.12, 0.25, 0.18, (10, 90, 10)),
        (0.15, 0.55, 0.20, 0.30, (90, 10, 10)),
        (0.62, 0.50, 0.30, 0.15, (10, 10, 90)),
    ]
    for bx, by, bw, bh, bcolor in blocks:
        x0, y0 = int(x + bx * w), int(y + by * h)
        x1, y1 = int(x + (bx + bw) * w), int(y + (by + bh) * h)
        cv2.rectangle(canvas, (x0, y0), (x1, y1), bcolor, thickness=-1)

    center = (x + w // 2, y + h // 2)
    radius = max(4, min(w, h) // 6)
    cv2.circle(canvas, center, radius, (255, 255, 255), thickness=-1)
    cv2.circle(canvas, center, radius, (0, 0, 0), thickness=2)

    corner_specs = [
        (x + w * 0.12, y + h * 0.12, 6),
        (x + w * 0.88, y + h * 0.12, 4),
        (x + w * 0.12, y + h * 0.88, 5),
        (x + w * 0.88, y + h * 0.88, 7),
    ]
    for (ox, oy, r) in corner_specs:
        cv2.circle(canvas, (int(ox), int(oy)), r, (255, 255, 0), thickness=-1)
        cv2.circle(canvas, (int(ox), int(oy)), r, (0, 0, 0), thickness=1)

    cv2.line(canvas, (x, y), (x + w, y + h), (0, 0, 0), thickness=1)
    return center



def _base_template(icon_w: int, icon_h: int, color=(0, 128, 255), bg_color=(240, 240, 240)) -> np.ndarray:
    """生成干净的模板小图（带少量边距，模拟真实图标素材裁剪时的留白）。"""
    margin = 3
    template = np.full((icon_h + margin * 2, icon_w + margin * 2, 3), bg_color, dtype=np.uint8)
    _draw_icon(template, margin, margin, icon_w, icon_h, color)
    return template


def generate_scale_cases() -> List[BenchmarkCase]:
    """场景1：不同缩放比例，模拟分辨率/DPI差异。"""
    cases = []
    icon_w, icon_h = 80, 80
    for scale in [0.6, 0.8, 1.0, 1.3, 1.6, 2.0]:
        canvas_size = (600, 800)
        bg_color = (240, 240, 240)
        scene = np.full((*canvas_size, 3), bg_color, dtype=np.uint8)
        sw, sh = int(icon_w * scale), int(icon_h * scale)
        x, y = 300, 200
        center = _draw_icon(scene, x, y, sw, sh)
        template = _base_template(icon_w, icon_h)
        cases.append(BenchmarkCase(
            name=f"scale_{scale}x", category="scale",
            scene=scene, template=template, expected_center=center,
        ))
    return cases


def generate_rotation_cases() -> List[BenchmarkCase]:
    """场景2：不同旋转角度，模拟轻微形变/UI动画旋转帧。"""
    cases = []
    icon_w, icon_h = 80, 80
    for angle in [0, 5, 15, 30, 45]:
        canvas_size = (600, 800)
        bg_color = (240, 240, 240)
        scene = np.full((*canvas_size, 3), bg_color, dtype=np.uint8)
        x, y = 300, 200
        center = _draw_icon(scene, x, y, icon_w, icon_h)
        if angle != 0:
            rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)
            scene = cv2.warpAffine(scene, rot_mat, (canvas_size[1], canvas_size[0]), borderValue=bg_color)
        template = _base_template(icon_w, icon_h)
        # 旋转后中心点位置不变（绕自身中心旋转），仍可用于坐标校验
        cases.append(BenchmarkCase(
            name=f"rotation_{angle}deg", category="rotation",
            scene=scene, template=template, expected_center=center,
        ))
    return cases


def generate_occlusion_cases() -> List[BenchmarkCase]:
    """场景3：部分遮挡，模拟弹窗/其他UI元素遮挡目标图标。"""
    cases = []
    icon_w, icon_h = 80, 80
    for occlusion_ratio in [0.0, 0.15, 0.3, 0.5]:
        canvas_size = (600, 800)
        bg_color = (240, 240, 240)
        scene = np.full((*canvas_size, 3), bg_color, dtype=np.uint8)
        x, y = 300, 200
        center = _draw_icon(scene, x, y, icon_w, icon_h)
        if occlusion_ratio > 0:
            occ_w = int(icon_w * occlusion_ratio)
            cv2.rectangle(scene, (x + icon_w - occ_w, y), (x + icon_w, y + icon_h), (100, 100, 100), thickness=-1)
        template = _base_template(icon_w, icon_h)
        cases.append(BenchmarkCase(
            name=f"occlusion_{int(occlusion_ratio*100)}pct", category="occlusion",
            scene=scene, template=template, expected_center=center,
        ))
    return cases


def generate_lighting_cases() -> List[BenchmarkCase]:
    """场景4：光照/主题变化，模拟深色模式 vs 浅色模式。"""
    cases = []
    icon_w, icon_h = 80, 80

    # 浅色模式（基准）
    light_bg = (240, 240, 240)
    scene_light = np.full((600, 800, 3), light_bg, dtype=np.uint8)
    center = _draw_icon(scene_light, 300, 200, icon_w, icon_h)
    template = _base_template(icon_w, icon_h, bg_color=light_bg)
    cases.append(BenchmarkCase(
        name="lighting_light_mode", category="lighting",
        scene=scene_light, template=template, expected_center=center,
    ))

    # 深色模式：背景反转为深色，图标颜色也做相应调整（模拟真实App深色模式主题）
    dark_bg = (30, 30, 30)
    scene_dark = np.full((600, 800, 3), dark_bg, dtype=np.uint8)
    center_dark = _draw_icon(scene_dark, 300, 200, icon_w, icon_h)
    cases.append(BenchmarkCase(
        name="lighting_dark_mode", category="lighting",
        scene=scene_dark, template=template, expected_center=center_dark,
    ))

    # 亮度整体提升/降低（模拟环境光照变化）
    for beta in [-60, 60]:
        scene_adj = cv2.convertScaleAbs(scene_light, alpha=1.0, beta=beta)
        cases.append(BenchmarkCase(
            name=f"lighting_beta_{beta}", category="lighting",
            scene=scene_adj, template=template, expected_center=center,
        ))
    return cases


def generate_noise_cases() -> List[BenchmarkCase]:
    """场景5：噪声干扰，模拟不同渲染引擎的抗锯齿/压缩差异。"""
    cases = []
    icon_w, icon_h = 80, 80
    for noise_level in [0, 10, 25, 40]:
        canvas_size = (600, 800)
        bg_color = (240, 240, 240)
        scene = np.full((*canvas_size, 3), bg_color, dtype=np.uint8)
        x, y = 300, 200
        center = _draw_icon(scene, x, y, icon_w, icon_h)
        if noise_level > 0:
            noise = np.random.randint(0, noise_level, scene.shape, dtype=np.uint8)
            scene = cv2.add(scene, noise)
        template = _base_template(icon_w, icon_h)
        cases.append(BenchmarkCase(
            name=f"noise_level_{noise_level}", category="noise",
            scene=scene, template=template, expected_center=center,
        ))
    return cases


def generate_all_cases() -> List[BenchmarkCase]:
    """汇总全部基准测试场景。"""
    cases: List[BenchmarkCase] = []
    cases += generate_scale_cases()
    cases += generate_rotation_cases()
    cases += generate_occlusion_cases()
    cases += generate_lighting_cases()
    cases += generate_noise_cases()
    return cases
