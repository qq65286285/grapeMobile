"""
runner/ui/steps_mixin.py - StepApp 步骤编排职责域(自 gui_steps.py 拆出,CR-08)
==========================================================================
负责: 中栏"步骤·实时预览"面板与右栏普通模式 Tab 构建、步骤增删改、
列表(Treeview)/双栏卡片渲染与选中联动、等待编辑弹窗、脚本保存/加载
(含锚点文件夹迁移与孤儿图清理)。

性能约定(CR-03 修复遗产): 等待修改/选中变更只做原地更新,
严禁全量重建卡片列表。
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from steps import (
    APP_OP_TYPES, KEYEVENT_LABELS, KEYEVENT_MAP,
    StepError, load_steps, save_steps,
)

from ui.constants import (
    DRAFT_SCRIPT_NAME, EXPECT_IMG_H, EXPECT_IMG_W, SCRIPTS_DIR,
)
from ui.theme import (
    THEME,
    FONT_BTN, FONT_CAPTION, FONT_MONO,
)
from ui.widgets import _RoundedButton


class _StepsMixin:
    """步骤编排/列表/卡片/保存加载(混入 StepApp)。"""

    # ------------------------------------------------------------------
    # 系统操作步骤
    # ------------------------------------------------------------------
    # 系统操作选项:显示文案 -> (类型, 附加参数)
    _SYSOP_OPTIONS = (
        ("关闭 App(force-stop)", ("app_stop", "")),
        ("启动 App", ("app_start", "")),
        ("清除 App 数据(pm clear)", ("app_clear", "")),
        ("Home 键", ("keyevent", "home")),
        ("返回键", ("keyevent", "back")),
        ("最近任务键", ("keyevent", "recents")),
    )

    def _add_system_step(self) -> None:
        """弹窗选择系统操作(App 生命周期/系统按键)并追加为步骤。"""
        if self._running:
            return
        dlg = tk.Toplevel(self)
        dlg.title("添加系统操作")
        dlg.resizable(False, False)
        dlg.transient(self.winfo_toplevel())

        ttk.Label(dlg, text="操作:").grid(row=0, column=0, padx=(12, 4),
                                          pady=(12, 4), sticky="e")
        labels = [o[0] for o in self._SYSOP_OPTIONS]
        op_var = tk.StringVar(value=labels[0])
        ttk.Combobox(dlg, textvariable=op_var, values=labels,
                     state="readonly", width=24).grid(
            row=0, column=1, columnspan=2, padx=(0, 12), pady=(12, 4))

        pkg_frm = ttk.Frame(dlg)
        pkg_frm.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=4)
        ttk.Label(pkg_frm, text="包名(留空=执行时\n自动识别前台App):").pack(side="left")
        pkg_var = tk.StringVar()
        ttk.Entry(pkg_frm, textvariable=pkg_var, width=22).pack(
            side="left", padx=4)

        def _detect_pkg() -> None:
            if not self.device_id:
                messagebox.showinfo("无设备", "当前未连接设备,无法识别包名", parent=dlg)
                return
            try:
                from adbtools import AdbClient
                c = AdbClient()
                c.attach(self.device_id)
                pkg_var.set(c.current_package())
            except Exception as exc:
                messagebox.showerror("识别失败", str(exc), parent=dlg)

        ttk.Button(pkg_frm, text="识别当前", command=_detect_pkg).pack(side="left")

        # keyevent 选项隐藏包名行,app 操作显示
        def _sync_pkg_row(_event=None) -> None:
            choice = dict((o[0], o[1]) for o in self._SYSOP_OPTIONS)[op_var.get()]
            if choice[0] == "keyevent":
                pkg_frm.grid_remove()
            else:
                pkg_frm.grid()

        dlg.bind("<<ComboboxSelected>>", _sync_pkg_row)

        def _ok() -> None:
            stype, arg = dict((o[0], o[1]) for o in self._SYSOP_OPTIONS)[op_var.get()]
            if stype == "keyevent":
                step = {"type": "keyevent", "key": arg}
            else:
                step = {"type": stype, "package": pkg_var.get().strip()}
            step.update({
                "cell": "", "x": None, "y": None,
                "delay_before": 0,
                "wait_after": 2 if stype in APP_OP_TYPES else 0,
                "before_image": "", "after_image": "", "desc": "",
            })
            self.steps.append(step)
            self._refresh_list()
            self._select_and_preview(len(self.steps) - 1)
            self._list_see_end()
            self._log(f"[添加] {self._step_action_text(step)}")
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=2, column=0, columnspan=3, pady=(8, 12))
        ttk.Button(btns, text="添加", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda _e: _ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.grab_set()

    @staticmethod
    def _step_action_text(step: Dict[str, Any]) -> str:
        """步骤的人类可读动作文案(日志/列表共用)。"""
        stype = step.get("type")
        if stype == "keyevent":
            return f"系统按键 {KEYEVENT_LABELS.get(step.get('key'), step.get('key'))}"
        if stype in APP_OP_TYPES:
            action = {"app_stop": "关闭 App", "app_start": "启动 App",
                      "app_clear": "清除 App 数据"}[stype]
            pkg = step.get("package") or "自动识别前台 App"
            return f"{action}: {pkg}"
        return f"{stype} {step.get('cell')}"

    def _current_script_dir(self) -> str:
        """标准图基准目录:已保存脚本取其 JSON 所在目录;未保存草稿取 scripts/untitled/。"""
        if self.script_path:
            return os.path.dirname(os.path.abspath(self.script_path))
        return os.path.join(SCRIPTS_DIR, DRAFT_SCRIPT_NAME)

    # ------------------------------------------------------------------
    # 列表选择与等待编辑
    # ------------------------------------------------------------------
    @staticmethod
    def _spin_value(var: tk.StringVar) -> float:
        """读取 Spinbox 数值,非法时按 0 处理。"""
        try:
            return max(0.0, float(var.get()))
        except ValueError:
            return 0.0

    def _list_sel(self) -> List[int]:
        """当前选中步骤下标列表(Treeview 版 curselection)。"""
        return [int(iid) for iid in self.listbox.selection()]

    def _list_select(self, i: int) -> None:
        """程序化选中第 i 步并滚动到可见。"""
        self.listbox.selection_set(str(i))
        self.listbox.see(str(i))

    def _list_see_end(self) -> None:
        """滚动到列表末行(新增步骤后调用)。"""
        kids = self.listbox.get_children()
        if kids:
            self.listbox.see(kids[-1])

    def _on_select(self, _event: tk.Event) -> None:
        """Treeview 选中步骤:显示预期画面 + 同步卡片高亮(用户手动点 Treeview 时触发)。

        程序化选中(_select_and_preview/_preview_running_step)已自行处理预览,
        此处若目标步未变则直接返回,避免同一张图被重复加载。
        """
        if self._suppress_select:
            return
        sel = self._list_sel()
        if len(sel) != 1:
            return
        idx = sel[0]
        if idx == self._preview_index:
            return  # 目标步未变,无需重复加载/刷新
        old_idx = self._preview_index
        self._show_expect_preview(idx)
        self._update_card_selection(old_idx, idx)

    def _apply_step_detail(self) -> None:
        """把右侧详情面板中的前/后等待写回当前选中步骤。"""
        i = self._preview_index
        if i is None or not (0 <= i < len(self.steps)):
            messagebox.showinfo("未选中", "请先在列表中选中一个步骤")
            return
        # 强制焦点提交 Spinbox 正在编辑的值
        self.focus_set()
        self.update_idletasks()
        db = self._spin_value(self._detail_before_var)
        da = self._spin_value(self._detail_after_var)
        self.steps[i]["delay_before"] = db
        self.steps[i]["wait_after"] = da
        # 等待时间只影响列表/卡片上的文案,不做全量重建:
        # 1) 原地更新列表行 meta 列; 2) 卡片副标题无需等待,仅选中卡片高亮不变
        self._update_list_row_waits(i)
        self._log(f"[修改] 第 {i + 1} 步等待: 前 {db:g}s / 后 {da:g}s")

    def _on_step_double_click(self, event: tk.Event) -> None:
        """双击列表行:弹窗直接修改该步骤的前/后等待时间。"""
        # 以双击位置为准确定行(双击时选中事件可能尚未更新)
        iid = self.listbox.identify_row(event.y)
        if not iid:
            return
        i = int(iid)
        if not (0 <= i < len(self.steps)):
            return
        self.listbox.selection_set(iid)
        self._edit_step_waits(i)

    def _edit_step_waits(self, i: int) -> None:
        """小弹窗编辑第 i 步:系统操作步骤可改包名/按键,所有步骤可改前/后等待。"""
        step = self.steps[i]
        stype = step.get("type")
        is_sysop = stype in APP_OP_TYPES or stype == "keyevent"
        dlg = tk.Toplevel(self)
        dlg.title(f"第 {i + 1} 步 - {self._step_action_text(step)}")
        dlg.resizable(False, False)
        dlg.transient(self.winfo_toplevel())

        row = 0
        if is_sysop:
            if stype == "keyevent":
                ttk.Label(dlg, text="按键:").grid(row=row, column=0, padx=10,
                                                   pady=(12, 4), sticky="e")
                key_var = tk.StringVar(value=step.get("key", "home"))
                ttk.Combobox(dlg, textvariable=key_var,
                             values=list(KEYEVENT_MAP), state="readonly",
                             width=10).grid(row=row, column=1, padx=10,
                                            pady=(12, 4), sticky="w")
            else:
                ttk.Label(dlg, text="包名(留空=\n自动识别):").grid(
                    row=row, column=0, padx=10, pady=(12, 4), sticky="e")
                pkg_var = tk.StringVar(value=step.get("package", ""))
                ttk.Entry(dlg, textvariable=pkg_var, width=20).grid(
                    row=row, column=1, padx=10, pady=(12, 4), sticky="w")
            row += 1

        ttk.Label(dlg, text="执行前等待(秒):").grid(
            row=row, column=0, padx=10, pady=(12, 4) if row == 0 else 4,
            sticky="e")
        before_var = tk.StringVar(value=f"{step.get('delay_before', 0):g}")
        ttk.Spinbox(dlg, from_=0, to=600, width=8, textvariable=before_var).grid(
            row=row, column=1, padx=10, pady=(12, 4) if row == 0 else 4,
            sticky="w")
        row += 1
        ttk.Label(dlg, text="执行后等待(秒):").grid(row=row, column=0, padx=10,
                                                    pady=4, sticky="e")
        after_var = tk.StringVar(value=f"{step.get('wait_after', 0):g}")
        ttk.Spinbox(dlg, from_=0, to=600, width=8, textvariable=after_var).grid(
            row=row, column=1, padx=10, pady=4, sticky="w")
        row += 1

        btns = ttk.Frame(dlg)
        btns.grid(row=row, column=0, columnspan=2, pady=(8, 12))

        def _ok() -> None:
            # 强制焦点提交 Spinbox 正在编辑的值
            dlg.focus_set()
            dlg.update_idletasks()
            if is_sysop:
                if stype == "keyevent":
                    step["key"] = key_var.get()
                else:
                    step["package"] = pkg_var.get().strip()
            step["delay_before"] = self._spin_value(before_var)
            step["wait_after"] = self._spin_value(after_var)
            # 仅原地更新该行列表/卡片文案,不做全量重建;
            # 走 _select_and_preview 统一保护路径(_suppress_select + 卡片高亮)
            self._update_list_row_waits(i)
            old_idx = self._preview_index
            self._select_and_preview(i)
            self._update_card_selection(old_idx, i)
            self._log(f"[修改] 第 {i + 1} 步 {self._step_action_text(step)}:"
                      f" 前 {step['delay_before']:g}s / 后 {step['wait_after']:g}s")
            dlg.destroy()

        ttk.Button(btns, text="确定", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda _e: _ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.grab_set()  # 模态

    # ------------------------------------------------------------------
    # 选中联动 / 卡片高亮(不重建)
    # ------------------------------------------------------------------
    def _select_and_preview(self, index: Optional[int]) -> None:
        """程序化选中某步并刷新预期画面(屏蔽 _on_select 避免重复加载图片)。"""
        self._suppress_select = True
        self.listbox.selection_remove(self.listbox.selection())
        if index is not None and 0 <= index < len(self.steps):
            self._list_select(index)
            self._show_expect_preview(index)
        else:
            self._show_expect_preview(None)
        self._suppress_select = False

    def _select_card(self, index: int) -> None:
        """点击步骤卡片:选中并显示预览,同步高亮两处卡片。

        不重建卡片列表(避免卡顿),仅更新新旧卡片边框高亮。
        """
        old_idx = self._preview_index
        self._select_and_preview(index)
        self._update_card_selection(old_idx, index)

    def _update_list_row_waits(self, i: int) -> None:
        """仅原地更新第 i 行 Treeview 的等待文案(不重建列表/卡片)。"""
        db = self.steps[i].get("delay_before", 0)
        da = self.steps[i].get("wait_after", 0)
        try:
            self.listbox.set(str(i), "waits", f"前{db:g}/后{da:g}")
        except (tk.TclError, KeyError):
            pass
        # 卡片上的等待 chip 是 tk.Label,按文本定位原地刷新
        self._refresh_card_wait_chips(i)

    # ------------------------------------------------------------------
    # 步骤增删改
    # ------------------------------------------------------------------
    def _delete_step(self, index: int) -> None:
        """删除指定步骤(卡片右上角 ✕)。"""
        if not (0 <= index < len(self.steps)):
            return
        del self.steps[index]
        self._refresh_list()
        nxt = min(index, len(self.steps) - 1) if self.steps else None
        self._select_and_preview(nxt)

    def _delete_selected(self) -> None:
        sel = self._list_sel()
        for i in reversed(sel):
            del self.steps[i]
        self._refresh_list()
        # 删除后选中相邻步骤(优先同位置),没有则清空预览
        nxt = min(sel[0], len(self.steps) - 1) if self.steps and sel else None
        self._select_and_preview(nxt)

    def _move(self, delta: int) -> None:
        sel = self._list_sel()
        if len(sel) != 1:
            return
        i = sel[0]
        j = i + delta
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self._refresh_list()
            self._select_and_preview(j)

    def _clear(self) -> None:
        self.steps.clear()
        self._refresh_list()
        self._select_and_preview(None)

    def _refresh_list(self) -> None:
        """重建步骤列表(Treeview 紧凑行:编号/操作/目标/等待)与中栏步骤卡片。"""
        self.listbox.delete(*self.listbox.get_children())
        for i, step in enumerate(self.steps):
            before_wait = step.get("delay_before", 0)
            after_wait = step.get("wait_after", 0)
            waits = f"前{before_wait:g}/后{after_wait:g}"
            stype = step.get("type")
            desc = (step.get("desc") or "").strip()
            tags = ["odd" if i % 2 else "even"]
            if stype == "input":
                title = desc or "输入文本"
                meta = f"{step['cell']} \"{step.get('text', '')}\""
            elif stype in APP_OP_TYPES or stype == "keyevent":
                title = self._step_action_text(step)
                meta = "系统操作"
                tags.append("sysop")
            else:
                title = desc or "点击"
                cell = step.get("cell", "")
                x, y = step.get("x"), step.get("y")
                meta = f"{cell}" + (f"({x},{y})" if x is not None and y is not None else "")
            self.listbox.insert("", "end", iid=str(i), tags=tags,
                                 values=(i + 1, title, meta, waits))
        # 同步刷新中栏步骤卡片与右栏普通模式紧凑卡片
        self._refresh_step_cards()
        self._refresh_normal_cards()

    def _refresh(self) -> None:
        """重新从设备截取当前画面并刷新网格显示(步骤列表保留)。"""
        if self._running:
            return
        try:
            self._capture_from_device()
        except Exception as exc:
            messagebox.showerror("截图失败", str(exc))
            return
        self._log(f"已刷新截图: {self.device_id} "
                  f"({self.grid_bgr.shape[1]}x{self.grid_bgr.shape[0]}, 格子 {self.cell_size}px)")

    # ------------------------------------------------------------------
    # 保存 / 加载
    # ------------------------------------------------------------------
    # Windows 文件名非法字符
    _INVALID_NAME_CHARS = set('\\/:*?"<>|')

    def _save(self) -> None:
        """
        保存脚本:以"脚本名称"输入框为唯一来源,直接存到 scripts/<名称>.json。

        - 名称为空:提示用户先输入名称,不保存;
        - 含非法字符:提示修正;
        - 与当前内存脚本名不同:
            * 新建草稿(untitled):移动锚点文件夹到新名称;
            * 已加载脚本另存新名:复制锚点文件夹(保留原脚本完整);
        - 目标 JSON 已存在且不是当前脚本:覆盖前二次确认;
        - 保存成功后清空名称输入框并回到"新建脚本"状态,
          确保下次保存必须输入新名称。
        """
        name = self.script_name_var.get().strip()
        if not name:
            messagebox.showwarning("缺少脚本名称", "请先在「脚本名称」输入框中输入名称,再点击保存。")
            return
        bad = sorted(set(name) & self._INVALID_NAME_CHARS)
        if bad:
            messagebox.showwarning("名称非法", f"脚本名称不能包含字符: {' '.join(bad)}")
            return

        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        target_path = os.path.join(SCRIPTS_DIR, name, f"{name}.json")
        same_script = self.script_path is not None and \
            os.path.normcase(os.path.abspath(target_path)) == \
            os.path.normcase(os.path.abspath(self.script_path))

        if not same_script and os.path.isfile(target_path):
            if not messagebox.askyesno("覆盖确认", f"脚本 {name} 已存在,是否覆盖?"):
                return

        if name != self.script_name:
            # 新建草稿:move(untitled 是临时目录);已加载脚本另存:copy(保留原脚本)
            move = self.script_path is None
            if not self._migrate_anchor_folder(self.script_name, name, move=move):
                messagebox.showerror(
                    "保存失败",
                    f"脚本文件夹 scripts/{name}/ 已存在且非空,\n"
                    f"请换一个脚本名,或先清空该文件夹。",
                )
                return
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        try:
            save_steps(self.steps, target_path)
            n_before = sum(1 for s in self.steps if s.get("before_image"))
            n_after = sum(1 for s in self.steps if s.get("after_image"))
            self._log(f"脚本已保存: {target_path}(共 {len(self.steps)} 步,"
                      f"执行前图 {n_before} 张,执行后图 {n_after} 张)")
        except StepError as exc:
            messagebox.showerror("保存失败", str(exc))
            return

        self._cleanup_orphan_anchors()
        # 成功后回到"新建脚本"状态:清空名称,后续录制进入新的 untitled 草稿
        self._reset_to_new_script()

    def _reset_to_new_script(self) -> None:
        """保存成功后重置:清空名称输入框/脚本绑定/步骤列表,准备录制下一个脚本。"""
        self.script_name_var.set("")
        self.script_name = DRAFT_SCRIPT_NAME
        self.script_path = None
        self.steps.clear()
        self._before_seq = 0
        self._after_seq = 0
        self._refresh_list()
        self._select_and_preview(None)

    def _cleanup_orphan_anchors(self) -> None:
        """删除当前脚本 images/ 中未被任何步骤 before/after 引用的图片(孤儿文件)。"""
        anchor_dir = self._anchor_dir()
        if not os.path.isdir(anchor_dir):
            return
        referenced_names = {
            os.path.basename(s.get(field, ""))
            for s in self.steps for field in ("before_image", "after_image")
            if s.get(field)
        }
        for name in os.listdir(anchor_dir):
            if name.lower().endswith(".png") and name not in referenced_names:
                try:
                    os.remove(os.path.join(anchor_dir, name))
                except OSError:
                    pass

    def _migrate_anchor_folder(self, old_name: str, new_name: str, move: bool = True) -> bool:
        """
        保存为不同脚本名时迁移脚本文件夹 scripts/<old>/ -> scripts/<new>/。

        JSON 中锚点路径固定为 "images/NNNN.png"(相对 JSON 所在目录),
        文件夹改名后无需改写。

        Args:
            move: True 整个文件夹移动(新建草稿 untitled -> 正式名,草稿目录消失);
                  False 仅复制 images/ 子目录(已加载脚本另存新名,
                  原脚本的 <old>.json 不会被带进新文件夹)。

        Returns:
            True 完成迁移(或无图片可迁);False 目标文件夹已存在非空内容,中止。
        """
        if old_name == new_name:
            return True
        old_dir = os.path.join(SCRIPTS_DIR, old_name)
        new_dir = os.path.join(SCRIPTS_DIR, new_name)
        if os.path.isdir(new_dir) and os.listdir(new_dir):
            return False
        os.makedirs(new_dir, exist_ok=True)
        if move:
            # 草稿目录里只有 images/,整体移动
            old_images = os.path.join(old_dir, "images")
            if os.path.isdir(old_images):
                shutil.move(old_images, os.path.join(new_dir, "images"))
            if os.path.isdir(old_dir):
                try:
                    os.rmdir(old_dir)  # 草稿目录此时应为空
                except OSError:
                    pass
        else:
            # 另存:只复制图片,不复制原 JSON
            old_images = os.path.join(old_dir, "images")
            if os.path.isdir(old_images):
                shutil.copytree(old_images, os.path.join(new_dir, "images"),
                                dirs_exist_ok=True)
        self.script_name = new_name
        # 编号计数器在下次截图时按 images/ 实际内容扫描,无需在此同步
        return True

    def _load(self) -> None:
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        path = filedialog.askopenfilename(
            initialdir=SCRIPTS_DIR, filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        self._load_script(os.path.abspath(path))

    def _load_script(self, path: str) -> bool:
        """加载指定脚本 JSON 到普通模式(执行模式"加载到普通模式"复用)。成功返回 True。"""
        try:
            self.steps = load_steps(path)
        except StepError as exc:
            messagebox.showerror("加载失败", str(exc))
            return False
        # 切换当前脚本:锚点文件夹与 JSON 同名,锚点相对路径以 JSON 所在目录为基准
        self.script_path = path
        self.script_name = os.path.splitext(os.path.basename(path))[0]
        self.script_name_var.set(self.script_name)  # 回填名称输入框
        script_dir = os.path.dirname(path)
        # 编号计数器在下次截图时按 images/ 实际内容扫描接续
        # 切换脚本时清空图片缓存:旧脚本的锚点图路径与新脚本不同,避免误命中
        self._img_cache.clear()
        self._preview_cache.clear()
        self._refresh_list()
        self._select_and_preview(0 if self.steps else None)  # 加载后展示第一步标准图
        missing = [s[field] for s in self.steps for field in ("before_image", "after_image")
                   if s.get(field)
                   and not os.path.isfile(os.path.join(script_dir, s[field]))]
        self._log(f"脚本已加载: {path}(共 {len(self.steps)} 步)")
        if missing:
            self._log(f"[提示] {len(missing)} 张标准图缺失,对应位置将跳过画面校验/退化为固定等待")
        return True

    # ------------------------------------------------------------------
    # UI 构建:中栏"步骤·实时预览"
    # ------------------------------------------------------------------
    def _build_center_panel(self, center: tk.Frame) -> None:
        """在中栏容器内构建脚本名行/步骤卡片滚动区/运行条/步骤详情与锚点面板。"""
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(2, weight=1)
        tk.Label(center, text="步骤 · 实时预览", fg=THEME["txt3"],
                 bg=THEME["surface"], font=FONT_CAPTION).grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        # 脚本名行
        sname = tk.Frame(center, bg=THEME["surface"])
        sname.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        sname.columnconfigure(0, weight=1)
        self.script_name_var = tk.StringVar(value="")
        tk.Entry(sname, textvariable=self.script_name_var,
                 bg=THEME["surface2"], fg=THEME["txt"], font=FONT_BTN,
                 bd=0, relief="flat", insertbackground=THEME["txt"],
                 highlightthickness=1,
                 highlightbackground=THEME["border2"],
                 highlightcolor=THEME["border2"]).grid(
            row=0, column=0, sticky="ew", padx=(0, 6), ipady=8)
        _RoundedButton(sname, text="保存", command=self._save).grid(row=0, column=1, padx=2)
        _RoundedButton(sname, text="加载", command=self._load).grid(row=0, column=2, padx=2)
        # 步骤卡片滚动区
        self._step_cards_wrap = tk.Frame(center, bg=THEME["surface"])
        self._step_cards_wrap.grid(row=2, column=0, sticky="nsew")
        self._step_cards_wrap.grid_columnconfigure(0, weight=1)
        self._step_cards_wrap.grid_rowconfigure(0, weight=1)
        self._cards_canvas = tk.Canvas(self._step_cards_wrap, bg=THEME["surface"],
                                       highlightthickness=0)
        cards_scroll = ttk.Scrollbar(self._step_cards_wrap, orient="vertical",
                                     command=self._cards_canvas.yview)
        self._cards_canvas.configure(yscrollcommand=cards_scroll.set)
        self._cards_canvas.grid(row=0, column=0, sticky="nsew")
        cards_scroll.grid(row=0, column=1, sticky="ns")
        self._step_cards_inner = tk.Frame(self._cards_canvas, bg=THEME["surface"])
        self._cards_win = self._cards_canvas.create_window(
            (0, 0), window=self._step_cards_inner, anchor="nw")
        self._step_cards_inner.bind("<Configure>",
            lambda _e: self._cards_canvas.configure(
                scrollregion=self._cards_canvas.bbox("all")))
        self._cards_canvas.bind("<Configure>",
            lambda e: self._cards_canvas.itemconfigure(self._cards_win, width=e.width))
        # 运行条(居中紧凑排列)
        runbar = tk.Frame(center, bg=THEME["surface"])
        runbar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        runbar_btns = tk.Frame(runbar, bg=THEME["surface"])
        runbar_btns.pack()
        self.run_btn = _RoundedButton(runbar_btns, text="▶ 运行", command=self._run,
                                      style="primary", width=96)
        self.run_btn.pack(side="left", padx=3)
        self.full_run_btn = _RoundedButton(runbar_btns, text="完整运行", command=self._full_run,
                                           width=96)
        self.full_run_btn.pack(side="left", padx=3)
        _RoundedButton(runbar_btns, text="清空", command=self._clear, style="ghost",
                       width=96).pack(side="left", padx=3)
        # 步骤详情 + 锚点(复用原 preview 控件,置于中栏卡片下方)
        self.preview_frame = tk.Frame(center, bg=THEME["bg2"],
                                      highlightthickness=1,
                                      highlightbackground=THEME["border"])
        self.preview_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.preview_frame.grid_columnconfigure(0, weight=1)
        self.preview_frame.grid_columnconfigure(1, weight=1)
        self._preview_index: Optional[int] = None
        self._suppress_select = False  # 程序化选中时屏蔽 _on_select,避免重复加载图片
        # LRU 有界缓存:防止单会话 Tcl 图片表 / 内存无限增长导致 GC 越来越慢
        from collections import OrderedDict
        self._preview_cache: "OrderedDict[str, Any]" = OrderedDict()
        self._img_cache: "OrderedDict[str, Any]" = OrderedDict()
        self._CACHE_LIMIT = 96
        ttk.Label(self.preview_frame, text="步骤详情 / 锚点", style="Caption.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 0))
        self._detail_info_var = tk.StringVar(value="未选中步骤")
        ttk.Label(self.preview_frame, textvariable=self._detail_info_var,
                  foreground=THEME["txt2"], wraplength=340, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 0))
        waits = ttk.Frame(self.preview_frame)
        waits.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 0))
        self._detail_before_var = tk.StringVar(value="0")
        self._detail_after_var = tk.StringVar(value="0")
        ttk.Label(waits, text="前等待(s)", style="Caption.TLabel").grid(row=0, column=0)
        ttk.Spinbox(waits, from_=0, to=600, width=6,
                    textvariable=self._detail_before_var).grid(row=0, column=1, padx=(2, 10))
        ttk.Label(waits, text="后等待(s)", style="Caption.TLabel").grid(row=0, column=2)
        ttk.Spinbox(waits, from_=0, to=600, width=6,
                    textvariable=self._detail_after_var).grid(row=0, column=3, padx=(2, 10))
        _RoundedButton(waits, text="保存等待",
                       command=self._apply_step_detail).grid(row=0, column=4, padx=(8, 0))
        # 锚点图横向排列
        anchors = ttk.Frame(self.preview_frame)
        anchors.grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 6))
        anchors.columnconfigure(0, weight=1)
        anchors.columnconfigure(1, weight=1)
        ttk.Label(anchors, text="执行前(开始条件)", style="Caption.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(anchors, text="执行后(完成条件)", style="Caption.TLabel").grid(
            row=0, column=1, sticky="w")
        self._before_photo = self._make_placeholder_image()
        self.before_img_label = tk.Label(
            anchors, image=self._before_photo, width=EXPECT_IMG_W, height=EXPECT_IMG_H,
            highlightthickness=1, highlightbackground=THEME["border2"],
            bg=THEME["surface2"])
        self.before_img_label.grid(row=1, column=0, sticky="w", pady=2)
        self._after_photo = self._make_placeholder_image()
        self.after_img_label = tk.Label(
            anchors, image=self._after_photo, width=EXPECT_IMG_W, height=EXPECT_IMG_H,
            highlightthickness=1, highlightbackground=THEME["border2"],
            bg=THEME["surface2"])
        self.after_img_label.grid(row=1, column=1, sticky="w", pady=2)
        self.before_status_var = tk.StringVar(value="未选中步骤")
        self.after_status_var = tk.StringVar(value="未选中步骤")
        ttk.Label(anchors, textvariable=self.before_status_var, style="Caption.TLabel",
                  wraplength=EXPECT_IMG_W, justify="left").grid(row=2, column=0, sticky="w")
        ttk.Label(anchors, textvariable=self.after_status_var, style="Caption.TLabel",
                  wraplength=EXPECT_IMG_W, justify="left").grid(row=2, column=1, sticky="w")
        anchor_btns = tk.Frame(self.preview_frame, bg=THEME["bg2"])
        anchor_btns.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        _RoundedButton(anchor_btns, text="重拍执行前图",
                       command=lambda: self._recapture_anchor("before_image")).pack(
            side="left", padx=(0, 6))
        _RoundedButton(anchor_btns, text="截执行后图",
                   command=lambda: self._recapture_anchor("after_image")).pack(side="left")

    # ------------------------------------------------------------------
    # UI 构建:右栏普通模式 Tab
    # ------------------------------------------------------------------
    def _build_normal_tab(self, normal: tk.Frame) -> None:
        """在普通模式面板内构建脚本名/紧凑卡片/底部操作/日志。"""
        normal.columnconfigure(0, weight=1)
        normal.rowconfigure(1, weight=1)
        ns = tk.Frame(normal, bg=THEME["surface"])
        ns.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        ns.columnconfigure(0, weight=1)
        tk.Entry(ns, textvariable=self.script_name_var,
                 bg=THEME["surface2"], fg=THEME["txt"], font=FONT_BTN,
                 bd=0, relief="flat", insertbackground=THEME["txt"],
                 highlightthickness=1,
                 highlightbackground=THEME["border2"],
                 highlightcolor=THEME["border2"]).grid(
            row=0, column=0, sticky="ew", padx=(0, 6), ipady=8)
        _RoundedButton(ns, text="保存脚本", command=self._save).grid(row=0, column=1, padx=2)
        _RoundedButton(ns, text="加载脚本", command=self._load).grid(row=0, column=2, padx=2)
        # 紧凑卡片容器(Canvas 滚动)
        nc_wrap = tk.Frame(normal, bg=THEME["surface"])
        nc_wrap.grid(row=1, column=0, sticky="nsew", padx=10)
        nc_wrap.columnconfigure(0, weight=1)
        nc_wrap.rowconfigure(0, weight=1)
        self._normal_canvas = tk.Canvas(nc_wrap, bg=THEME["surface"],
                                        highlightthickness=0)
        self._normal_canvas.grid(row=0, column=0, sticky="nsew")
        nc_sb = ttk.Scrollbar(nc_wrap, orient="vertical",
                              command=self._normal_canvas.yview)
        nc_sb.grid(row=0, column=1, sticky="ns")
        self._normal_canvas.configure(yscrollcommand=nc_sb.set)
        self._normal_inner = tk.Frame(self._normal_canvas, bg=THEME["surface"])
        self._normal_win = self._normal_canvas.create_window(
            (0, 0), window=self._normal_inner, anchor="nw")
        self._normal_inner.bind("<Configure>", lambda _e: self._normal_canvas.configure(
            scrollregion=self._normal_canvas.bbox("all")))
        self._normal_canvas.bind("<Configure>", lambda e: self._normal_canvas.itemconfigure(
            self._normal_win, width=e.width))
        # 隐藏的 Treeview 作为选中状态模型(不显示)
        self.listbox = ttk.Treeview(
            normal, columns=("no", "title", "meta", "waits"), show="headings",
            selectmode="browse", height=0)
        self.listbox.bind("<<TreeviewSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", self._on_step_double_click)
        # 底部按钮:清空 / 运行(主) / 完整运行(居中紧凑排列,不拉满全宽)
        nfoot = tk.Frame(normal, bg=THEME["surface"])
        nfoot.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 6))
        btns = tk.Frame(nfoot, bg=THEME["surface"])
        btns.pack()
        _RoundedButton(btns, text="✕ 清空", command=self._clear, style="ghost",
                       width=88).pack(side="left", padx=3)
        _RoundedButton(btns, text="▶ 运行", command=self._run, style="primary",
                       width=88).pack(side="left", padx=3)
        _RoundedButton(btns, text="完整运行", command=self._full_run,
                       width=88).pack(side="left", padx=3)
        _RoundedButton(nfoot, text="＋ 系统操作", command=self._add_system_step,
                       width=200).pack(pady=(8, 0))
        # 普通模式日志区(adb 操作等)
        log_wrap = tk.Frame(normal, bg=THEME["surface"])
        log_wrap.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 8))
        tk.Label(log_wrap, text="操作日志", bg=THEME["surface"], fg=THEME["txt2"],
                 font=FONT_CAPTION).pack(anchor="w", pady=(0, 2))
        self._normal_log_text = tk.Text(log_wrap, height=5, font=FONT_MONO, state="disabled",
                                        bg=THEME["term_bg"], fg=THEME["term_fg"],
                                        wrap="word", relief="flat", bd=0,
                                        highlightthickness=1,
                                        highlightbackground=THEME["border2"])
        self._normal_log_text.pack(fill="x")
