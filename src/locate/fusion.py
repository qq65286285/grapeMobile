"""
src/locate/fusion.py — 多源候选融合
=====================================
Step C: 把 OCR / VLM / 模板匹配产生的候选按空间重叠聚类,
        结合来源权重与多源印证打分,输出排序后的 Top-K。

对外接口:
    - CandidateFusion.fuse(candidates) -> List[Candidate]  # 排序后 Top-K

辅助函数:
    - iou(a, b) -> float                 # 计算两个 bbox 的 IoU
    - cluster_by_iou(candidates, thr) -> List[List[int]]   # 聚类返回索引组
"""

from __future__ import annotations

from typing import List, Tuple

from .config import FusionConfig
from .types import BBox, Candidate


def iou(a: BBox, b: BBox) -> float:
    """计算两个像素 bbox (x1,y1,x2,y2) 的 IoU。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    b_area = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = a_area + b_area - inter
    return float(inter / union) if union > 0 else 0.0


def _center_distance(a: Candidate, b: Candidate) -> float:
    """两个候选中心点的欧氏距离(像素)。"""
    ax, ay = a.center
    bx, by = b.center
    return float(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)


def _min_bbox_dim(c: Candidate) -> float:
    """候选 bbox 的最短边(像素),至少 1。"""
    x1, y1, x2, y2 = c.bbox
    return float(max(1, min(x2 - x1, y2 - y1)))


def cluster_by_iou(
    candidates: List[Candidate],
    threshold: float = 0.3,
) -> List[List[int]]:
    """
    按 IoU > threshold 或中心距离 < 最短边 聚类(BFS)。

    Args:
        candidates:  候选列表。
        threshold:   IoU 阈值,默认 0.3。

    Returns:
        索引组列表(每组是同一目标的多个候选索引)。
    """
    n = len(candidates)
    visited = [False] * n
    clusters: List[List[int]] = []
    for i in range(n):
        if visited[i]:
            continue
        cluster = [i]
        visited[i] = True
        stack = [i]
        while stack:
            j = stack.pop()
            min_dim_j = _min_bbox_dim(candidates[j])
            for k in range(n):
                if visited[k]:
                    continue
                same = (
                    iou(candidates[j].bbox, candidates[k].bbox) > threshold
                    or _center_distance(candidates[j], candidates[k]) < min_dim_j
                )
                if same:
                    visited[k] = True
                    cluster.append(k)
                    stack.append(k)
        clusters.append(cluster)
    return clusters


class CandidateFusion:
    """多源候选融合:聚类 + 加权打分 + Top-K。"""

    def __init__(self, config: FusionConfig) -> None:
        self.config = config
        self._weights = config.weights
        self._top_k = 3

    def fuse(self, candidates: List[Candidate]) -> List[Candidate]:
        """
        对所有候选聚类、加权打分、排序,返回 Top-K。

        Returns:
            按融合分数降序排序的候选列表(最多 K=3 个)。
        """
        if not candidates:
            return []

        # 1. 按 IoU > 0.3 或中心距离 < 最短边 聚类
        clusters = cluster_by_iou(candidates, threshold=0.3)

        results: List[Candidate] = []
        for idxs in clusters:
            members = [candidates[i] for i in idxs]
            fused_score = self._score_cluster(members)

            # 3. 簇的最终 bbox 和 point 取簇内得分最高者
            best = max(members, key=lambda c: c.score)
            merged = Candidate(
                bbox=best.bbox,
                point=best.point,
                score=fused_score,
                source=best.source,
                text=best.text,
                confidence=best.confidence,
                verified=best.verified,
                verify_reason=best.verify_reason,
                sources=sorted({m.source for m in members}),
            )
            results.append(merged)

        # 4. 按融合分数降序排序,取 Top-K
        results.sort(key=lambda c: c.score, reverse=True)
        return results[: self._top_k]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _score_cluster(self, members: List[Candidate]) -> float:
        """为单个簇计算融合分数(基于来源权重 + 多源印证)。"""
        score = 0.0
        sources_seen = set()
        has_template = False
        has_ocr_exact = False
        has_ocr_fuzzy = False
        vlm_count = 0

        for m in members:
            src = m.source
            sources_seen.add(src)
            if src == "template":
                has_template = True
            elif src == "ocr":
                if m.score >= 1.0:
                    has_ocr_exact = True
                else:
                    has_ocr_fuzzy = True
            elif src == "vlm":
                vlm_count += 1

        # 如果有 template 来源
        if has_template:
            score += self._weights.get("template", 0.0)
        # 如果有 ocr 精确匹配(score=1.0)
        if has_ocr_exact:
            score += self._weights.get("ocr_exact", 0.0)
        # 模糊匹配
        if has_ocr_fuzzy:
            score += self._weights.get("ocr_fuzzy", 0.0)
        # 如果有 vlm 来源且多次采样一致(同一簇里有多个 vlm 候选)
        if vlm_count >= 2:
            score += self._weights.get("vlm_consistent", 0.0)
        # 多来源印证:每多一个不同来源 +
        if len(sources_seen) >= 2:
            score += self._weights.get("multi_source", 0.0) * (len(sources_seen) - 1)

        return float(score)
