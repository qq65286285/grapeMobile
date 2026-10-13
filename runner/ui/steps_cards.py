"""
runner/ui/steps_cards.py - StepApp 步骤卡片渲染职责域(自 steps_mixin 拆出,CR-08)
==========================================================================
steps_mixin 二次拆分: 中栏大卡片与右栏普通模式紧凑卡片的构建/刷新,
及"选中高亮/等待 chip 原地更新"等不重建列表的增量操作。

性能约定(CR-03 修复遗产): _update_card_selection / _refresh_card_wait_chips
只做原地更新,严禁全量重建卡片列表。
"""

from __future__ import annotations

from typing import List, Optional

import tkinter as tk

from ui.theme import (
    THEME,
    FONT_CAPTION, FONT_MONO, FONT_UI, FONT_UI_BOLD,
)
from ui.widgets import _RoundedBadge, _RoundedButton
from steps import APP_OP_TYPES


class _CardsMixin:
    """步骤卡片渲染与选中联动(混入 StepApp,依赖 _StepsMixin 提供步骤数据)。"""

    # ------------------------------------------------------------------
    # 卡片高亮 / 等待 chip(原地更新,不重建)
    # ------------------------------------------------------------------
    def _refresh_card_wait_chips(self, i: int) -> None:
        """刷新指定索引卡片上的前/后等待 chip 文本。"""
        db = self.steps[i].get("delay_before", 0)
        da = self.steps[i].get("wait_after", 0)
        for attr in ("_step_card_widgets", "_normal_card_widgets"):
            cards = getattr(self, attr, None)
            if not cards or not (0 <= i < len(cards)):
                continue
            card = cards[i]
            if not card.winfo_exists():
                continue
            # 中栏卡片的 meta 帧在 row=2,直接遍历其子 Label
            self._update_wait_labels(card, db, da)

    @staticmethod
    def _update_wait_labels(card: "tk.Frame", db: float, da: float) -> None:
        """把卡片内文本为 '前Ns'/'后Ns' 的 Label 刷新为新值(限定 3 层深度)。"""
        # 卡片布局:card(0) -> meta 帧(1) -> Label(2),只在第 2 层查找
        try:
            meta_frames = [c for c in card.winfo_children()
                           if isinstance(c, tk.Frame)]
        except tk.TclError:
            return
        for meta in meta_frames:
            try:
                children = meta.winfo_children()
            except tk.TclError:
                continue
            for chip in children:
                try:
                    txt = chip.cget("text")
                except tk.TclError:
                    continue
                if isinstance(txt, str):
                    if txt.startswith("前") and txt.endswith("s"):
                        chip.configure(text=f"前{db:g}s")
                    elif txt.startswith("后") and txt.endswith("s"):
                        chip.configure(text=f"后{da:g}s")

    def _update_card_selection(self, old_idx: Optional[int],
                                new_idx: Optional[int]) -> None:
        """仅更新新旧卡片边框颜色(不改 thickness 避免触发 <Configure> 级联)。"""
        for attr in ("_step_card_widgets", "_normal_card_widgets"):
            cards = getattr(self, attr, None)
            if not cards:
                continue
            for idx in (old_idx, new_idx):
                if idx is None or not (0 <= idx < len(cards)):
                    continue
                card = cards[idx]
                if not card.winfo_exists():
                    continue
                card.configure(highlightbackground=(
                    THEME["accent"] if idx == new_idx else THEME["border"]))

    # ------------------------------------------------------------------
    # 中栏步骤卡片
    # ------------------------------------------------------------------
    def _refresh_step_cards(self) -> None:
        """重建中栏步骤卡片列表(Canvas 滚动区 + 每张卡片一个 Frame)。"""
        for child in self._step_cards_inner.winfo_children():
            child.destroy()
        self._step_card_widgets: List[tk.Frame] = []
        if not self.steps:
            tk.Label(self._step_cards_inner, text="(暂无步骤,点击左侧画面添加)",
                     fg=THEME["txt3"], bg=THEME["surface"],
                     font=FONT_CAPTION).pack(pady=20)
            return
        for i, step in enumerate(self.steps):
            card = tk.Frame(self._step_cards_inner, bg=THEME["bg2"],
                            highlightthickness=1, highlightbackground=THEME["border"])
            card.pack(fill="x", pady=(0, 6), padx=1)
            # 选中态高亮(仅改颜色,不改 thickness 避免布局抖动)
            if self._preview_index == i:
                card.configure(highlightbackground=THEME["accent"])
            # 编号徽章(圆角)
            num = _RoundedBadge(card, text=str(i + 1), size=22, radius=5)
            num.grid(row=0, column=0, rowspan=2, padx=6, pady=6, sticky="n")
            # 主体
            body = tk.Frame(card, bg=THEME["bg2"])
            body.grid(row=0, column=1, sticky="ew", pady=(6, 0))
            card.grid_columnconfigure(1, weight=1)
            stype = step.get("type")
            desc = (step.get("desc") or "").strip()
            if stype == "input":
                ttl = desc or f"输入 \"{step.get('text', '')}\""
            elif stype in APP_OP_TYPES or stype == "keyevent":
                ttl = self._step_action_text(step)
            else:
                ttl = desc or f"点击 {step.get('cell', '')}"
            tk.Label(body, text=ttl, fg=THEME["txt"], bg=THEME["bg2"],
                     font=FONT_UI, anchor="w").pack(side="left", fill="x", expand=True)
            # 锚点标记
            has_anchor = step.get("before_image") or step.get("after_image")
            if has_anchor:
                tk.Label(body, text="◎锚点", fg=THEME["accent"], bg=THEME["bg2"],
                         font=FONT_CAPTION).pack(side="left", padx=(4, 0))
            # 副标题(格子/坐标/系统操作)
            sub = tk.Frame(card, bg=THEME["bg2"])
            sub.grid(row=1, column=1, sticky="w", padx=(0, 6), pady=(2, 0))
            if stype == "input":
                sub_text = f"格子 {step.get('cell', '')}"
            elif stype in APP_OP_TYPES or stype == "keyevent":
                sub_text = "系统操作"
            else:
                x, y = step.get("x"), step.get("y")
                sub_text = f"格子 {step.get('cell', '')}" + (
                    f" · ({x}, {y})" if x is not None and y is not None else "")
                conf = step.get("confidence")
                if conf is not None:
                    sub_text += f" · 置信度 {conf}"
            tk.Label(sub, text=sub_text, fg=THEME["txt2"], bg=THEME["bg2"],
                     font=FONT_MONO, anchor="w").pack(side="left")
            # 等待 chip
            meta = tk.Frame(card, bg=THEME["bg2"])
            meta.grid(row=2, column=1, sticky="w", padx=(0, 6), pady=(4, 6))
            tk.Label(meta, text=f"前{step.get('delay_before', 0):g}s",
                     fg=THEME["txt2"], bg=THEME["surface2"], font=FONT_MONO,
                     padx=4, pady=1).pack(side="left", padx=(0, 4))
            tk.Label(meta, text=f"后{step.get('wait_after', 0):g}s",
                     fg=THEME["txt2"], bg=THEME["surface2"], font=FONT_MONO,
                     padx=4, pady=1).pack(side="left")
            # 操作按钮(上移/下移/删除):24x24 圆角小方块
            acts = tk.Frame(card, bg=THEME["bg2"])
            acts.grid(row=0, column=2, rowspan=3, padx=(0, 6), pady=6, sticky="ne")
            for icon, cmd in [
                ("△", lambda idx=i: self._move(-1)),
                ("▽", lambda idx=i: self._move(1)),
                ("✕", lambda idx=i: self._delete_step(idx)),
            ]:
                _RoundedButton(acts, text=icon, command=cmd,
                               width=24, height=24, radius=5).pack(side="left", padx=1)
            # 交互:点击卡片选中;双击改等待
            for w in (card, body, sub, meta, num):
                w.bind("<Button-1>", lambda _e, idx=i: self._select_card(idx))
                w.bind("<Double-Button-1>", lambda _e, idx=i: self._edit_step_waits(idx))
            self._step_card_widgets.append(card)

    # ------------------------------------------------------------------
    # 右栏普通模式紧凑卡片
    # ------------------------------------------------------------------
    def _refresh_normal_cards(self) -> None:
        """重建右栏普通模式的紧凑步骤卡片(nstep:编号+标题+副标题+上移/下移/删除)。"""
        for child in self._normal_inner.winfo_children():
            child.destroy()
        self._normal_card_widgets: List[tk.Frame] = []
        if not self.steps:
            tk.Label(self._normal_inner, text="(暂无步骤)",
                     fg=THEME["txt3"], bg=THEME["surface"],
                     font=FONT_CAPTION).pack(pady=16)
            return
        for i, step in enumerate(self.steps):
            stype = step.get("type")
            desc = (step.get("desc") or "").strip()
            if stype == "input":
                ttl = desc or f"输入 \"{step.get('text', '')}\""
            elif stype in APP_OP_TYPES or stype == "keyevent":
                ttl = self._step_action_text(step)
            else:
                ttl = desc or f"点击 {step.get('cell', '')}"
            # 副标题
            if stype == "input":
                sub = f"格子 {step.get('cell', '')}"
            elif stype in APP_OP_TYPES or stype == "keyevent":
                sub = "系统操作"
            else:
                x, y = step.get("x"), step.get("y")
                sub = f"格子 {step.get('cell', '')}" + (
                    f" · ({x}, {y})" if x is not None and y is not None else "")
            selected = (self._preview_index == i)
            card = tk.Frame(self._normal_inner, bg=THEME["bg2"],
                            highlightthickness=1,
                            highlightbackground=THEME["accent"] if selected else THEME["border"])
            card.pack(fill="x", pady=(0, 6))
            card.columnconfigure(1, weight=1)  # 主体列自动伸缩
            # 编号徽章(圆角)
            num = _RoundedBadge(card, text=str(i + 1), size=22, radius=5)
            num.grid(row=0, column=0, rowspan=2, padx=(6, 8), pady=8, sticky="w")
            # 主体
            body = tk.Frame(card, bg=THEME["bg2"])
            body.grid(row=0, column=1, rowspan=2, sticky="ew", pady=7)
            tk.Label(body, text=ttl, bg=THEME["bg2"], fg=THEME["txt"],
                     font=FONT_UI_BOLD, anchor="w").pack(fill="x")
            tk.Label(body, text=sub, bg=THEME["bg2"], fg=THEME["txt2"],
                     font=FONT_MONO, anchor="w").pack(fill="x", pady=(1, 0))
            # 操作图标按钮(上移/下移/删除):24x24 圆角小方块
            acts = tk.Frame(card, bg=THEME["bg2"])
            acts.grid(row=0, column=2, rowspan=2, padx=(4, 6), pady=6, sticky="e")
            for icon, cmd in [
                ("△", lambda idx=i: self._move(-1)),
                ("▽", lambda idx=i: self._move(1)),
                ("✕", lambda idx=i: self._delete_step(idx)),
            ]:
                _RoundedButton(acts, text=icon, command=cmd,
                               width=24, height=24, radius=5).pack(side="left", padx=1)
            # 点击卡片选中(同步 listbox 选中态,触发预览)
            for w in (card, num, body):
                w.bind("<Button-1>", lambda _e, idx=i: self._select_card(idx))
                w.bind("<Double-Button-1>", lambda _e, idx=i: self._edit_step_waits(idx))
            self._normal_card_widgets.append(card)
