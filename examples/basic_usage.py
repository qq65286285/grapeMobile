"""
examples/basic_usage.py - imgloc 基本用法示例
================================================
演示如何用 Locator 在大图中定位小图，并展示单一策略模式与自定义配置的用法。

运行方式：
    python examples/basic_usage.py
（脚本内使用程序化生成的合成图像，不依赖外部图片文件，可直接运行体验效果）
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cv2
import numpy as np

from imgloc import Locator


def make_demo_images():
    """生成一张演示用的"截图"和对应的"图标模板"（带不对称纹理，保证ORB/SIFT也能正常演示）。"""
    scene = np.full((600, 800, 3), (240, 240, 240), dtype=np.uint8)
    x, y, w, h = 350, 220, 90, 90

    def draw_icon(canvas, x, y, w, h):
        cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 128, 255), thickness=-1)
        cv2.rectangle(canvas, (x, y), (x + w, y + h), (20, 20, 20), thickness=3)
        # 不对称内部色块，为ORB/SIFT提供足够可区分的特征点
        cv2.rectangle(canvas, (x + 10, y + 10), (x + 35, y + 30), (30, 30, 30), thickness=-1)
        cv2.rectangle(canvas, (x + 55, y + 15), (x + 80, y + 35), (10, 90, 10), thickness=-1)
        cv2.rectangle(canvas, (x + 15, y + 55), (x + 35, y + 80), (90, 10, 10), thickness=-1)
        cv2.circle(canvas, (x + w // 2, y + h // 2), 18, (255, 255, 255), thickness=-1)
        cv2.circle(canvas, (x + w // 2, y + h // 2), 18, (0, 0, 0), thickness=2)

    draw_icon(scene, x, y, w, h)

    template = np.full((110, 110, 3), (240, 240, 240), dtype=np.uint8)
    draw_icon(template, 10, 10, w, h)
    return scene, template



def example_auto_strategy():
    """示例1：自动多算法路由（推荐默认用法）"""
    print("\n=== 示例1: 自动多算法路由 (strategy='auto') ===")
    scene, template = make_demo_images()
    locator = Locator(strategy="auto")
    result = locator.find(scene, template, threshold=0.7)
    print(result)
    if result.found:
        print(f"  -> 点击坐标: {result.center_point}, 命中算法: {result.method_used}")


def example_single_strategy():
    """示例2：只使用单一算法（不走责任链降级）"""
    print("\n=== 示例2: 指定单一策略 (strategy='orb') ===")
    scene, template = make_demo_images()
    locator = Locator(strategy="orb")
    result = locator.find(scene, template, threshold=0.2)
    print(result)


def example_custom_config():
    """示例3：自定义配置（调整责任链顺序和参数）"""
    print("\n=== 示例3: 自定义配置 ===")
    scene, template = make_demo_images()
    locator = Locator(strategy="auto", config={
        "router": {"order": ["template", "orb"]},  # 跳过 shape/sift/lightglue
        "template": {"threshold": 0.9},
    })
    result = locator.find(scene, template)
    print(result)
    print(f"  -> 完整结果字典: {result.to_dict()}")


def example_not_found():
    """示例4：目标不存在时的返回结果"""
    print("\n=== 示例4: 目标不存在场景 ===")
    scene, _ = make_demo_images()
    random_template = np.random.randint(0, 255, (50, 50, 3), dtype=np.uint8)
    locator = Locator(strategy="auto")
    result = locator.find(scene, random_template, threshold=0.9)
    print(result)
    print(f"  -> found={result.found}, 最后尝试的算法: {result.method_used}")


if __name__ == "__main__":
    example_auto_strategy()
    example_single_strategy()
    example_custom_config()
    example_not_found()
