"""
src/locate/ — AI 辅助定位包
============================
多策略融合定位:OCR 文字定位 + VLM 视觉 grounding + 模板匹配,
经过候选融合与裁剪验证后输出高置信度坐标。

入口: pipeline.locate(image, query) -> LocateResult
"""

from .types import Candidate, LocateResult  # noqa: F401
