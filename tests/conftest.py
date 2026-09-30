"""
pytest 公共 fixtures
======================
提供测试用的合成图像生成函数：由于不依赖真实App截图，
测试使用程序化生成的几何图形（矩形/圆形/十字线/棋盘格纹理）模拟"大图"和"小图"，
可以精确控制缩放/旋转/位置等参数，从而验证各算法的正确性。

注：叠加 4x4 棋盘格纹理是为了给 ORB/SIFT 等特征点算法提供足够多的、
分布均匀的角点特征（简单的纯色矩形+圆形往往关键点数<10，
会导致特征点匹配层因"关键点不足"而始终无法参与测试）。
"""

from __future__ import annotations

import numpy as np
import cv2
import pytest


def _draw_icon(canvas: np.ndarray, x: int, y: int, w: int, h: int, color=(0, 128, 255)):
    """
    在画布指定位置绘制一个带有丰富且不对称内部结构特征的"图标"。

    注意：避免使用规则棋盘格等具有平移/旋转对称性的纹理——对称图案会让
    ORB/SIFT 产生大量"看起来都差不多"的歧义特征点，反而降低匹配质量。
    这里改用不同大小、不同位置的圆点+矩形组合，打破对称性，
    为特征点算法提供足够多且彼此可区分的角点/斑点特征。
    """
    cv2.rectangle(canvas, (x, y), (x + w, y + h), color, thickness=-1)
    cv2.rectangle(canvas, (x, y), (x + w, y + h), (20, 20, 20), thickness=3)

    # 不对称的内部小方块图案（每块大小/位置各不相同，避免重复纹理造成的歧义匹配）
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

    # 四个大小不同的角点标记，进一步打破对称性，提供独特可区分的特征点
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



def make_synthetic_scene(
    canvas_size=(600, 800),
    icon_size=(80, 80),
    icon_pos=(300, 200),
    icon_color=(0, 128, 255),
    bg_color=(240, 240, 240),
    scale=1.0,
    angle=0.0,
    noise=False,
):
    """
    生成一张"大图"，其中在指定位置绘制一个带有可识别形状的图标区域，
    并返回该图标区域对应的"小图"模板（用于后续在大图中定位）。

    Returns:
        (scene, template, expected_center): 大图, 模板小图, 预期中心点坐标
    """
    h, w = canvas_size
    scene = np.full((h, w, 3), bg_color, dtype=np.uint8)

    icon_w, icon_h = int(icon_size[0] * scale), int(icon_size[1] * scale)
    x, y = icon_pos
    center = _draw_icon(scene, x, y, icon_w, icon_h, icon_color)

    if angle != 0.0:
        rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)
        scene = cv2.warpAffine(scene, rot_mat, (w, h), borderValue=bg_color)

    if noise:
        noise_arr = np.random.randint(0, 15, scene.shape, dtype=np.uint8)
        scene = cv2.add(scene, noise_arr)

    # 模板直接从"未旋转"的原始图标区域裁剪（模拟采集到的干净图标素材）
    template_canvas = np.full((icon_h + 6, icon_w + 6, 3), bg_color, dtype=np.uint8)
    tx, ty = 3, 3
    _draw_icon(template_canvas, tx, ty, icon_w, icon_h, icon_color)

    expected_center = center if angle == 0.0 else None  # 旋转后期望坐标不再简单已知
    return scene, template_canvas, expected_center


@pytest.fixture
def synthetic_scene_basic():
    """基础场景：无缩放无旋转，用于验证基础匹配能力。"""
    return make_synthetic_scene()


@pytest.fixture
def synthetic_scene_scaled():
    """缩放场景：图标被放大1.3倍绘制到大图中，模拟分辨率差异。"""
    return make_synthetic_scene(scale=1.3)


@pytest.fixture
def synthetic_scene_rotated():
    """旋转场景：整个大图旋转15度，模拟轻微旋转/形变场景。"""
    return make_synthetic_scene(angle=15.0)
