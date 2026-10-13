"""
runner/ui/canvas_mixin.py - StepApp 设备画面职责域(自 gui_steps.py 拆出,CR-08)
==========================================================================
负责: 左栏设备画面面板构建、网格图生产(设备实时截图/静态回退)、
画布渲染与缩放、点击坐标换算、锚点标准图截图、before/after 预览。

线程纪律: _produce_grid_from_device 为纯数据生产(可后台线程调用),
其余方法均只在主线程执行; 后台结果经 self._bridge.post() 回主线程。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
import tkinter as tk
from tkinter import messagebox, ttk

import cvio
from grid import GridMarker, col_name
from steps import APP_OP_TYPES, StepError
from adbtools import AdbClient, AdbError

from ui.constants import (
    DEFAULT_CELL_SIZE, EXPECT_IMG_H, EXPECT_IMG_W, GRID_IMAGE, GRID_META,
    MARGIN, MAX_DISPLAY_HEIGHT, MAX_DISPLAY_WIDTH, RAW_IMAGE, SCRIPTS_DIR,
)
from ui.theme import THEME, FONT_CAPTION
from ui.widgets import _RoundedButton


class _CanvasMixin:
    """设备画面/网格/画布交互/锚点截图(混入 StepApp)。"""

    # ------------------------------------------------------------------
    # 自动刷新(伪实时)
    # ------------------------------------------------------------------
    def _auto_tick(self) -> None:
        """每秒触发一次:条件满足时启动后台截图线程。

        暂停原因变化时写一条 Console 日志,让"画面为什么不动了"可诊断。
        """
        if not self.winfo_exists():
            return
        reason = ""
        if not self._auto_var.get():
            reason = "开关已关闭"
        elif self._running:
            reason = "脚本运行中"
        elif not self.device_id:
            reason = "未连接设备"
        elif self._ai_busy:
            reason = "AI 定位中"
        elif self._ai_overlay_active:
            reason = "AI 候选待确认(点「✓ 保存为步骤」或「⟳ 刷新截图」恢复)"
        if reason != getattr(self, "_auto_pause_reason", ""):
            if reason:
                self._log(f"[自动刷新] 暂停: {reason}")
            self._auto_pause_reason = reason
        if (not reason and not self._auto_busy):
            self._auto_busy = True
            threading.Thread(target=self._auto_worker, daemon=True).start()
        self.after(1000, self._auto_tick)

    def _auto_worker(self) -> None:
        """后台线程:只做 adb 截图与网格生成,不触碰 UI。"""
        error, payload = None, None
        try:
            payload = self._produce_grid_from_device()
        except Exception as exc:
            error = exc
        self._bridge.post(lambda: self._auto_apply(error, payload))

    def _auto_apply(self, error, payload) -> None:
        """主线程:应用自动刷新结果。"""
        self._auto_busy = False
        # 截图期间用户点了"运行":丢弃本次刷新,避免半写入的 screenshot.png 影响执行,
        # 也避免执行过程中画面被替换
        if self._running:
            return
        if error is not None:
            if not self._auto_fail_logged:
                self._log(f"[自动刷新] 失败: {error}")
                self._auto_fail_logged = True
            return
        self._auto_fail_logged = False
        device_id, grid, cell_size, raw = payload
        self.device_id = device_id
        self._latest_raw = raw
        self._set_grid(grid, cell_size)

    # ------------------------------------------------------------------
    # 网格图来源:设备实时截图 / 静态文件
    # ------------------------------------------------------------------
    def _produce_grid_from_device(self):
        """
        连接设备 -> 实时截图 -> 生成网格图(并落盘保持与 CLI 产物一致)。
        纯数据生产,不触碰任何 UI 组件,可在后台线程调用。

        Returns:
            (device_id, grid_bgr, cell_size, raw_bgr)

        Raises:
            StepError / AdbError: 未选择设备、设备离线或截图失败时抛出。
        """
        device_id = self.device_id
        if not device_id:
            raise StepError("尚未选择设备,请重新启动并在设备列表中选择一台在线设备")

        client = AdbClient()
        # 与 executor.AdbExecutor 一致:含 ":" 视为网络设备走 adb connect,
        # 模拟器/USB 序列号走 attach 在线校验
        if ":" in device_id:
            client.connect(device_id)
        else:
            client.attach(device_id)
        image_path = client.screenshot()  # 保存到 runner/tmp/image/screenshot.png
        img = cv2.imread(image_path)
        if img is None:
            raise StepError(f"截图读取失败: {image_path}")

        cell_size = self._read_cell_size()
        marker = GridMarker(cell_size=cell_size)
        grid = marker.draw(img)
        cv2.imwrite(GRID_IMAGE, grid)
        # 同步 sidecar 元数据,供 grid.py CLI 查询使用
        cols, rows = marker.grid_dims(img.shape)
        with open(GRID_META, "w", encoding="utf-8") as f:
            json.dump({"cell_size": cell_size,
                       "image_width": int(img.shape[1]),
                       "image_height": int(img.shape[0]),
                       "cols": cols, "rows": rows}, f, ensure_ascii=False, indent=2)
        return device_id, grid, cell_size, img

    def _capture_from_device(self) -> None:
        """生产网格并应用到界面(主线程调用)。"""
        device_id, grid, cell_size, raw = self._produce_grid_from_device()
        self.device_id = device_id
        self._latest_raw = raw
        self._set_grid(grid, cell_size)

    def _read_cell_size(self) -> int:
        """优先沿用现有网格元数据中的格子尺寸,否则用默认值。"""
        if os.path.isfile(GRID_META):
            try:
                with open(GRID_META, "r", encoding="utf-8") as f:
                    return int(json.load(f)["cell_size"])
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
        return DEFAULT_CELL_SIZE

    def _set_grid(self, grid_bgr, cell_size: int) -> None:
        """设置当前网格图并重算显示参数(画布存在时同步刷新)。"""
        prev = getattr(self, "grid_bgr", None)
        self.grid_bgr = grid_bgr
        self.cell_size = cell_size
        gh, gw = grid_bgr.shape[:2]
        # 网格尺寸未变时保留用户当前缩放:
        # 否则自动刷新每秒调本方法,会把用户放大后的缩放打回适配值(缩放形同虚设)
        if (prev is None or prev.shape != grid_bgr.shape
                or not getattr(self, "scale", None)):
            self.scale = min(1.0, MAX_DISPLAY_WIDTH / gw, MAX_DISPLAY_HEIGHT / gh)
        self.disp_w, self.disp_h = int(gw * self.scale), int(gh * self.scale)
        # Tkinter PhotoImage 不支持任意比例缩放,先用 cv2 缩放后写临时文件再加载
        disp = cv2.resize(grid_bgr, (self.disp_w, self.disp_h), interpolation=cv2.INTER_AREA)
        self._tmp = os.path.join(tempfile.gettempdir(), "grapemobile_grid_display.png")
        cv2.imwrite(self._tmp, disp)
        new_photo = tk.PhotoImage(file=self._tmp)
        old_photo = getattr(self, "photo", None)
        self.photo = new_photo
        self._photo_ref = old_photo  # 保持引用,避免旧图被 GC 导致画布闪黑
        if hasattr(self, "canvas"):
            self.canvas.configure(width=self.disp_w, height=self.disp_h,
                                  scrollregion=(0, 0, self.disp_w, self.disp_h))
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.title(f"GrapeMobile 步骤编排器 - {self.device_id or '静态网格图'}")
        # 新网格到达后,旧候选的像素坐标失效,复位 AI 预览态
        if hasattr(self, "_ai_overlay_active"):
            self._ai_overlay_active = False
            self._ai_result = None
            self._ai_cand_idx = 0
            self._ai_adjusted = False
            self._ai_drag_last = None
            if hasattr(self, "_ai_confirm_btn"):
                self._show_cand_card(False)
                self._ai_cand_info_var.set("")

    # ------------------------------------------------------------------
    # 左栏 UI 构建:设备画面(画布 + 工具条 + 提示)
    # ------------------------------------------------------------------
    def _build_left_panel(self, left: tk.Frame) -> None:
        """在左栏容器内构建设备画面画布与工具条。"""
        tk.Label(left, text="设备画面", fg=THEME["txt3"], bg=THEME["bg"],
                 font=FONT_CAPTION).pack(anchor="w", pady=(0, 6))
        # 画布 + 滚动条:放大(>适配比例)后可平移查看完整画面,
        # 点击坐标统一走 canvasx/canvasy(视口坐标 -> 画布坐标)
        cwrap = tk.Frame(left, bg=THEME["bg"])
        cwrap.pack()
        cwrap.grid_rowconfigure(0, weight=1)
        cwrap.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(cwrap, width=self.disp_w, height=self.disp_h,
                                highlightthickness=1,
                                highlightbackground=THEME["border2"], bg=THEME["bg2"])
        self.canvas.grid(row=0, column=0, sticky="nw")
        cvs = ttk.Scrollbar(cwrap, orient="vertical", command=self.canvas.yview)
        cvs.grid(row=0, column=1, sticky="ns")
        chs = ttk.Scrollbar(cwrap, orient="horizontal", command=self.canvas.xview)
        chs.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.canvas.configure(yscrollcommand=cvs.set, xscrollcommand=chs.set)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        # 滚轮竖向平移(Windows: delta 为 120 的倍数)
        self.canvas.bind("<MouseWheel>",
                         lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        # 工具条
        tools = tk.Frame(left, bg=THEME["bg"])
        tools.pack(fill="x", pady=(8, 0))
        self._grid_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(tools, text="网格", variable=self._grid_on,
                        command=self._toggle_grid).pack(side="left", padx=(0, 6))
        # 伪实时开关:每秒后台重截设备画面(AI 定位/运行中会自动暂停)
        self._auto_var = getattr(self, "_auto_var", tk.BooleanVar(value=True))
        ttk.Checkbutton(tools, text="自动刷新", variable=self._auto_var).pack(
            side="left", padx=(0, 6))
        _RoundedButton(tools, text="－", width=28,
                       command=lambda: self._zoom(1 / 1.25)).pack(side="left", padx=1)
        self._zoom_label = tk.Label(tools, text="100%", width=5, anchor="center",
                                    bg=THEME["bg"], fg=THEME["txt2"], font=FONT_CAPTION)
        self._zoom_label.pack(side="left", padx=1)
        _RoundedButton(tools, text="＋", width=28,
                       command=lambda: self._zoom(1.25)).pack(side="left", padx=1)
        _RoundedButton(tools, text="1:1", width=36,
                       command=self._zoom_reset).pack(side="left", padx=(6, 1))
        tk.Label(left, text="点画面任意处 = 追加一步点击(自动定位格子)",
                 fg=THEME["txt3"], bg=THEME["bg"], font=FONT_CAPTION,
                 wraplength=280, justify="left").pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------------
    # 画布显示:网格显隐 / 缩放
    # ------------------------------------------------------------------
    def _toggle_grid(self) -> None:
        """网格显隐:关掉时显示无网格的原始截图(点击换算不受影响)。"""
        if self._grid_on.get():
            self._render_canvas(self.grid_bgr)
        elif self._latest_raw is not None:
            self._render_canvas(self._latest_raw)
        else:
            self._log("[提示] 暂无无网格原图,保持当前显示")

    def _zoom(self, factor: float) -> None:
        """缩放画布显示。self.scale 同时用于点击坐标换算,故缩放后点击依旧准确。"""
        base_h, base_w = self.grid_bgr.shape[:2]
        self.scale = min(2.0, max(0.2, self.scale * factor))
        self.disp_w = max(1, int(base_w * self.scale))
        self.disp_h = max(1, int(base_h * self.scale))
        self._zoom_label.configure(text=f"{int(round(self.scale * 100))}%")
        # AI 候选叠加态下缩放:重画叠加层(否则叠加图残留在旧缩放,点击却按新缩放换算)
        if getattr(self, "_ai_overlay_active", False) and self._ai_result is not None:
            self._show_candidate(self._ai_cand_idx, silent=True)
        else:
            self._render_canvas(self.grid_bgr if self._grid_on.get() else self._latest_raw)

    def _zoom_reset(self) -> None:
        """回到 1:1(按当前网格图原始像素 1:1 显示,超出部分由滚动条平移查看)。"""
        self.scale = 1.0
        base_h, base_w = self.grid_bgr.shape[:2]
        self.disp_w = max(1, int(base_w * self.scale))
        self.disp_h = max(1, int(base_h * self.scale))
        self._zoom_label.configure(text="100%")
        if getattr(self, "_ai_overlay_active", False) and self._ai_result is not None:
            self._show_candidate(self._ai_cand_idx, silent=True)
        else:
            self._render_canvas(self.grid_bgr if self._grid_on.get() else self._latest_raw)

    def _render_canvas(self, bgr) -> None:
        """按当前 self.scale 把 bgr 渲染到画布(PhotoImage 不支持任意缩放,先落盘再加载)。

        注意:先 create_image 再赋 self.photo,并保留旧引用,
        否则旧 PhotoImage 被 GC 时 Tcl 侧图片同步销毁,画布会闪黑图(实测踩过)。
        """
        if bgr is None:
            return
        h, w = bgr.shape[:2]
        self.disp_w, self.disp_h = max(1, int(w * self.scale)), max(1, int(h * self.scale))
        disp = cv2.resize(bgr, (self.disp_w, self.disp_h), interpolation=cv2.INTER_AREA)
        self._tmp = os.path.join(tempfile.gettempdir(), "grapemobile_grid_display.png")
        cv2.imwrite(self._tmp, disp)
        new_photo = tk.PhotoImage(file=self._tmp)
        old_photo = getattr(self, "photo", None)
        self.canvas.configure(width=self.disp_w, height=self.disp_h,
                              scrollregion=(0, 0, self.disp_w, self.disp_h))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=new_photo)
        self.photo = new_photo
        self._photo_ref = old_photo  # 保持引用,避免画布闪黑

    # ------------------------------------------------------------------
    # 画布点击:格子换算 + 锚点截图
    # ------------------------------------------------------------------
    def _on_canvas_click(self, event: tk.Event) -> None:
        """画布点击 -> 换算为格子编号 -> 实时截"执行前图" -> 追加 tap 步骤。"""
        if self._running:
            return
        # AI 候选预览态:点击 = 把候选点跳到光标处(微调)
        if self._ai_overlay_active:
            self._ai_adjust_candidate(self._event_to_raw(event), jump=True)
            return
        # 视口坐标 -> 画布坐标(画布可滚动,事件坐标不含滚动偏移)
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        orig_x = cx / self.scale - MARGIN
        orig_y = cy / self.scale - MARGIN
        meta_w = self.grid_bgr.shape[1] - 2 * MARGIN
        meta_h = self.grid_bgr.shape[0] - 2 * MARGIN
        if not (0 <= orig_x < meta_w and 0 <= orig_y < meta_h):
            return  # 点在边距标签区,忽略
        col = int(orig_x // self.cell_size)
        row = int(orig_y // self.cell_size)
        cell = f"{col_name(col)}{row + 1}"
        # 记录格子中心像素坐标,卡片显示用;回放时 steps 优先于格子换算
        tap_x = int(col * self.cell_size + self.cell_size // 2)
        tap_y = int(row * self.cell_size + self.cell_size // 2)
        step = {
            "type": "tap", "cell": cell,
            "x": tap_x, "y": tap_y,
            "delay_before": self._spin_value(self.before_var),
            "wait_after": self._spin_value(self.after_var),
            "before_image": "",
            "after_image": "",
        }
        # 关键:点击瞬间立即从设备实时截图作为"执行前标准图",
        # 不用自动刷新缓存(缓存最坏滞后 1 秒,可能录到跳转中间帧)
        before_rel = self._capture_anchor_now("before")
        if before_rel:
            step["before_image"] = before_rel
        else:
            self._log("[提示] 未能实时截图,本步无执行前标准图(执行时跳过开始条件校验)")
        self.steps.append(step)
        self._refresh_list()
        self._select_and_preview(len(self.steps) - 1)
        self._list_see_end()

    def _anchor_dir(self) -> str:
        """当前脚本的标准图目录:scripts/<脚本名>/images/。"""
        return os.path.join(SCRIPTS_DIR, self.script_name, "images")

    def _max_existing_anchor_seq(self, prefix: str) -> int:
        """扫描 images/ 中指定前缀(before_/after_)图片的最大编号,无则 0。"""
        anchor_dir = self._anchor_dir()
        if not os.path.isdir(anchor_dir):
            return 0
        seq = 0
        for n in os.listdir(anchor_dir):
            stem = os.path.splitext(n)[0]
            if stem.startswith(prefix):
                tail = stem[len(prefix):]
                if tail.isdigit():
                    seq = max(seq, int(tail))
        return seq

    def _capture_anchor_now(self, kind: str) -> str:
        """
        立即从设备实时截图,存为当前脚本的标准图。

        Args:
            kind: "before" -> images/before_NNNN.png;
                  "after"  -> images/after_NNNN.png。

        Returns:
            相对脚本 JSON 目录的路径(如 "images/before_0001.png");失败返回 ""。
            设备不可用(静态图模式)时 before 回退为最近缓存/screenshot.png,
            after 不允许回退(执行后图必须是真实跳转后的画面)。
        """
        anchor_dir = self._anchor_dir()
        os.makedirs(anchor_dir, exist_ok=True)
        if kind == "before":
            self._before_seq = self._max_existing_anchor_seq("before_")
        else:
            self._after_seq = self._max_existing_anchor_seq("after_")
        seq_attr = "_before_seq" if kind == "before" else "_after_seq"
        setattr(self, seq_attr, getattr(self, seq_attr) + 1)
        name = f"{kind}_{getattr(self, seq_attr):04d}.png"
        final_path = os.path.join(anchor_dir, name)

        captured = False
        if self.device_id:
            try:
                self.config(cursor="watch")
                self.update_idletasks()
                if self._anchor_client is None:
                    self._anchor_client = AdbClient()
                self._anchor_client.attach(self.device_id)
                self._anchor_client.screenshot(save_path=final_path)
                captured = os.path.isfile(final_path) and os.path.getsize(final_path) > 0
            except AdbError as exc:
                self._log(f"[提示] 实时截图失败: {exc}")
            finally:
                self.config(cursor="")

        if not captured and kind == "before":
            # 静态模式回退:用最近缓存帧(无设备场景仍可手工编排)
            raw = self._latest_raw
            if raw is None and os.path.isfile(RAW_IMAGE):
                raw = cvio.imread(RAW_IMAGE)
            if raw is not None:
                captured = cvio.imwrite(final_path, raw)

        if not captured:
            setattr(self, seq_attr, getattr(self, seq_attr) - 1)
            return ""
        return f"images/{name}"

    def _recapture_anchor(self, field: str) -> None:
        """
        为当前选中步骤重拍标准图(before_image / after_image)。

        典型用途:页面跳转稳定后选中上一步,点"📷 截执行后图"标记完成条件;
        或录制时机不对时重拍执行前图。
        """
        if self._running:
            return
        sel = self._list_sel()
        if len(sel) != 1:
            messagebox.showinfo("请选择步骤", "请先在步骤列表中选中一个步骤。")
            return
        kind = "before" if field == "before_image" else "after"
        rel = self._capture_anchor_now(kind)
        if rel:
            self.steps[sel[0]][field] = rel
            self._refresh_list()
            self._select_and_preview(sel[0])
            self._log(f"第 {sel[0] + 1} 步 {self.steps[sel[0]]['cell']} "
                      f"{'执行前' if kind == 'before' else '执行后'}标准图已更新: {rel}")
        else:
            messagebox.showwarning("截图失败", "未能从设备截图,请确认设备连接正常。")

    # ------------------------------------------------------------------
    # 标准图预览(before/after)
    # ------------------------------------------------------------------
    def _make_placeholder_image(self) -> "tk.PhotoImage":
        """生成深灰占位图(缓存,避免每次选中都写盘+创建 PhotoImage)。"""
        if hasattr(self, "_placeholder_photo") and self._placeholder_photo is not None:
            return self._placeholder_photo
        img = np.full((EXPECT_IMG_H, EXPECT_IMG_W, 3), 40, dtype=np.uint8)
        tmp = os.path.join(tempfile.gettempdir(), "grapemobile_expect_placeholder.png")
        cv2.imwrite(tmp, img)
        self._placeholder_photo = tk.PhotoImage(file=tmp)
        return self._placeholder_photo

    def _imread_cached(self, path: str):
        """带 LRU 缓存的读图:同一张图只读盘+解码一次,后续命中内存缓存。"""
        if path in self._img_cache:
            self._img_cache.move_to_end(path)
            return self._img_cache[path]
        img = cvio.imread(path)
        if img is not None:
            self._img_cache[path] = img
            if len(self._img_cache) > self._CACHE_LIMIT:
                self._img_cache.popitem(last=False)
        return img

    def _set_preview_image(self, which: str, img: Optional[np.ndarray],
                           cache_key: str = "") -> None:
        """把图像等比缩放居中贴入固定预览框(which: "before"/after")。

        用 PIL.ImageTk 从内存直接创建 PhotoImage,避免写盘+读盘的磁盘 I/O。
        """
        from PIL import Image, ImageTk
        photo_attr = f"_{which}_photo"
        label = self.before_img_label if which == "before" else self.after_img_label
        if img is None:
            photo = self._make_placeholder_image()
        elif cache_key and cache_key in self._preview_cache:
            self._preview_cache.move_to_end(cache_key)
            photo = self._preview_cache[cache_key]
        else:
            h, w = img.shape[:2]
            scale = min(EXPECT_IMG_W / w, EXPECT_IMG_H / h)
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            # BGR -> RGB + 从内存直接创建,不写临时文件
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb).resize((new_w, new_h), Image.LANCZOS)
            canvas = Image.new("RGB", (EXPECT_IMG_W, EXPECT_IMG_H), (40, 40, 40))
            canvas.paste(pil, ((EXPECT_IMG_W - new_w) // 2,
                               (EXPECT_IMG_H - new_h) // 2))
            photo = ImageTk.PhotoImage(canvas)
            if cache_key:
                self._preview_cache[cache_key] = photo
                if len(self._preview_cache) > self._CACHE_LIMIT:
                    self._preview_cache.popitem(last=False)
        setattr(self, photo_attr, photo)
        label.configure(image=photo)

    def _show_expect_preview(self, index: Optional[int]) -> None:
        """
        显示指定步骤的执行前/执行后标准图。

        执行前:本步 before_image;
        执行后:本步 after_image;缺省时提示"默认=下一步执行前图"(引擎自动链接)。
        """
        if index is None or not (0 <= index < len(self.steps)):
            self._preview_index = None
            self._detail_info_var.set("未选中步骤")
            self._set_preview_image("before", None)
            self._set_preview_image("after", None)
            self.before_status_var.set("未选中步骤")
            self.after_status_var.set("未选中步骤")
            return

        self._preview_index = index
        step = self.steps[index]
        stype = step.get("type")

        # ---- 非 UI 步骤:无格子/标准图,详情展示动作与包名/按键 ----
        if stype in APP_OP_TYPES or stype == "keyevent":
            title = f"第 {index + 1} 步"
            self._detail_info_var.set(f"{title}  {self._step_action_text(step)}")
            self._detail_before_var.set(f"{step.get('delay_before', 0):g}")
            self._detail_after_var.set(f"{step.get('wait_after', 0):g}")
            self._set_preview_image("before", None)
            self._set_preview_image("after", None)
            self.before_status_var.set(f"{title}\n系统操作,无标准图")
            self.after_status_var.set(f"{title}\n系统操作,无标准图")
            return

        title = f"第 {index + 1} 步 {step['cell']}"

        # ---- 步骤详情(可编辑前/后等待) ----
        is_input = stype == "input"
        kind = f"输入 \"{step.get('text', '')}\"" if is_input else "点击"
        coord = (f"({step['x']}, {step['y']})"
                 if step.get("x") is not None and step.get("y") is not None else "-")
        self._detail_info_var.set(
            f"第 {index + 1} 步  {kind}  {step['cell']}  {coord}")
        self._detail_before_var.set(f"{step.get('delay_before', 0):g}")
        self._detail_after_var.set(f"{step.get('wait_after', 0):g}")

        # 执行前
        before_rel = step.get("before_image", "")
        if not before_rel:
            self._set_preview_image("before", None)
            self.before_status_var.set(f"{title}\n无执行前标准图")
        else:
            before_path = before_rel if os.path.isabs(before_rel) \
                else os.path.join(self._current_script_dir(), before_rel)
            img = self._imread_cached(before_path)
            self._set_preview_image("before", img, cache_key=before_path)
            self.before_status_var.set(
                f"{title}\n{before_rel}" if img is not None
                else f"{title}\n执行前图缺失:\n{before_rel}")

        # 执行后
        after_rel = step.get("after_image", "")
        if after_rel:
            after_path = after_rel if os.path.isabs(after_rel) \
                else os.path.join(self._current_script_dir(), after_rel)
            img = self._imread_cached(after_path)
            self._set_preview_image("after", img, cache_key=after_path)
            self.after_status_var.set(
                f"{title}\n{after_rel}" if img is not None
                else f"{title}\n执行后图缺失:\n{after_rel}")
        else:
            # 缺省时引擎自动用"下一步执行前图"作为本步完成条件
            self._set_preview_image("after", None)
            if index + 1 < len(self.steps):
                nxt = self.steps[index + 1]
                self.after_status_var.set(
                    f"{title}\n默认=第 {index + 2} 步 {nxt['cell']} 的执行前图")
            else:
                self.after_status_var.set(f"{title}\n无执行后标准图(末步点完即结束)")

    # ------------------------------------------------------------------
    # 画布拖动坐标换算(AI 候选微调入口,行为在 ai_mixin)
    # ------------------------------------------------------------------
    def _on_canvas_drag(self, event: tk.Event) -> None:
        if self._running or not self._ai_overlay_active:
            return
        self._ai_adjust_candidate(self._event_to_raw(event), jump=False)

    def _event_to_raw(self, event: tk.Event) -> tuple:
        """画布事件坐标 -> 原始截图坐标(视口->画布->去缩放/边距),并夹取到画面内。"""
        raw_w = self.grid_bgr.shape[1] - 2 * MARGIN
        raw_h = self.grid_bgr.shape[0] - 2 * MARGIN
        x = int(round(self.canvas.canvasx(event.x) / self.scale - MARGIN))
        y = int(round(self.canvas.canvasy(event.y) / self.scale - MARGIN))
        return max(0, min(x, raw_w - 1)), max(0, min(y, raw_h - 1))
