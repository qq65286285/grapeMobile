"""
src/locate/vlm.py — VLM 视觉 grounding 与候选验证
====================================================
Step B2: 用 VLM(agnes-3.0-flash)在设备截图中定位 UI 元素。
Step D:  对候选裁剪后让 VLM 二次确认,过滤误检。

对外接口:
    - VlmLocator.locate(image, intent) -> List[Candidate]            # grounding
    - VlmLocator.verify(image, candidate, intent) -> (bool, str)   # 验证
"""

from __future__ import annotations

import json
import os
import re
import statistics
import tempfile
from typing import List, Optional, Tuple

import cv2
import numpy as np

from aiclient import AIClient, AIError

from .config import VLMConfig
from .types import BBox, Candidate, Intent
from .viz import crop_and_pad

import cvio


# ---------------------------------------------------------------------------
# 内部工具:JSON 容错解析、归一化 bbox 的 IoU / 聚类 / 中位数
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[dict]:
    """容错地从 VLM 返回文本中提取 JSON 对象。"""
    if not text:
        return None
    text = text.strip()
    # 1. 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2. 正则提取 {...}(支持嵌套一层大括号)
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # 3. 退而求其次:第一个 { 到最后一个 }
    start = text.find("{")
    if start == -1:
        return None
    end = text.rfind("}")
    if end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _iou_norm(a, b) -> float:
    """计算两个 0~1000 归一化 bbox [x1,y1,x2,y2] 的 IoU。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    b_area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = a_area + b_area - inter
    return float(inter / union) if union > 0 else 0.0


def _cluster_by_iou_norm(bboxes, threshold: float = 0.5) -> List[List[int]]:
    """按 IoU > threshold 对归一化 bbox 聚类(BFS),返回索引组列表。"""
    n = len(bboxes)
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
            for k in range(n):
                if not visited[k] and _iou_norm(bboxes[j], bboxes[k]) > threshold:
                    visited[k] = True
                    cluster.append(k)
                    stack.append(k)
        clusters.append(cluster)
    return clusters


def _median_bbox(bboxes) -> List[float]:
    """取一组归一化 bbox 的逐分量中位数。"""
    return [
        float(statistics.median(b[0] for b in bboxes)),
        float(statistics.median(b[1] for b in bboxes)),
        float(statistics.median(b[2] for b in bboxes)),
        float(statistics.median(b[3] for b in bboxes)),
    ]


# ---------------------------------------------------------------------------
# VlmLocator
# ---------------------------------------------------------------------------

class VlmLocator:
    """VLM 视觉 grounding + 候选验证。"""

    # grounding 系统提示
    _GROUNDING_SYSTEM = (
        "你是 Android 自动化助手,用户给你一张设备截图,"
        "你要找到用户描述的元素。"
        "坐标使用 0~1000 归一化(左上为 (0,0),右下为 (1000,1000)),"
        "输出格式为严格 JSON: "
        '{"thinking":"你对目标外观/位置的简要分析和判断依据",'
        '"bbox":[x1,y1,x2,y2], "label":"元素描述", "confidence":0~1}。'
        "如果目标是图标(icon 或 icon_with_label),要找的是图标本体本身,"
        "不是它下方的文字标签。"
        "如果找不到该元素,返回 "
        '{"thinking":"找不到的原因", "bbox": null, "label": "not found", "confidence": 0}。'
        "只返回 JSON,不要有任何额外文字或代码块标记。"
    )

    def __init__(self, config: VLMConfig) -> None:
        self.config = config
        self._client: Optional[AIClient] = None

    # ------------------------------------------------------------------
    # 客户端
    # ------------------------------------------------------------------
    def _ensure_client(self) -> AIClient:
        """
        惰性创建 AIClient。
        config.api_key 为空时用 AIClient 默认配置(已有硬编码 key)。
        """
        if self._client is None:
            cfg = self.config
            if cfg.api_key:
                self._client = AIClient(
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                    model=cfg.model,
                    timeout=cfg.timeout,
                )
            else:
                # api_key 为空,用 AIClient 默认配置(已有硬编码 key)
                self._client = AIClient(model=cfg.model, timeout=cfg.timeout)
        return self._client

    # ------------------------------------------------------------------
    # grounding
    # ------------------------------------------------------------------
    def locate(self, image: np.ndarray, intent: Intent) -> List[Candidate]:
        """
        用 VLM 在截图中定位 intent 描述的元素。

        Returns:
            候选列表(通常 1 个);失败时返回空列表。
        """
        try:
            if image is None or image.size == 0:
                return []

            # 1. 准备干净截图(不叠网格),长边超限则等比缩小
            h, w = image.shape[:2]
            scale = 1.0
            work = image
            long_side = max(h, w)
            if long_side > self.config.max_side:
                scale = self.config.max_side / long_side
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                work = cv2.resize(work, (new_w, new_h), interpolation=cv2.INTER_AREA)

            # 2. 保存到临时文件
            fd, tmp_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            cvio.imwrite(tmp_path, work)

            try:
                client = self._ensure_client()
                user_prompt = self._build_grounding_prompt(intent)

                # 4. 多次采样(temperature 在 0.3~0.5 范围内变化)
                samples = max(1, self.config.samples)
                bboxes_norm: List[List[float]] = []
                labels: List[str] = []
                confs: List[float] = []
                for i in range(samples):
                    t = self._sample_temperature(i, samples)
                    try:
                        resp = client.chat_with_image(
                            user_prompt,
                            tmp_path,
                            system=self._GROUNDING_SYSTEM,
                            temperature=t,
                        )
                    except AIError:
                        continue
                    # 5. 解析每次返回的 JSON
                    parsed = _extract_json(resp)
                    if not parsed:
                        continue
                    bbox = parsed.get("bbox")
                    if not bbox:
                        continue  # not found
                    try:
                        x1, y1, x2, y2 = (float(v) for v in bbox)
                    except (TypeError, ValueError):
                        continue
                    bboxes_norm.append([x1, y1, x2, y2])
                    labels.append(str(parsed.get("label", "")).strip())
                    confs.append(float(parsed.get("confidence", 0.0)))

                if not bboxes_norm:
                    return []

                # 6. 多次采样 bbox 按 IoU > 0.5 聚类,取最大簇的中位数 bbox
                clusters = _cluster_by_iou_norm(bboxes_norm, threshold=0.5)
                biggest = max(clusters, key=len)
                median_bbox = _median_bbox([bboxes_norm[i] for i in biggest])

                # 采样一致性 = 最大簇样本数 / 有效样本总数
                consistency = len(biggest) / float(len(bboxes_norm))
                avg_conf = sum(confs[i] for i in biggest) / len(biggest)

                # 7. 归一化坐标还原成像素坐标(基于送入 VLM 的 work 图尺寸)
                wh, ww = work.shape[0], work.shape[1]
                x1, y1, x2, y2 = median_bbox
                px1 = int(round(x1 / 1000.0 * ww))
                py1 = int(round(y1 / 1000.0 * wh))
                px2 = int(round(x2 / 1000.0 * ww))
                py2 = int(round(y2 / 1000.0 * wh))
                # 8. 如果缩放过,还原到原图坐标
                if scale != 1.0:
                    inv = 1.0 / scale
                    px1 = int(round(px1 * inv))
                    py1 = int(round(py1 * inv))
                    px2 = int(round(px2 * inv))
                    py2 = int(round(py2 * inv))
                # 钳到原图范围
                px1 = max(0, min(px1, w - 1))
                py1 = max(0, min(py1, h - 1))
                px2 = max(0, min(px2, w))
                py2 = max(0, min(py2, h))

                bbox_px: BBox = (px1, py1, px2, py2)
                point = ((px1 + px2) // 2, (py1 + py2) // 2)
                # 取簇内首个非空 label 作为描述
                cluster_labels = [labels[i] for i in biggest if labels[i]]
                text = cluster_labels[0] if cluster_labels else ""
                # 9. score: 采样一致性越高分数越高(0.5~1.0)
                score = 0.5 + 0.5 * consistency

                return [Candidate(
                    bbox=bbox_px,
                    point=point,
                    score=score,
                    source="vlm",
                    text=text,
                    confidence=avg_conf,
                )]
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 验证
    # ------------------------------------------------------------------
    def verify(
        self,
        image: np.ndarray,
        candidate: Candidate,
        intent: Intent,
    ) -> Tuple[bool, str]:
        """
        裁剪 candidate 区域,问 VLM 主体是否匹配 intent.target_text。

        Returns:
            (是否通过, 理由);调用失败返回 (False, "VLM调用失败")。
        """
        try:
            if image is None or image.size == 0:
                candidate.verified = False
                candidate.verify_reason = "图像为空"
                return False, "图像为空"

            # 1. 裁剪 bbox 区域(外扩 20%,至少 256px)
            crop = crop_and_pad(
                image,
                candidate.bbox,
                expand=0.2,
                min_size=256,
            )

            # 2. 保存裁剪图到临时文件
            fd, tmp_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            cvio.imwrite(tmp_path, crop)

            try:
                # 3. 问 VLM
                prompt = (
                    f"这张图里的主体是不是「{intent.target_text}」?"
                    f'只回答 JSON: {{"yes": true/false, "reason": "..."}}'
                )
                client = self._ensure_client()
                try:
                    resp = client.chat_with_image(
                        prompt,
                        tmp_path,
                        temperature=0.2,
                    )
                except AIError:
                    candidate.verified = False
                    candidate.verify_reason = "VLM调用失败"
                    return False, "VLM调用失败"

                # 4. 解析返回,设置 candidate.verified / verify_reason
                parsed = _extract_json(resp)
                if not parsed:
                    candidate.verified = False
                    candidate.verify_reason = "VLM返回解析失败"
                    return False, "VLM返回解析失败"

                yes = parsed.get("yes")
                reason = str(parsed.get("reason", "")).strip()
                # 容错:yes 可能是 bool / 字符串 / 0/1
                if isinstance(yes, str):
                    yes = yes.strip().lower() in ("true", "yes", "1", "y")
                else:
                    yes = bool(yes)

                candidate.verified = bool(yes)
                candidate.verify_reason = reason or ("匹配" if yes else "不匹配")
                return candidate.verified, candidate.verify_reason
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:
            candidate.verified = False
            candidate.verify_reason = "VLM调用失败"
            return False, "VLM调用失败"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _build_grounding_prompt(self, intent: Intent) -> str:
        """根据 Intent 构造 grounding 用户提示。"""
        type_desc = {
            "icon_with_label": "图标(带文字标签)",
            "icon": "纯图标(无文字)",
            "text": "文字",
            "other": "元素",
        }.get(intent.target_type, "元素")

        parts = [
            f"请在截图中找到这个元素:目标文字/名称 = 「{intent.target_text}」。",
            f"目标类型: {type_desc}。",
        ]
        if intent.target_type in ("icon", "icon_with_label"):
            parts.append("注意:要找的是图标本体本身,不是它下方的文字标签。")
        if intent.visual_hint:
            parts.append(f"外观/位置提示: {intent.visual_hint}。")
        parts.append("先简要描述该元素的外观和大致位置,然后输出严格 JSON。")
        return "\n".join(parts)

    def _sample_temperature(self, i: int, samples: int) -> float:
        """在 0.3~0.5 范围内为第 i 次采样取温度,鼓励多样性。"""
        if samples <= 1:
            return float(max(0.3, min(0.5, self.config.temperature)))
        # 在 [0.3, 0.5] 内线性分布
        t = 0.3 + 0.2 * i / (samples - 1)
        return float(max(0.3, min(0.5, t)))
