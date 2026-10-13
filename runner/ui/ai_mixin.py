"""
runner/ui/ai_mixin.py - StepApp AI 模式职责域(自 gui_steps.py 拆出,CR-08)
==========================================================================
负责: 右栏 AI 模式 Tab 构建、指令发送、定位管线(OCR+VLM+模板 融合)调用、
候选展示/切换/确认入库、画布叠加绘制与拖动微调。

线程纪律: 定位在后台线程执行,结果经 self._bridge.post() 回主线程;
except 块中先落地局部变量再进 lambda(闭包陷阱,SKILL.md 约定)。
"""

from __future__ import annotations

import os
import tempfile
import threading
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk

import cvio

from ui.constants import MARGIN, RAW_IMAGE
from ui.theme import THEME, FONT_BTN, FONT_CAPTION, FONT_MONO
from ui.widgets import _RoundedButton


class _AiMixin:
    """AI 模式定位/候选/叠加(混入 StepApp)。"""

    # ------------------------------------------------------------------
    # 候选卡片显隐
    # ------------------------------------------------------------------
    def _show_cand_card(self, visible: bool) -> None:
        """候选确认卡片整体显隐(信息 + 三个按钮同进同退)。

        AI Tab 是 grid 布局,卡片固定占用 row=2(对话区与输入行之间),
        用 grid()/grid_remove() 显隐,不与 pack 混用。
        """
        if visible:
            self._ai_cand_frame.grid()
        else:
            self._ai_cand_frame.grid_remove()

    def _set_cand_btns(self, confirm: bool, deny: bool, next_btn: bool) -> None:
        """候选卡片内按钮显隐(卡片已显示时调用)。"""
        for btn, flag in ((self._ai_confirm_btn, confirm),
                          (self._ai_deny_btn, deny),
                          (self._ai_next_btn, next_btn)):
            if flag:
                btn.pack(side="left", padx=4, pady=2)
            else:
                btn.pack_forget()

    # ------------------------------------------------------------------
    # Tab 切换联动
    # ------------------------------------------------------------------
    def _on_tab_changed(self, event=None) -> None:
        """Tab 切换:普通模式清除 AI 候选叠加;AI 模式重绘当前候选。"""
        idx = self._tab_idx
        if idx == 0:  # AI 模式
            if (self._ai_result is not None
                    and self._ai_result.top_candidates and not self._ai_busy):
                self._show_candidate(self._ai_cand_idx, silent=True)
        elif idx == 1:  # 普通模式
            self._clear_ai_overlay()

    # ------------------------------------------------------------------
    # AI 定位主链
    # ------------------------------------------------------------------
    _AI_MODE_MAP = {"全流程": "full", "仅OCR": "ocr_only", "仅VLM": "vlm_only"}

    def _ai_send(self) -> None:
        """用户发送指令 -> 新定位管线(OCR+VLM+模板 融合验证)。"""
        instruction = self._ai_input_var.get().strip()
        if not instruction:
            return
        if self._ai_busy:
            # 忙时不能静默丢弃:用户会以为"发了没反应"
            self._ai_chat_append(
                "AI", "⏳ 上一条指令还在定位中(约 10~30 秒),请等它返回再发下一条。")
            return
        self._ai_input_var.set("")
        self._ai_instruction = instruction  # 确认后随步骤保存为 desc

        # 清除上一次的候选预览
        self._show_cand_card(False)
        self._ai_cand_info_var.set("")
        self._clear_ai_overlay()
        self._ai_result = None
        self._ai_adjusted = False
        self._ai_drag_last = None

        self._ai_chat_append("用户", instruction)
        mode_label = self._ai_mode_var.get()
        self._ai_chat_append(
            "AI", f"[{mode_label}] 正在定位(意图解析→候选→融合→验证),约 10~30 秒…")
        self._ai_log(f"用户指令: {instruction} ({mode_label})")

        if not os.path.isfile(RAW_IMAGE):
            self._ai_chat_append("AI", "未找到截图,请先点击「⟳ 刷新截图」。")
            return

        self._ai_busy = True
        threading.Thread(
            target=self._ai_locate_worker,
            args=(instruction, mode_label),
            daemon=True,
        ).start()

    def _ai_locate_worker(self, instruction: str, mode_label: str) -> None:
        """后台线程:跑完整定位管线,结果回主线程展示。"""
        try:
            from locate.config import load_config
            from locate import pipeline as locate_pipeline

            image = cvio.imread(RAW_IMAGE)
            if image is None:
                raise RuntimeError(f"无法读取截图: {RAW_IMAGE}")
            cfg = load_config()
            cfg.mode = self._AI_MODE_MAP[mode_label]
            # "仅OCR"语义:完全不调 VLM(含验证),纯本地快速路径
            if cfg.mode == "ocr_only":
                cfg.verify.enabled = False
            result = locate_pipeline.locate(image, instruction, cfg)
            self._bridge.post(lambda: self._ai_locate_done(result))
        except Exception as exc:  # 管线内任何异常都回显,不崩 GUI
            # except 块结束后 exc 会被 Python 删除,lambda 延迟执行取不到,
            # 必须先落地为普通局部变量再捕获
            err_msg = str(exc)
            self._bridge.post(lambda m=err_msg: self._ai_log(f"定位异常: {m}"))
            self._bridge.post(lambda m=err_msg: self._ai_locate_error(m))

    def _ai_locate_error(self, msg: str) -> None:
        self._ai_busy = False
        self._ai_chat_append("AI", f"定位失败: {msg}")

    def _ai_locate_done(self, result) -> None:
        """管线返回 -> 展示第 1 个候选。任何异常都回显到对话区,绝不静默。"""
        try:
            self._show_locate_result(result)
        except Exception as exc:
            # 展示环节抛异常会让用户停在"正在定位..."后再无下文(看起来像无反应)
            self._ai_busy = False
            self._ai_chat_append("AI", f"结果处理异常: {exc}")
            self._ai_log(f"locate_done 异常: {exc}")

    def _show_locate_result(self, result) -> None:
        """候选展示主体(由 _ai_locate_done 调用,异常由其兜底)。"""
        self._ai_result = result
        self._ai_cand_idx = 0

        if not result.top_candidates:
            self._ai_chat_append(
                "AI", f"没找到匹配元素({result.reason or '无候选'})。"
                      "可换个说法,或直接在普通模式点格子。")
            self._ai_log(f"定位失败: {result.reason}")
            return

        if not result.ok:
            self._ai_chat_append(
                "AI", f"⚠ 候选均未通过验证({result.reason}),"
                      f"仍列出 {len(result.top_candidates)} 个候选供人工裁决。")
        self._show_candidate(0)

    # ------------------------------------------------------------------
    # 候选展示 / 切换 / 确认
    # ------------------------------------------------------------------
    def _show_candidate(self, idx: int, silent: bool = False) -> None:
        """在画布叠加显示第 idx 个融合候选的框+十字。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        self._ai_cand_idx = idx
        cand = result.top_candidates[idx]
        n = len(result.top_candidates)
        self._draw_ai_overlay(cand, [c for j, c in enumerate(result.top_candidates) if j != idx])

        x, y = int(cand.point[0]), int(cand.point[1])
        cell = self._cell_for_point(x, y)
        if self._ai_adjusted:
            ver = "🔧已手动微调"
        elif cand.verified:
            ver = "✓验证通过"
        elif cand.verify_reason:
            ver = "✗验证未过"
        else:
            ver = "未验证"
        sources = "+".join(sorted(set(cand.sources or [cand.source])))
        detail = "" if self._ai_adjusted else (cand.verify_reason or cand.text or "")
        info = (f"候选 {idx + 1}/{n}  格子 {cell}  像素({x},{y})\n"
                f"来源: {sources}  置信度: {cand.confidence:.2f}  {ver}\n"
                f"{detail}")
        self._ai_cand_info_var.set(info.strip())
        if not silent:
            self._ai_chat_append("AI", f"候选 {idx + 1}/{n}: 格子 {cell} ({x},{y}) "
                                       f"[{sources}] 置信度 {cand.confidence:.2f} {ver}"
                                       "(自动刷新已暂停,点「✓ 保存为步骤」确认,"
                                       "或「⟳ 刷新截图」丢弃)")
            self._ai_log(f"候选{idx + 1}/{n} {cell} ({x},{y}) {sources} "
                         f"conf={cand.confidence:.2f} {ver}")
        self._show_cand_card(True)
        self._set_cand_btns(True, True, n > 1)

    def _ai_cycle_candidate(self, delta: int) -> None:
        """切换到下一个/上一个候选。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        n = len(result.top_candidates)
        if n <= 1:
            # 只有一个候选时「✗ 不是」= 丢弃本次提案:
            # 否则候选一直挂着,自动刷新会一直处于暂停状态
            self._reset_ai_proposal_ui()
            self._ai_chat_append("AI", "好,已丢弃这个候选。可以重新输入指令,"
                                       "或切到普通模式点格子手动指定。")
            return
        self._ai_adjusted = False
        self._ai_drag_last = None
        self._show_candidate((self._ai_cand_idx + delta) % n)

    def _ai_confirm(self) -> None:
        """确认当前候选 -> 带像素坐标的 tap/input 步骤入库(指令文本存为 desc)。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        cand = result.top_candidates[self._ai_cand_idx]
        x, y = int(cand.point[0]), int(cand.point[1])
        cell = self._cell_for_point(x, y)

        # 意图:点击 or 输入(定位管线已解析,随 result 带出)
        intent = getattr(result, "intent", None)
        action = getattr(intent, "action", "tap") or "tap"
        input_text = (getattr(intent, "input_text", "") or "").strip()

        step = {
            "type": "input" if (action == "input" and input_text) else "tap",
            "cell": cell,
            "x": x, "y": y,  # 精确像素坐标,回放时优先于格子换算
            "delay_before": self._spin_value(self.before_var),
            "wait_after": self._spin_value(self.after_var),
            "before_image": "",
            "after_image": "",
            "desc": (self._ai_instruction or "").strip(),  # 用户指令文本,便于阅读脚本
            "confidence": round(cand.confidence, 2),  # AI 候选置信度,卡片显示用
            "sources": "+".join(sorted(set(cand.sources or [cand.source]))),
        }
        if step["type"] == "input":
            step["text"] = input_text
        before_rel = self._capture_anchor_now("before")
        if before_rel:
            step["before_image"] = before_rel
        self.steps.append(step)
        self._refresh_list()
        self._select_and_preview(len(self.steps) - 1)
        self._list_see_end()

        if step["type"] == "input":
            warn = ""
            if any(ord(ch) > 127 for ch in input_text):
                warn = "。⚠ 含非 ASCII 字符(如中文),adb input text 可能无法输入"
            self._ai_chat_append(
                "AI", f"已保存为脚本步骤 {len(self.steps)}: 在 {cell} ({x},{y}) "
                      f"输入 {input_text!r}{warn}。"
                      "可继续输入下一条指令,或到普通模式查看/运行脚本。")
            self._ai_log(f"确认 -> 步骤 {len(self.steps)}: input {cell} ({x},{y}) "
                         f"text={input_text!r}")
        else:
            self._ai_chat_append("AI", f"已保存为脚本步骤 {len(self.steps)}: "
                                       f"点击 {cell} ({x},{y})。"
                                       "可继续输入下一条指令,或到普通模式查看/运行脚本。")
            self._ai_log(f"确认 -> 步骤 {len(self.steps)}: {cell} ({x},{y})")
        self._reset_ai_proposal_ui()

    def _reset_ai_proposal_ui(self) -> None:
        """确认/切换截图后复位候选预览状态。"""
        self._clear_ai_overlay()
        self._ai_result = None
        self._ai_adjusted = False
        self._ai_drag_last = None
        self._ai_cand_idx = 0
        self._ai_cand_info_var.set("")
        self._show_cand_card(False)

    def _clear_ai_overlay(self) -> None:
        """画布恢复纯网格图。"""
        if getattr(self, "_ai_overlay_active", False):
            self._set_grid(self.grid_bgr, self.cell_size)
            self._ai_overlay_active = False

    # ------------------------------------------------------------------
    # 坐标换算与叠加绘制
    # ------------------------------------------------------------------
    def _cell_for_point(self, x: int, y: int) -> str:
        """原始截图像素坐标 -> 格子编号(显示用,坐标仍以 x/y 为准)。"""
        from grid import col_name
        raw_w = self.grid_bgr.shape[1] - 2 * MARGIN
        raw_h = self.grid_bgr.shape[0] - 2 * MARGIN
        cols = max(1, int(np.ceil(raw_w / self.cell_size)))
        rows = max(1, int(np.ceil(raw_h / self.cell_size)))
        col = max(0, min(int(x // self.cell_size), cols - 1))
        row = max(0, min(int(y // self.cell_size), rows - 1))
        return f"{col_name(col)}{row + 1}"

    def _draw_ai_overlay(self, cand, others) -> None:
        """在网格图副本上画候选框(坐标是原始截图坐标,需加 MARGIN 边距)。"""
        img = self.grid_bgr.copy()

        def _shift(bbox):
            x1, y1, x2, y2 = [int(v) for v in bbox]
            return x1 + MARGIN, y1 + MARGIN, x2 + MARGIN, y2 + MARGIN

        # 其他候选:细品红框
        for c in others:
            cv2.rectangle(img, _shift(c.bbox)[:2], _shift(c.bbox)[2:],
                          (200, 0, 200), 1)
        # 当前候选:绿(验证通过)/橙(未过)粗框 + 十字
        color = (0, 200, 0) if cand.verified else (0, 165, 255)
        x1, y1, x2, y2 = _shift(cand.bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 3)
        cx, cy = int(cand.point[0]) + MARGIN, int(cand.point[1]) + MARGIN
        cv2.drawMarker(img, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 24, 2)
        # ASCII 标签(cv2 不渲染中文):格子号+置信度+来源
        x0, y0 = int(cand.point[0]), int(cand.point[1])
        label = f"{self._cell_for_point(x0, y0)} {cand.confidence:.2f}"
        cv2.rectangle(img, (x1, max(0, y1 - 18)), (x1 + 130, y1), color, -1)
        cv2.putText(img, label, (x1 + 4, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

        disp = cv2.resize(img, (self.disp_w, self.disp_h),
                          interpolation=cv2.INTER_AREA)
        self._tmp = os.path.join(tempfile.gettempdir(),
                                 "grapemobile_ai_preview.png")
        cv2.imwrite(self._tmp, disp)
        self.photo = tk.PhotoImage(file=self._tmp)
        self.canvas.configure(width=self.disp_w, height=self.disp_h,
                              scrollregion=(0, 0, self.disp_w, self.disp_h))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self._ai_overlay_active = True

    # ---- 候选拖动微调 ----
    def _ai_adjust_candidate(self, raw: tuple, jump: bool) -> None:
        """拖动/点击微调当前候选:jump=点击时框中心跳到光标;否则按增量平移。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        cand = result.top_candidates[self._ai_cand_idx]
        x1, y1, x2, y2 = [int(v) for v in cand.bbox]
        raw_w = self.grid_bgr.shape[1] - 2 * MARGIN
        raw_h = self.grid_bgr.shape[0] - 2 * MARGIN
        if jump:
            # 保持框大小,中心移到光标
            bw, bh = x2 - x1, y2 - y1
            nx = max(0, min(raw[0], raw_w - 1))
            ny = max(0, min(raw[1], raw_h - 1))
            nx1 = max(0, min(nx - bw // 2, raw_w - bw))
            ny1 = max(0, min(ny - bh // 2, raw_h - bh))
            cand.bbox = (nx1, ny1, nx1 + bw, ny1 + bh)
            cand.point = (nx, ny)
        else:
            if self._ai_drag_last is None:
                self._ai_drag_last = raw
                return
            dx, dy = raw[0] - self._ai_drag_last[0], raw[1] - self._ai_drag_last[1]
            if dx == 0 and dy == 0:
                return
            # 平移并夹取,框碰壁时贴边(点同步夹取)
            nx1 = max(0, min(x1 + dx, raw_w - (x2 - x1)))
            ny1 = max(0, min(y1 + dy, raw_h - (y2 - y1)))
            cand.bbox = (nx1, ny1, nx1 + (x2 - x1), ny1 + (y2 - y1))
            px = max(0, min(cand.point[0] + (nx1 - x1), raw_w - 1))
            py = max(0, min(cand.point[1] + (ny1 - y1), raw_h - 1))
            cand.point = (px, py)
        self._ai_drag_last = raw
        self._ai_adjusted = True
        # 微调后不再代表模型原始验证结论
        cand.verified = False
        cand.verify_reason = "用户手动微调"
        self._show_candidate(self._ai_cand_idx, silent=True)

    # ------------------------------------------------------------------
    # UI 构建:右栏 AI 模式 Tab
    # ------------------------------------------------------------------
    def _build_ai_tab(self, ai_tab: tk.Frame) -> None:
        """在 AI 模式面板内构建思考过程区/候选卡片/输入行/执行日志。"""
        ai_tab.rowconfigure(1, weight=3)   # 思考过程区:主要弹性,跟随窗口拉高
        ai_tab.rowconfigure(5, weight=1)   # 执行日志区:次要弹性
        ai_tab.columnconfigure(0, weight=1)
        ttk.Label(ai_tab, text="AI 思考过程", style="Caption.TLabel").grid(
            row=0, column=0, sticky="w")
        # 顶部思考过程区:aiclient 请求/回复 trace + 对话流(用户/AI/系统)并入同一流
        thinkbox = ttk.Frame(ai_tab)
        thinkbox.grid(row=1, column=0, sticky="nsew", pady=(2, 4))
        thinkbox.columnconfigure(0, weight=1)
        thinkbox.rowconfigure(0, weight=1)
        self._ai_think_text = tk.Text(thinkbox, height=8, font=FONT_MONO,
                                      state="disabled", bg=THEME["term_bg"],
                                      fg=THEME["term_fg"], wrap="word",
                                      relief="flat", bd=0)
        self._ai_think_text.grid(row=0, column=0, sticky="nsew")
        think_sb = ttk.Scrollbar(thinkbox, orient="vertical",
                                 command=self._ai_think_text.yview)
        think_sb.grid(row=0, column=1, sticky="ns")
        self._ai_think_text.configure(yscrollcommand=think_sb.set)
        # 候选卡片
        self._ai_cand_frame = tk.Frame(ai_tab, bg=THEME["surface"])
        self._ai_cand_info_var = tk.StringVar(value="")
        tk.Label(self._ai_cand_frame, textvariable=self._ai_cand_info_var,
                 bg=THEME["surface"], fg=THEME["txt"], font=FONT_MONO,
                 justify="left", anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        cand_btns = tk.Frame(self._ai_cand_frame, bg=THEME["surface"])
        cand_btns.pack(fill="x", padx=6, pady=(0, 4))
        self._ai_confirm_btn = _RoundedButton(cand_btns, text="保存为步骤",
                                              command=self._ai_confirm, style="primary")
        self._ai_confirm_btn.pack(side="left", padx=2)
        self._ai_deny_btn = _RoundedButton(cand_btns, text="不是",
                                           command=lambda: self._ai_cycle_candidate(1))
        self._ai_deny_btn.pack(side="left", padx=2)
        self._ai_next_btn = _RoundedButton(cand_btns, text="换候选",
                                           command=lambda: self._ai_cycle_candidate(1))
        self._ai_next_btn.pack(side="left", padx=2)
        # 候选卡片占位行:默认隐藏(_show_cand_card 控制 grid/grid_remove)。
        # AI Tab 是 grid 布局,卡片不能用 pack 插入(同一容器混用会抛 TclError)
        self._ai_cand_frame.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        self._ai_cand_frame.grid_remove()
        # 输入行
        self._ai_input_row = tk.Frame(ai_tab, bg=THEME["surface"])
        self._ai_input_row.grid(row=3, column=0, sticky="ew", pady=(2, 2))
        self._ai_input_row.columnconfigure(0, weight=1)
        self._ai_input_var = tk.StringVar()
        self._ai_entry = tk.Entry(self._ai_input_row, textvariable=self._ai_input_var,
                                  bg=THEME["surface2"], fg=THEME["txt"], font=FONT_BTN,
                                  bd=0, relief="flat", insertbackground=THEME["txt"],
                                  highlightthickness=1,
                                  highlightbackground=THEME["border2"],
                                  highlightcolor=THEME["border2"])
        self._ai_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=8)
        self._ai_entry.bind("<Return>", lambda e: self._ai_send())
        self._ai_mode_var = tk.StringVar(value="全流程")
        ttk.Combobox(self._ai_input_row, textvariable=self._ai_mode_var,
                     values=["全流程", "仅OCR", "仅VLM"], width=8,
                     state="readonly").grid(row=0, column=1, padx=(0, 4))
        _RoundedButton(self._ai_input_row, text="发送", command=self._ai_send,
                       style="primary").grid(row=0, column=2)
        # 执行日志(镜像主 Console 的 _log 输出)
        ttk.Label(ai_tab, text="执行日志", style="Caption.TLabel").grid(
            row=4, column=0, sticky="w", pady=(6, 2))
        aibox = ttk.Frame(ai_tab)
        aibox.grid(row=5, column=0, sticky="nsew")
        aibox.columnconfigure(0, weight=1)
        aibox.rowconfigure(0, weight=1)
        self._ai_log_text = tk.Text(aibox, height=6, font=FONT_MONO, state="disabled",
                                    bg=THEME["term_bg"], fg=THEME["term_fg"],
                                    wrap="word", relief="flat", bd=0)
        self._ai_log_text.grid(row=0, column=0, sticky="nsew")
        ai_ls = ttk.Scrollbar(aibox, orient="vertical", command=self._ai_log_text.yview)
        ai_ls.grid(row=0, column=1, sticky="ns")
        self._ai_log_text.configure(yscrollcommand=ai_ls.set)
