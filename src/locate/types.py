"""
src/locate/types.py — 定位结果的数据结构
==========================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


BBox = Tuple[int, int, int, int]  # (x1, y1, x2, y2) 像素坐标


@dataclass
class Candidate:
    """单个定位候选(来自 OCR / VLM / 模板匹配)。"""

    bbox: BBox                      # 候选框(像素)
    point: Tuple[int, int]          # 点击中心坐标(像素)
    score: float                    # 原始得分(0~1)
    source: str                     # "ocr" | "vlm" | "template"
    text: str = ""                  # OCR 识别到的文字 / VLM 描述
    confidence: float = 0.0         # 来源自身的置信度
    verified: bool = False          # 是否通过验证阶段
    verify_reason: str = ""         # 验证理由
    # 融合簇内出现过的全部来源(如 ["ocr","vlm"]);单源候选为空,显示时回退 source
    sources: List[str] = field(default_factory=list)

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bbox
        return max(0, x2 - x1) * max(0, y2 - y1)

    @property
    def center(self) -> Tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) // 2, (y1 + y2) // 2)


@dataclass
class Intent:
    """Step A: 意图解析结果。"""

    target_text: str               # 提取的目标文字(如 "KOF:Legend";输入指令时为输入框)
    target_type: str               # icon_with_label | text | icon | other
    visual_hint: str = ""          # 外观提示(如 "左上角的红色图标")
    raw_query: str = ""            # 用户原始输入
    action: str = "tap"            # tap | input(点击 | 输入文字)
    input_text: str = ""           # action=input 时要输入的文本


@dataclass
class LocateResult:
    """定位最终输出。"""

    ok: bool
    point: Tuple[int, int] = (0, 0)           # 点击像素坐标
    bbox: BBox = (0, 0, 0, 0)
    confidence: float = 0.0
    sources: List[str] = field(default_factory=list)
    grid_cell: str = ""                       # 兼容旧流程的格子编号
    debug_images: Dict[str, str] = field(default_factory=dict)
    reason: str = ""                          # 失败原因
    query: str = ""                           # 用户原始描述
    candidates: List[Candidate] = field(default_factory=list)  # 所有候选(调试用)
    top_candidates: List[Candidate] = field(default_factory=list)  # 融合排序后的候选(GUI 切换用)
    intent: Optional[Intent] = None               # 意图解析结果(GUI 据此区分点击/输入)
