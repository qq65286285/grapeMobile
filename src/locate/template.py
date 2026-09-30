"""
src/locate/template.py — 模板匹配定位(Step B3)
================================================
封装现有 imgloc 库,在当前截图中查找已保存的模板图片。
用户确认过的定位结果会自动保存为模板,下次优先走此路径。
"""

from __future__ import annotations

import logging
import os
from typing import List

import cv2
import numpy as np

import cvio
from .types import Candidate, Intent

logger = logging.getLogger("imgloc.locate.template")

# 模板存放根目录
ANCHORS_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                           "runner", "scripts", "anchors")


class TemplateLocator:
    """用 imgloc 多策略匹配在截图中找模板。"""

    def __init__(self, anchors_dir: str = ANCHORS_DIR):
        self.anchors_dir = os.path.abspath(anchors_dir)

    def locate(self, image: np.ndarray, intent: Intent) -> List[Candidate]:
        """
        在 anchors/ 目录下找与 intent.target_text 匹配的模板图,
        用 imgloc 在截图中定位。

        Args:
            image: 设备截图(BGR)。
            intent: 意图解析结果(target_text 用于索引模板)。

        Returns:
            候选列表(命中且得分高时返回,否则空列表)。
        """
        target = intent.target_text.strip()
        if not target:
            return []

        # 查找模板图片: anchors/{target}.png 或 anchors/{target}/latest.png
        template_path = self._find_template(target)
        if template_path is None:
            return []

        template = cvio.imread(template_path)
        if template is None:
            logger.warning("模板图片读取失败: %s", template_path)
            return []

        # 用 imgloc 的 LocateService 定位
        try:
            import sys
            src_dir = os.path.join(os.path.dirname(__file__), "..")
            if src_dir not in sys.path:
                sys.path.insert(0, os.path.abspath(src_dir))
            from service import LocateService
        except ImportError:
            logger.warning("无法导入 imgloc LocateService")
            return []

        # 保存截图到临时文件(LocateService 需要文件路径)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        cvio.imwrite(tmp_path, image)

        try:
            service = LocateService(strategy="auto", threshold=0.7)
            coord = service.locate(template_path, tmp_path)
        except Exception as exc:
            logger.warning("模板匹配失败: %s", exc)
            coord = None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        if coord is None:
            return []

        x, y = coord
        # 从模板尺寸推算 bbox
        th, tw = template.shape[:2]
        bbox = (max(0, x - tw // 2), max(0, y - th // 2),
                x + tw // 2, y + th // 2)

        return [Candidate(
            bbox=bbox, point=(x, y), score=1.0, source="template",
            text=intent.target_text, confidence=1.0,
        )]

    def save_template(self, image: np.ndarray, bbox, target_text: str) -> str:
        """
        保存裁剪的模板图到 anchors/ 目录,供下次匹配。

        Args:
            image: 原截图。
            bbox: 裁剪框。
            target_text: 目标描述(用作文件名)。

        Returns:
            保存的文件路径;失败返回空串。
        """
        if not target_text:
            return ""
        x1, y1, x2, y2 = bbox
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            return ""

        safe_name = target_text.replace(" ", "_").replace("/", "_")[:50]
        save_dir = os.path.join(self.anchors_dir)
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{safe_name}.png")
        if cvio.imwrite(save_path, crop):
            logger.info("模板已保存: %s", save_path)
            return save_path
        return ""

    def _find_template(self, target_text: str) -> str | None:
        """在 anchors/ 目录查找匹配的模板图片。"""
        safe = target_text.replace(" ", "_").replace("/", "_")[:50]
        # 直接匹配
        path = os.path.join(self.anchors_dir, f"{safe}.png")
        if os.path.isfile(path):
            return path
        # 大小写不敏感
        if os.path.isdir(self.anchors_dir):
            for name in os.listdir(self.anchors_dir):
                stem = os.path.splitext(name)[0]
                if stem.lower() == safe.lower():
                    return os.path.join(self.anchors_dir, name)
        return None
