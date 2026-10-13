"""
runner/ui/runner_mixin.py - StepApp 执行职责域(自 gui_steps.py 拆出,CR-08)
==========================================================================
负责: 普通模式运行/完整运行(含录制+回放渲染)、右栏执行模式 Tab
(测试计划:批量勾选 scripts 下用例,逐条录制+回放,汇总 plan.json)。

线程纪律: StepRunner.run 在后台线程执行,日志/状态经 self._bridge.post()
回主线程; 事件回调固定三参 (idx, total, msg)(SKILL.md 约定)。
"""

from __future__ import annotations

import json
import os
import time
import threading
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import messagebox, ttk

from steps import APP_OP_TYPES, StepError, StepRunner, load_steps

from ui.constants import DRAFT_SCRIPT_NAME, RESULT_DIR, SCRIPTS_DIR
from ui.theme import THEME, FONT_CAPTION, FONT_MONO
from ui.widgets import _RoundedButton


class _RunnerMixin:
    """运行控制与执行模式(混入 StepApp)。"""

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    def _run(self) -> None:
        if self._running:
            return
        if not self.steps:
            messagebox.showwarning("无步骤", "请先在左侧图片上点击格子添加步骤")
            return
        # 选中且仅选中一个步骤时:只执行该步;否则从头顺序执行全部
        sel = self._list_sel()
        if len(sel) == 1:
            self._run_offset = sel[0]
            run_steps = [self.steps[sel[0]]]
        else:
            self._run_offset = 0
            run_steps = self.steps
        self._start_run(run_steps, record=False)

    def _full_run(self) -> None:
        """完整运行:忽略选中态从头跑全部步骤,并自动录制 + 渲染回放视频。"""
        if self._running:
            return
        if not self.steps:
            messagebox.showwarning("无步骤", "请先在左侧图片上点击格子添加步骤")
            return
        self._run_offset = 0
        self._start_run(list(self.steps), record=True)

    def _start_run(self, run_steps: List[Dict[str, Any]], record: bool) -> None:
        """_run/_full_run 共用的执行入口:停刷新 -> 建 runner -> 起后台线程。"""
        # 先进入运行态:阻止新的自动刷新启动;再等在途刷新落盘完成,
        # 避免读到半写入的 screenshot.png / 与执行轮询争抢 adb
        self._running = True
        self.run_btn.configure(state="disabled")
        self.full_run_btn.configure(state="disabled")
        deadline = time.monotonic() + 3.0
        while self._auto_busy and time.monotonic() < deadline:
            self.update()
            time.sleep(0.03)
        if self._auto_busy:
            # 极端情况下刷新仍未结束:执行线程与刷新各用独立临时文件,不再互相破坏,
            # 但 screenshot.png 可能正在写——执行不依赖它(坐标只取分辨率),继续即可
            self._log("[提示] 画面刷新尚未完成,已忽略其结果继续执行")
        try:
            from ui.constants import _BASE_DIR
            runner = StepRunner.from_capture_artifacts(_BASE_DIR)
        except StepError as exc:
            self._running = False
            self.run_btn.configure(state="normal")
            self.full_run_btn.configure(state="normal")
            messagebox.showerror("执行环境缺失", str(exc))
            return
        # 锚点图相对路径以当前脚本 JSON 所在目录为基准(未保存草稿用 scripts/untitled/)
        runner.script_dir = (os.path.dirname(os.path.abspath(self.script_path))
                             if self.script_path
                             else os.path.join(SCRIPTS_DIR, DRAFT_SCRIPT_NAME))
        if record:
            self._log(f"开始完整运行脚本 [{self.script_name}],共 {len(run_steps)} 步"
                      f"(自动录制+回放),设备: {runner.device_id}")
        elif self._run_offset:
            self._log(f"开始执行选中步骤 第 {self._run_offset + 1} 步,"
                      f"设备: {runner.device_id}")
        else:
            self._log(f"开始执行脚本 [{self.script_name}],共 {len(self.steps)} 步,"
                      f"设备: {runner.device_id}")
        threading.Thread(target=self._run_worker,
                         args=(runner, run_steps, record), daemon=True).start()

    def _run_worker(self, runner: StepRunner, run_steps: List[Dict[str, Any]],
                    record: bool) -> None:
        recorder = None
        try:
            if record:
                from recording import Recorder
                recorder = Recorder.create(device_id=runner.device_id)
                rec_path = recorder.recording_path
                self._bridge.post(lambda p=rec_path: self._log(f"[录制] 运行目录: {p}"))
            runner.run(run_steps, on_event=self._on_step_event, recorder=recorder)
        except Exception as exc:  # AdbError / StepError 等,统一在 UI 层提示
            err_msg = str(exc)
            self._bridge.post(lambda m=err_msg: self._log(f"[失败] {m}"))
        finally:
            self._bridge.post(lambda: self._run_finished(recorder))

    def _run_finished(self, recorder=None) -> None:
        self._running = False
        self.run_btn.configure(state="normal")
        self.full_run_btn.configure(state="normal")
        # 完整运行:录制已收尾(recording.json 落盘),后台渲染回放视频
        if recorder is not None and os.path.isfile(recorder.recording_path):
            rec_path = recorder.recording_path
            threading.Thread(target=self._render_worker,
                             args=(rec_path,), daemon=True).start()

    def _render_worker(self, recording_path: str) -> None:
        """后台渲染回放视频(测试报告),结果回执行日志。"""
        try:
            self._bridge.post(lambda: self._log("[回放] 正在渲染回放视频…"))
            from replay_render import render_to_video
            out = render_to_video(recording_path)
            size_mb = os.path.getsize(out) / 1048576
            self._bridge.post(
                lambda o=out, s=size_mb: self._log(f"[回放] 视频已生成: {o} ({s:.2f} MB)"))
        except Exception as exc:
            err_msg = str(exc)
            self._bridge.post(lambda m=err_msg: self._log(f"[回放] 渲染失败: {m}"))

    # ------------------------------------------------------------------
    # 执行模式(测试计划:批量勾选 scripts 下的用例执行)
    # ------------------------------------------------------------------
    def _exec_log(self, msg: str) -> None:
        """写入执行模式日志区。"""
        self._exec_log_text.configure(state="normal")
        self._exec_log_text.insert("end", msg + "\n")
        self._exec_log_text.see("end")
        self._exec_log_text.configure(state="disabled")

    def _exec_refresh(self) -> None:
        """扫描 SCRIPTS_DIR(递归,支持多层文件夹):每个 JSON 文件视为一条用例。"""
        self._exec_cases.clear()
        if os.path.isdir(SCRIPTS_DIR):
            for dirpath, dirnames, filenames in os.walk(SCRIPTS_DIR):
                dirnames[:] = [d for d in dirnames if d != "images"]  # 跳过锚点图目录
                for fn in sorted(filenames):
                    if not fn.lower().endswith(".json"):
                        continue
                    path = os.path.abspath(os.path.join(dirpath, fn))
                    self._exec_cases[path] = {
                        "name": os.path.splitext(fn)[0],
                        "path": path,
                        "dir": dirpath,
                        "rel": os.path.relpath(path, SCRIPTS_DIR),
                    }
        # 重建树(保留旧勾选状态)
        checked = {iid for iid in self._exec_tree.get_children()
                   if self._exec_tree.set(iid, "sel") == "☑"}
        self._exec_tree.delete(*self._exec_tree.get_children())
        for iid, case in self._exec_cases.items():
            mark = "☑" if iid in checked else "☐"
            self._exec_tree.insert("", "end", iid=iid, values=(
                mark, case["name"], "待执行"))
        self._exec_log(f"[用例] 扫描完成,共 {len(self._exec_cases)} 条"
                       f"(目录: {SCRIPTS_DIR})")

    def _on_exec_tree_click(self, event) -> None:
        """点击用例行:右侧展示详情;点"选"或"用例"列同时切换勾选状态。"""
        region = self._exec_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self._exec_tree.identify_column(event.x)
        iid = self._exec_tree.identify_row(event.y)
        if not iid:
            return
        self._show_exec_case_detail(iid)
        if col in ("#1", "#2"):  # 点"选"或"用例"列都可切换勾选
            cur = self._exec_tree.set(iid, "sel")
            self._exec_tree.set(iid, "sel", "☐" if cur == "☑" else "☑")

    def _show_exec_case_detail(self, iid: str) -> None:
        """执行模式:展示用例名称/路径/步骤摘要,并展开详情面板。"""
        case = self._exec_cases.get(iid)
        if case is None:
            return
        self._exec_detail_iid = iid
        try:
            steps = load_steps(case["path"])
        except Exception as exc:
            self._exec_detail_info.set(
                f"用例: {case['name']} | 路径: {case['rel']} | 加载失败: {exc}")
            steps = []
        else:
            self._exec_detail_info.set(
                f"用例: {case['name']} | 路径: {case['rel']} | 共 {len(steps)} 步")
        lines = []
        for i, s in enumerate(steps, 1):
            if s["type"] == "input":
                action = f"输入[{s.get('text', '')}]"
            elif s["type"] in APP_OP_TYPES or s["type"] == "keyevent":
                action = self._step_action_text(s)
            else:
                action = f"点击 {s['cell']}"
            desc = s.get("desc") or ""
            lines.append(f"{i}. {action} 前{s['delay_before']}s/后{s['wait_after']}s"
                         + (f"  {desc}" if desc else ""))
        self._exec_detail_steps.configure(state="normal")
        self._exec_detail_steps.delete("1.0", "end")
        self._exec_detail_steps.insert("end", "\n".join(lines) if lines else "(无步骤)")
        self._exec_detail_steps.configure(state="disabled")
        self._exec_detail_frame.grid()  # 点击用例时展开详情

    def _exec_load_to_normal(self) -> None:
        """把执行模式当前详情的用例加载到普通模式并切换过去。"""
        case = self._exec_cases.get(self._exec_detail_iid or "")
        if case is None:
            messagebox.showinfo("未选中用例", "请先在左侧列表点击一条用例")
            return
        if self._load_script(case["path"]):
            self._switch_tab(1)  # 切到普通模式

    def _exec_open_report(self) -> None:
        """用系统资源管理器打开最近一次测试计划的结果目录。"""
        if self._last_plan_dir and os.path.isdir(self._last_plan_dir):
            os.startfile(self._last_plan_dir)  # Windows
        else:
            messagebox.showinfo("无报告", "尚未执行过测试计划")

    def _exec_toggle_all(self) -> None:
        """全选/全不选:有任一未勾选则全部勾选,否则全部取消。"""
        iids = self._exec_tree.get_children()
        target = "☐" if all(self._exec_tree.set(i, "sel") == "☑" for i in iids) else "☑"
        for iid in iids:
            self._exec_tree.set(iid, "sel", target)

    def _exec_set_all(self, on: bool) -> None:
        """全选(on=True)或全不选(on=False)。"""
        target = "☑" if on else "☐"
        for iid in self._exec_tree.get_children():
            self._exec_tree.set(iid, "sel", target)

    def _exec_set_status(self, iid: str, status: str) -> None:
        self._exec_tree.set(iid, "status", status)

    def _exec_checked(self) -> None:
        """执行所有勾选用例:每次生成一个测试计划目录 runner/result/<plan-id>/。"""
        if self._running or self._exec_busy:
            return
        checked = [iid for iid in self._exec_tree.get_children()
                   if self._exec_tree.set(iid, "sel") == "☑"]
        if not checked:
            messagebox.showwarning("未选择用例", "请先勾选要执行的用例")
            return
        self._exec_busy = True
        self._running = True  # 与普通模式运行互斥
        self._exec_btn.configure(state="disabled")
        plan_id = time.strftime("plan-%Y%m%d-%H%M%S")
        plan_dir = os.path.join(RESULT_DIR, plan_id)
        os.makedirs(plan_dir, exist_ok=True)
        self._last_plan_dir = plan_dir
        self._exec_log(f"[计划] {plan_id}:共 {len(checked)} 条用例,结果目录 {plan_dir}")
        for iid in self._exec_tree.get_children():  # 重置所有状态列
            self._exec_tree.set(iid, "status", "待执行")
        threading.Thread(target=self._exec_worker,
                         args=(checked, plan_dir, plan_id), daemon=True).start()

    def _exec_worker(self, checked: List[str], plan_dir: str, plan_id: str) -> None:
        """后台顺序执行勾选用例;每条用例录制+回放,最后写 plan.json 汇总。"""
        results: List[Dict[str, Any]] = []
        try:
            from ui.constants import _BASE_DIR
            runner = StepRunner.from_capture_artifacts(_BASE_DIR)
        except Exception as exc:
            err_msg = str(exc)
            self._bridge.post(lambda m=err_msg: self._exec_log(f"[失败] 环境异常: {m}"))
            self._bridge.post(lambda: self._exec_finished())
            return
        self._bridge.post(
            lambda d=runner.device_id: self._exec_log(f"[设备] {d}"))

        for seq, iid in enumerate(checked, 1):
            case = self._exec_cases[iid]
            name = case["name"]
            self._bridge.post(lambda i=iid: self._exec_set_status(i, "执行中…"))
            self._bridge.post(lambda n=name, s=seq, t=len(
                checked): self._exec_log(f"—— [{s}/{t}] 用例 [{n}] 开始 ——"))
            t0 = time.time()
            rec = None
            error: Optional[str] = None
            rec_rel = replay_rel = ""
            try:
                steps = load_steps(case["path"])
                if not steps:
                    raise StepError("用例没有任何步骤")
                from recording import Recorder
                rec = Recorder.create(device_id=runner.device_id,
                                      runs_dir=plan_dir, run_id=name)
                rec_rel = os.path.relpath(rec.recording_path, plan_dir)
                runner.script_dir = case["dir"]  # 锚点图以用例目录为基准
                runner.run(steps,
                           on_event=self._make_exec_event(name, len(steps)),
                           recorder=rec)  # 异常时 recorder 内部已 fail+finish
                status = "passed"
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                status = "failed"
            duration = round(time.time() - t0, 1)

            # 回放视频(测试报告):recording.json 已落盘才渲染
            if rec is not None and os.path.isfile(rec.recording_path):
                try:
                    from replay_render import render_to_video
                    out = render_to_video(rec.recording_path)
                    replay_rel = os.path.relpath(out, plan_dir)
                    self._bridge.post(lambda n=name, o=replay_rel: self._exec_log(
                        f"[{n}] 回放视频: {o}"))
                except Exception as exc:
                    self._bridge.post(lambda n=name, m=str(
                        exc): self._exec_log(f"[{n}] 回放渲染失败: {m}"))

            mark = "✓ 成功" if status == "passed" else "✗ 失败"
            self._bridge.post(lambda i=iid, m=mark: self._exec_set_status(i, m))
            if error:
                self._bridge.post(lambda n=name, m=error: self._exec_log(
                    f"[{n}] 失败原因: {m}"))
            self._bridge.post(lambda n=name, s=status, d=duration: self._exec_log(
                f"[{n}] 结束: {s},耗时 {d}s"))
            results.append({
                "name": name,
                "path": case["rel"],
                "status": status,
                "error": error,
                "duration_sec": duration,
                "recording": rec_rel,
                "replay": replay_rel,
            })

        # 汇总测试计划 plan.json
        passed = sum(1 for r in results if r["status"] == "passed")
        summary = {
            "version": 1,
            "plan_id": plan_id,
            "created_at": time.time(),
            "created_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "device_id": runner.device_id,
            "cases": results,
            "summary": {
                "total": len(results),
                "passed": passed,
                "failed": len(results) - passed,
            },
        }
        plan_path = os.path.join(plan_dir, "plan.json")
        try:
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            self._bridge.post(lambda m=str(
                exc): self._exec_log(f"[失败] plan.json 写入失败: {m}"))
        self._bridge.post(
            lambda p=passed, t=len(results), d=plan_dir: self._exec_log(
                f"[计划完成] 通过 {p}/{t},结果目录: {d}"))
        self._bridge.post(lambda: self._exec_finished())

    def _make_exec_event(self, case_name: str, total: int):
        """生成某条用例的步骤事件回调:日志带用例名前缀,写执行模式日志区。"""
        def emit(idx: int, _total: int, msg: str) -> None:
            self._bridge.post(
                lambda n=case_name, i=idx, m=msg: self._exec_log(f"[{n}][{i}/{total}] {m}"))
        return emit

    def _exec_finished(self) -> None:
        self._exec_busy = False
        self._running = False
        self._exec_btn.configure(state="normal")
        if self._last_plan_dir:  # 执行完成后允许打开报告目录
            self._exec_open_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # UI 构建:右栏执行模式 Tab
    # ------------------------------------------------------------------
    def _build_exec_tab(self, exec_tab: tk.Frame) -> None:
        """在执行模式面板内构建勾选树/用例详情/执行按钮/日志。"""
        exec_tab.columnconfigure(0, weight=1)
        exec_tab.rowconfigure(2, weight=1)
        ttk.Label(exec_tab, text="勾选测试用例执行(每个对应一个录制脚本)",
                  style="Caption.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        eh = tk.Frame(exec_tab, bg=THEME["surface"])
        eh.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        eh.columnconfigure(0, weight=1)
        _RoundedButton(eh, text="↻ 刷新", command=self._exec_refresh).pack(side="left")
        _RoundedButton(eh, text="全选", command=lambda: self._exec_set_all(True)).pack(side="left", padx=4)
        _RoundedButton(eh, text="全不选", command=lambda: self._exec_set_all(False)).pack(side="left")
        self._exec_tree = ttk.Treeview(
            exec_tab, columns=("sel", "name", "status"),
            show="headings", height=10, selectmode="none")
        self._exec_tree.heading("sel", text="选")
        self._exec_tree.heading("name", text="用例")
        self._exec_tree.heading("status", text="状态")
        self._exec_tree.column("sel", width=36, anchor="center", stretch=False)
        self._exec_tree.column("name", width=180, anchor="w")
        self._exec_tree.column("status", width=70, anchor="center", stretch=False)
        self._exec_tree.grid(row=2, column=0, sticky="nsew", pady=(0, 4))
        es = ttk.Scrollbar(exec_tab, orient="vertical", command=self._exec_tree.yview)
        es.grid(row=2, column=1, sticky="ns")
        self._exec_tree.configure(yscrollcommand=es.set)
        self._exec_tree.bind("<Button-1>", self._on_exec_tree_click)
        self._exec_cases: Dict[str, Dict[str, Any]] = {}
        # 用例详情(点击用例时填充)
        self._exec_detail_frame = ttk.Frame(exec_tab)
        self._exec_detail_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self._exec_detail_frame.grid_remove()
        self._exec_detail_iid: Optional[str] = None
        self._exec_detail_info = tk.StringVar(value="")
        ttk.Label(self._exec_detail_frame, textvariable=self._exec_detail_info,
                  style="Caption.TLabel", wraplength=300, justify="left").pack(anchor="w")
        self._exec_detail_steps = tk.Text(self._exec_detail_frame, height=4, font=FONT_MONO,
                                          state="disabled", wrap="none", relief="flat", bd=0)
        self._exec_detail_steps.pack(fill="x", pady=(2, 2))
        _RoundedButton(self._exec_detail_frame, text="← 加载到普通模式",
                       command=self._exec_load_to_normal).pack(anchor="w")
        # 执行按钮行
        efoot = tk.Frame(exec_tab, bg=THEME["surface"])
        efoot.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        efoot.columnconfigure(0, weight=1)
        efoot.columnconfigure(1, weight=1)
        self._exec_btn = _RoundedButton(efoot, text="▶ 执行选中用例",
                                        command=self._exec_checked, style="primary", width=130)
        self._exec_btn.grid(row=0, column=0, sticky="w", padx=(0, 4))
        self._exec_open_btn = _RoundedButton(efoot, text="打开报告目录",
                                             command=self._exec_open_report, width=120)
        self._exec_open_btn.grid(row=0, column=1, sticky="e", padx=(4, 0))
        # 执行模式日志
        self._exec_log_text = tk.Text(exec_tab, height=5, font=FONT_MONO, state="disabled",
                                      bg=THEME["term_bg"], fg=THEME["term_fg"], wrap="word",
                                      relief="flat", bd=0)
        self._exec_log_text.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0))
