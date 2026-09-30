"""
imgloc.matchers - 各算法层匹配器实现
=====================================
按责任链顺序包含：
    template  - 模板匹配层（cv2.matchTemplate + 多尺度金字塔 + RGB加权 + 旋转不变）
    shape     - 形状匹配层（边缘方向梯度，抗光照变化）
    orb       - ORB 特征点匹配层
    sift      - SIFT 特征点匹配层
    lightglue - 深度学习兜底层（可选依赖）
"""

from imgloc.matchers.template import TemplateMatcher
from imgloc.matchers.shape import ShapeMatcher
from imgloc.matchers.orb import OrbMatcher
from imgloc.matchers.sift import SiftMatcher
from imgloc.matchers.lightglue_matcher import LightGlueMatcher

__all__ = [
    "TemplateMatcher",
    "ShapeMatcher",
    "OrbMatcher",
    "SiftMatcher",
    "LightGlueMatcher",
]
