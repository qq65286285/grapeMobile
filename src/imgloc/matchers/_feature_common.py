"""
特征点匹配公共逻辑 (Feature Matching Common Utilities)
========================================================
ORB 和 SIFT 匹配器共享同一套"检测关键点 -> 描述子匹配 -> RANSAC单应性估计
-> 计算目标框/中心点/旋转角度"流程，仅检测器(detector)和匹配参数不同，
因此抽取为公共函数，避免重复代码。

参考设计思路：Airtest 项目 aircv 模块中的 SIFT 匹配核心逻辑
(https://github.com/AirtestProject/Airtest/tree/master/airtest/aircv)
——即"关键点检测 + knnMatch + 距离比值过滤 + findHomography(RANSAC)"的经典组合，
本实现为独立重写，不引入 airtest 整体依赖。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from imgloc.types import MatchResult
from imgloc.utils import to_gray


def build_matcher(matcher_type: str, is_binary_descriptor: bool):
    """
    构建描述子匹配器。

    Args:
        matcher_type: "bf"(BFMatcher) 或 "flann"(FlannBasedMatcher)。
        is_binary_descriptor: 描述子是否为二进制类型（ORB=True，SIFT=False），
            FLANN 对二进制描述子和浮点描述子需要不同的索引参数(LSH vs KDTree)。
    """
    if matcher_type == "flann":
        if is_binary_descriptor:
            # ORB 等二进制描述子用 LSH 索引
            index_params = dict(algorithm=6, table_number=6, key_size=12, multi_probe_level=1)
        else:
            # SIFT 等浮点描述子用 KDTree 索引
            index_params = dict(algorithm=1, trees=5)
        search_params = dict(checks=50)
        return cv2.FlannBasedMatcher(index_params, search_params)
    # 默认 BFMatcher；二进制描述子用 HAMMING 距离，浮点描述子用 L2 距离
    norm_type = cv2.NORM_HAMMING if is_binary_descriptor else cv2.NORM_L2
    return cv2.BFMatcher(norm_type)


def knn_match_and_filter(
    matcher, des1: np.ndarray, des2: np.ndarray, knn_ratio: float = 0.75
) -> List[cv2.DMatch]:
    """
    执行 KNN(k=2) 匹配，并用经典的 Lowe's ratio test 过滤误匹配点。

    原理：对每个描述子取最近邻和次近邻，若最近邻距离远小于次近邻距离
    (比值 < knn_ratio)，说明该匹配足够"独特"，可信度高；否则认为是歧义匹配，丢弃。
    """
    if des1 is None or des2 is None or len(des1) < 2 or len(des2) < 2:
        return []
    try:
        raw_matches = matcher.knnMatch(des1, des2, k=2)
    except cv2.error:
        return []

    good: List[cv2.DMatch] = []
    for pair in raw_matches:
        if len(pair) != 2:
            continue
        m, n = pair
        if m.distance < knn_ratio * n.distance:
            good.append(m)
    return good


def estimate_homography_and_build_result(
    kp_target: List[cv2.KeyPoint],
    kp_source: List[cv2.KeyPoint],
    good_matches: List[cv2.DMatch],
    target_shape: Tuple[int, int],
    min_match_count: int,
    ransac_reproj_threshold: float,
    method_name: str,
) -> MatchResult:
    """
    用过滤后的良好匹配点估计单应性矩阵(RANSAC)，据此计算目标在大图中的
    四个角点、外接矩形、中心点、旋转角度和缩放比例。

    Args:
        kp_target: 模板图（小图）的关键点列表。
        kp_source: 大图的关键点列表。
        good_matches: 经过 ratio test 过滤的匹配对（query=target索引, train=source索引）。
        target_shape: 模板图的 (h, w)。
        min_match_count: 最少需要多少个良好匹配点才尝试估计单应性，过少则不可信。
        ransac_reproj_threshold: RANSAC 重投影误差阈值(像素)。
        method_name: 写入 MatchResult.method_used 的算法名。

    Returns:
        MatchResult: found=True 时包含 rect/center_point/angle/scale/raw_corners；
                     匹配点不足或单应性求解失败时返回 found=False。
    """
    # cv2.findHomography(RANSAC) 硬性要求至少4个点，即使配置的 min_match_count 更低也需兜底，
    # 避免因配置不当导致底层 OpenCV 抛出异常（该异常会被 Matcher.match() 捕获，
    # 但这里提前判断可以提供更明确的 not_found 原因，便于调试）。
    effective_min_count = max(min_match_count, 4)
    if len(good_matches) < effective_min_count:
        return MatchResult.not_found(
            method_used=method_name,
            extra={"good_matches": len(good_matches), "reason": "not_enough_matches"},
        )

    src_pts = np.float32([kp_target[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp_source[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

    homography, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, ransac_reproj_threshold)
    if homography is None:
        return MatchResult.not_found(
            method_used=method_name,
            extra={"good_matches": len(good_matches), "reason": "homography_failed"},
        )

    inlier_count = int(mask.sum()) if mask is not None else 0
    confidence = inlier_count / max(len(good_matches), 1)

    h, w = target_shape
    template_corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    projected_corners = cv2.perspectiveTransform(template_corners, homography).reshape(-1, 2)

    xs = projected_corners[:, 0]
    ys = projected_corners[:, 1]
    x0, y0 = float(np.min(xs)), float(np.min(ys))
    x1, y1 = float(np.max(xs)), float(np.max(ys))
    rect = (int(round(x0)), int(round(y0)), int(round(x1 - x0)), int(round(y1 - y0)))
    center_point = (int(round((x0 + x1) / 2)), int(round((y0 + y1) / 2)))

    angle = _estimate_rotation_angle(homography)
    scale = _estimate_scale(homography)

    corners_tuple = tuple(tuple(pt) for pt in projected_corners)  # type: ignore[assignment]

    return MatchResult(
        found=True,
        rect=rect,
        center_point=center_point,
        confidence=float(confidence),
        angle=angle,
        scale=scale,
        method_used=method_name,
        raw_corners=corners_tuple,  # type: ignore[arg-type]
        extra={"good_matches": len(good_matches), "inliers": inlier_count},
    )


def _estimate_rotation_angle(homography: np.ndarray) -> float:
    """从单应性矩阵的线性部分估计旋转角度（度），基于第一列向量的方向。"""
    a, b = homography[0, 0], homography[1, 0]
    angle_rad = math.atan2(b, a)
    return math.degrees(angle_rad)


def _estimate_scale(homography: np.ndarray) -> float:
    """从单应性矩阵的线性部分估计缩放比例（取两个基向量长度的几何平均）。"""
    a, b = homography[0, 0], homography[1, 0]
    c, d = homography[0, 1], homography[1, 1]
    scale_x = math.hypot(a, b)
    scale_y = math.hypot(c, d)
    if scale_x <= 1e-6 or scale_y <= 1e-6:
        return 1.0
    return float(math.sqrt(scale_x * scale_y))


def detect_and_compute(detector, image: np.ndarray) -> Tuple[Optional[List[cv2.KeyPoint]], Optional[np.ndarray]]:
    """统一的关键点检测+描述子计算封装，自动转灰度图。"""
    gray = to_gray(image)
    keypoints, descriptors = detector.detectAndCompute(gray, None)
    return keypoints, descriptors
