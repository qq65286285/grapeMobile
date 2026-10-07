"""
src/steps.py - 步骤编排执行引擎
================================
将"等待 -> 点击格子 -> 等待画面跳转 …"这类操作流程抽象为步骤列表并顺序执行。
与界面解耦：GUI(runner/gui_steps.py)或其他入口均可复用本引擎。

步骤 JSON 结构(可保存/加载复用),每个脚本一个同名文件夹:
    runner/scripts/steps-1/steps-1.json
    runner/scripts/steps-1/images/0002.png
    [
        {
            "type": "tap", "cell": "U18",
            "desc": "点击登录按钮",  # 可选人类可读描述(GUI AI 模式附带),省略则为空
            "delay_before": 0,    # 执行前等待秒数(默认 0)
            "wait_after": 10,     # 点击后等待秒数/画面等待超时上限(默认 0)
            "expect_image": "images/0002.png"
                                  # 可选:本步执行前设备应显示的画面(即上一步跳转
                                  # 完成的标志)。相对脚本 JSON 所在目录解析,
                                  # 锚点图统一放在脚本文件夹下的 images/ 子目录
        },
        {"type": "tap", "cell": "F1"}
    ]

画面等待语义:
    - 点完第 N 步后,若第 N+1 步带 expect_image,则以第 N 步的 wait_after 为超时上限
      轮询设备当前截图,与锚点图做全屏相似度比对:
        * 相似度 >= expect_threshold(默认 0.92):跳转完成,立即执行下一步;
        * 超时仍不一致:上报告警后继续(不卡死整个流程);
        * 下一步无 expect_image:退化为旧的固定等待 wait_after 秒。
    - 锚点路径:绝对路径原样使用,相对路径相对"脚本目录"(script_dir,
      默认等于 runner 目录)解析。

执行所需的设备与网格信息全部来自 capture.py 的产物:
    runner/device_id.txt      设备 id
    runner/tmp/image/grid_meta.json   格子尺寸 cell_size
    runner/tmp/image/screenshot.png   原图(提供分辨率)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2

import cvio
from adbtools import AdbClient, AdbError
from grid import GridMarker, parse_ref

logger = logging.getLogger("imgloc.steps")

Step = Dict[str, Any]
EventCallback = Callable[[int, int, str], None]

# 全屏画面相似度阈值:同设备同页面的两次截图几乎逐像素一致,0.92 留足动画/状态栏裕量
DEFAULT_EXPECT_THRESHOLD = 0.92
# 轮询截图的最小间隔(秒);adb 截图本身有耗时,这是两次截图之间的额外间隔
DEFAULT_POLL_INTERVAL = 0.5

# ---- 非 UI 步骤(不依赖画面定位):App 生命周期 + 系统按键 ----
# app_stop: 强制关闭 App(am force-stop)
# app_start: 启动 App(monkey LAUNCHER)
# app_clear: 清除 App 数据(pm clear)
# keyevent: 发送系统按键
APP_OP_TYPES = ("app_stop", "app_start", "app_clear")
KEYEVENT_STEP_TYPE = "keyevent"
NON_UI_STEP_TYPES = frozenset(APP_OP_TYPES + (KEYEVENT_STEP_TYPE,))
# keyevent 可选按键(脚本里写短名) -> Android KEYCODE
KEYEVENT_MAP = {
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
    "recents": "KEYCODE_APP_SWITCH",
}
KEYEVENT_LABELS = {"home": "Home 键", "back": "返回键", "recents": "最近任务键"}


class StepError(Exception):
    """步骤定义非法或执行环境缺失(设备/网格产物)时抛出。"""


def _parse_seconds(value: Any, field: str, index: int) -> float:
    """解析秒数字段:必须是非负数字,否则抛 StepError。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise StepError(f"第 {index} 步 {field} 必须是数字: {value!r}") from exc
    if seconds < 0:
        raise StepError(f"第 {index} 步 {field} 不能为负: {seconds}")
    return seconds


def validate_steps(steps: Any) -> List[Step]:
    """
    校验并规范化步骤列表。

    每个 tap/input 步骤支持:
        delay_before: 执行前固定等待秒数(默认 0);
        wait_after:   点击后"等待画面到达"的超时上限秒数(默认 0,无锚点时即固定等待);
        before_image: 执行前标准图(开始条件)——本步执行前设备应显示的画面;
        after_image:  执行后标准图(完成条件)——本步点击后应跳转到的画面。
                      缺省时,引擎自动用"下一步的 before_image"作为本步完成条件。
        text:         input 步骤专用:要输入的文本(仅支持英文/数字,中文受限)。
    旧字段 expect_image 等价于 before_image(仅加载兼容,保存时不再写出)。

    App 外操作(非 UI 步骤,无需格子/锚点):
        {"type": "app_stop", "package": ""}   关闭 App;package 留空 = 执行时
                                             自动识别当前前台 App
        {"type": "app_start", "package": "com.xxx"}  启动 App
        {"type": "app_clear", "package": "com.xxx"}  清除 App 数据
        {"type": "keyevent", "key": "home"}   系统按键:home/back/recents
    非 UI 步骤同样支持 delay_before/wait_after/desc。

    Raises:
        StepError: 结构非法(类型未知/格子编号非法/秒数非法等)时抛出。

    Returns:
        规范化后的步骤列表(cell 大写、时间字段 float、锚点字段补齐为字符串)。
    """
    if not isinstance(steps, list) or not steps:
        raise StepError("步骤列表必须是非空数组")
    normalized: List[Step] = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise StepError(f"第 {i} 步必须是对象,实际: {step!r}")
        stype = step.get("type")
        if stype not in ("tap", "input") + APP_OP_TYPES + (KEYEVENT_STEP_TYPE,):
            raise StepError(
                f"第 {i} 步类型未知: {stype!r}"
                f"(支持 tap/input/{'/'.join(APP_OP_TYPES)}/{KEYEVENT_STEP_TYPE})")

        # 非 UI 步骤:无格子/坐标/锚点,只需时间字段 + 类型专有字段
        if stype in APP_OP_TYPES:
            package = step.get("package", "")
            if package is not None and not isinstance(package, str):
                raise StepError(f"第 {i} 步 package 必须是字符串: {package!r}")
            normalized.append({
                "type": stype,
                "cell": "",
                "x": None,
                "y": None,
                "delay_before": _parse_seconds(step.get("delay_before", 0),
                                               "delay_before", i),
                "wait_after": _parse_seconds(step.get("wait_after", 0),
                                             "wait_after", i),
                "before_image": "",
                "after_image": "",
                "desc": str(step.get("desc") or "").strip(),
                "package": (package or "").strip(),
            })
            continue
        if stype == KEYEVENT_STEP_TYPE:
            key = str(step.get("key", "")).strip().lower()
            if key not in KEYEVENT_MAP:
                raise StepError(
                    f"第 {i} 步 keyevent 的 key 必须是 "
                    f"{'/'.join(KEYEVENT_MAP)} 之一: {key!r}")
            normalized.append({
                "type": stype,
                "cell": "",
                "x": None,
                "y": None,
                "delay_before": _parse_seconds(step.get("delay_before", 0),
                                               "delay_before", i),
                "wait_after": _parse_seconds(step.get("wait_after", 0),
                                             "wait_after", i),
                "before_image": "",
                "after_image": "",
                "desc": str(step.get("desc") or "").strip(),
                "key": key,
            })
            continue

        cell = str(step.get("cell", "")).strip().upper()

        # 可选:精确像素坐标(AI 定位产出);提供时优先于格子换算,
        # 此时 cell 仅用于显示/兼容,允许为空
        def _coord_field(field: str):
            value = step.get(field)
            if value in (None, ""):
                return None
            try:
                ival = int(value)
            except (TypeError, ValueError) as exc:
                raise StepError(f"第 {i} 步 {field} 必须是整数像素坐标: {value!r}") from exc
            if ival < 0:
                raise StepError(f"第 {i} 步 {field} 不能为负: {ival}")
            return ival

        px = _coord_field("x")
        py = _coord_field("y")
        if (px is None) != (py is None):
            raise StepError(f"第 {i} 步 x/y 必须同时提供或同时省略")

        if cell:
            try:
                parse_ref(cell)
            except ValueError as exc:
                raise StepError(f"第 {i} 步格子编号非法: {exc}") from exc
        elif px is None:
            raise StepError(f"第 {i} 步必须提供 cell 或 x/y 像素坐标")

        def _img_field(field: str) -> str:
            value = step.get(field, "")
            if value is not None and not isinstance(value, str):
                raise StepError(f"第 {i} 步 {field} 必须是字符串路径: {value!r}")
            return (value or "").strip()

        before_image = _img_field("before_image")
        # 兼容旧脚本:expect_image 语义即 before_image
        if not before_image:
            before_image = _img_field("expect_image")
        after_image = _img_field("after_image")
        # 可选人类可读描述(GUI AI 模式保存步骤时附带的用户指令文本)
        desc = step.get("desc")
        item: Step = {
            "type": stype,
            "cell": cell,
            "x": px,
            "y": py,
            "delay_before": _parse_seconds(step.get("delay_before", 0), "delay_before", i),
            "wait_after": _parse_seconds(step.get("wait_after", 0), "wait_after", i),
            "before_image": before_image,
            "after_image": after_image,
            "desc": "" if desc is None else str(desc).strip(),
        }
        if stype == "input":
            text = step.get("text")
            item["text"] = "" if text is None else str(text)
            if not item["text"]:
                raise StepError(f"第 {i} 步 input 必须提供 text(要输入的内容)")
        normalized.append(item)
    return normalized


def load_steps(path: str) -> List[Step]:
    """从 JSON 文件加载步骤列表并校验。"""
    if not os.path.isfile(path):
        raise StepError(f"步骤文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise StepError(f"步骤文件 JSON 解析失败: {exc}") from exc
    return validate_steps(data)


def save_steps(steps: List[Step], path: str) -> None:
    """将步骤列表保存为 JSON 文件(保存前先校验)。"""
    validate_steps(steps)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(steps, f, ensure_ascii=False, indent=2)


class StepRunner:
    """
    步骤执行器:绑定设备与网格,顺序执行步骤。

    Args:
        device_id: 目标设备 id(来自 capture.py 记录)。
        cell_size: 网格格子边长(像素),须与生成网格图时一致。
        image_shape: 原图 shape(提供分辨率,用于坐标换算)。
        runner_dir: capture 产物目录(device_id.txt 所在,图片在 tmp/image/ 下)。
        script_dir: 脚本目录,锚点图相对路径以此解析;默认同 runner_dir。
                    GUI/CLI 执行 runner/scripts/xxx.json 时传该 JSON 所在目录。
        expect_threshold: 画面到达判定阈值(全屏归一化相关系数),默认 0.92。
        poll_interval: 画面轮询的最小间隔(秒),默认 0.5。
    """

    def __init__(
        self,
        device_id: str,
        cell_size: int,
        image_shape: Tuple[int, ...],
        runner_dir: str = "",
        script_dir: Optional[str] = None,
        expect_threshold: float = DEFAULT_EXPECT_THRESHOLD,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        self.device_id = device_id
        self.marker = GridMarker(cell_size=cell_size)
        self.image_shape = image_shape
        self.runner_dir = runner_dir or os.getcwd()
        # 锚点图基准目录:未显式指定时与 runner_dir 一致(向后兼容旧布局)
        self.script_dir = script_dir or self.runner_dir
        self.expect_threshold = expect_threshold
        self.poll_interval = poll_interval
        self._client: Optional[AdbClient] = None

    @classmethod
    def from_capture_artifacts(cls, runner_dir: str) -> "StepRunner":
        """
        从 capture.py 产物构建执行器(推荐用法,保证与网格图配置一致)。

        Raises:
            StepError: 缺少 device_id.txt / grid_meta.json / screenshot.png 时抛出。
        """
        device_file = os.path.join(runner_dir, "device_id.txt")
        meta_file = os.path.join(runner_dir, "tmp", "image", "grid_meta.json")
        image_file = os.path.join(runner_dir, "tmp", "image", "screenshot.png")
        missing = [p for p in (device_file, meta_file, image_file) if not os.path.isfile(p)]
        if missing:
            raise StepError(
                "缺少 capture.py 产物,请先运行 python runner/capture.py:\n  - "
                + "\n  - ".join(missing)
            )
        with open(device_file, "r", encoding="utf-8") as f:
            device_id = f.read().strip()
        with open(meta_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
        img = cvio.imread(image_file)
        if img is None:
            raise StepError(f"原图读取失败: {image_file}")
        return cls(device_id, int(meta["cell_size"]), img.shape, runner_dir=runner_dir)

    def _resolve_expect_path(self, rel_path: str) -> str:
        """锚点图路径:绝对路径原样返回,相对路径相对脚本目录(script_dir)解析。"""
        return rel_path if os.path.isabs(rel_path) else os.path.join(self.script_dir, rel_path)

    def _ensure_client(self) -> AdbClient:
        """惰性挂载设备:首次执行时校验在线并记录 device_id。"""
        if self._client is None:
            self._client = AdbClient()
            self._client.attach(self.device_id)
        return self._client

    @staticmethod
    def _wait(seconds: float, idx: int, emit: EventCallback, wait_tick: float) -> None:
        """带心跳的固定等待:按 wait_tick 间隔发出倒计时事件。"""
        remaining = float(seconds)
        while remaining > 0:
            tick = min(wait_tick, remaining)
            time.sleep(tick)
            remaining -= tick
            emit(idx, f"等待中,剩余 {max(remaining, 0):.1f} 秒")

    @staticmethod
    def screen_similarity(image_a: str, image_b: str) -> float:
        """
        两张全屏截图的相似度:灰度图归一化相关系数(TM_CCOEFF_NORMED),范围约 [-1, 1]。

        同设备同页面截图几乎逐像素一致(分数接近 1);页面不同时分数显著偏低。
        尺寸不一致时把 image_b 缩放到 image_a(理论上不应发生,属防御处理)。
        任一图片读取失败返回 0.0。
        """
        a = cvio.imread(image_a, cv2.IMREAD_GRAYSCALE)
        b = cvio.imread(image_b, cv2.IMREAD_GRAYSCALE)
        if a is None or b is None:
            return 0.0
        if a.shape != b.shape:
            b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
        # 零方差(纯色)图时归一化相关系数无定义:两张都是同色纯色图视为一致,
        # 仅一方为纯色或颜色不同视为不一致。真实截图不会出现,属防御处理。
        a_std, b_std = float(a.std()), float(b.std())
        if a_std < 1e-6 or b_std < 1e-6:
            if a_std < 1e-6 and b_std < 1e-6 and abs(float(a.mean()) - float(b.mean())) < 1.0:
                return 1.0
            return 0.0
        score = float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0, 0])
        return max(0.0, score)

    def _wait_for_expected_screen(
        self,
        expected_path: str,
        timeout: float,
        idx: int,
        emit: EventCallback,
    ) -> bool:
        """
        轮询设备截图,直到当前画面与锚点图相似度达标或超时。

        Args:
            expected_path: 锚点图完整路径(下一步执行前的预期画面)。
            timeout: 超时上限(秒),取上一步的 wait_after。
            idx: 当前步序号(用于事件回调)。
            emit: 进度回调。

        Returns:
            True 表示画面已到达;False 表示超时未到达。
        """
        client = self._ensure_client()
        fd, tmp_path = tempfile.mkstemp(prefix="__imgloc_poll_", suffix=".png")
        os.close(fd)
        start = time.monotonic()
        attempt = 0
        try:
            while True:
                attempt += 1
                client.screenshot(save_path=tmp_path)
                score = self.screen_similarity(tmp_path, expected_path)
                elapsed = time.monotonic() - start
                emit(idx, f"等待跳转… 画面相似度 {score:.2f}/{self.expect_threshold:.2f}"
                          f"(第 {attempt} 次,已等 {elapsed:.1f}s)")
                if score >= self.expect_threshold:
                    emit(idx, f"画面已到达预期状态(相似度 {score:.2f}),提前 "
                              f"{max(timeout - elapsed, 0):.1f}s 继续")
                    return True
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    return False
                time.sleep(min(self.poll_interval, remaining))
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _check_start_condition(self, before_rel: str, idx: int, emit: EventCallback) -> None:
        """
        点击前校验"开始条件":当前画面是否与 before_image 一致。

        只告警不阻断——用户可能已手动导航到中间状态,仍需继续执行。
        """
        before_path = self._resolve_expect_path(before_rel)
        if not os.path.isfile(before_path):
            emit(idx, f"[告警] 执行前标准图不存在: {before_rel},跳过开始条件校验")
            return
        client = self._ensure_client()
        fd, tmp_path = tempfile.mkstemp(prefix="__imgloc_start_", suffix=".png")
        os.close(fd)
        try:
            client.screenshot(save_path=tmp_path)
            score = self.screen_similarity(tmp_path, before_path)
            if score >= self.expect_threshold:
                emit(idx, f"开始条件满足(相似度 {score:.2f})")
            else:
                emit(idx, f"[告警] 当前画面与执行前标准图不一致(相似度 {score:.2f}"
                          f"/{self.expect_threshold:.2f}),仍按计划点击")
        except AdbError as exc:
            emit(idx, f"[告警] 开始条件校验截图失败(继续执行): {exc}")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _execute_non_ui_step(
        self,
        client: Any,
        step: Step,
        idx: int,
        emit: Callable[[int, str], None],
    ) -> None:
        """执行一个非 UI 步骤:App 生命周期(app_*)或系统按键(keyevent)。"""
        stype = step["type"]
        if stype == KEYEVENT_STEP_TYPE:
            key = step["key"]
            emit(idx, f"发送{KEYEVENT_LABELS[key]}: {KEYEVENT_MAP[key]}")
            client.keyevent(KEYEVENT_MAP[key])
            return
        # App 操作:package 留空则执行瞬间识别当前前台 App
        pkg = step.get("package") or ""
        if not pkg:
            pkg = client.current_package()
            emit(idx, f"自动识别当前前台 App: {pkg}")
        if stype == "app_stop":
            emit(idx, f"关闭 App: {pkg}")
            client.force_stop(pkg)
        elif stype == "app_start":
            emit(idx, f"启动 App: {pkg}")
            client.launch_app(pkg)
        else:  # app_clear
            emit(idx, f"清除 App 数据: {pkg}")
            client.clear_app(pkg)

    def run(
        self,
        steps: List[Step],
        on_event: Optional[EventCallback] = None,
        wait_tick: float = 0.2,
        recorder: Optional[Any] = None,
    ) -> None:
        """
        顺序执行步骤列表。

        UI 步骤(tap/input)每步:
          1. delay_before 固定等待;
          2. 若配了 before_image,截图校验"开始条件"(不满足只告警,不阻断);
          3. 点击格子;
          4. 等待"完成条件":优先用本步 after_image,缺省则用下一步 before_image;
             轮询截图比对(超时上限 wait_after),到达立即继续,超时告警后继续;
             两者都没有时退化为固定等待 wait_after 秒。
        非 UI 步骤(app_stop/app_start/app_clear/keyevent)跳过 2~3 与锚点等待,
        只执行 delay_before -> 系统操作 -> wait_after 固定等待。

        Args:
            steps: 步骤列表(内部会再校验一次)。
            on_event: 进度回调 (当前步序号1基, 总步数, 消息文本)。
            wait_tick: 固定等待的心跳间隔(秒),用于倒计时刷新。
            recorder: 可选录制器(recording.Recorder),传入后自动采集每步前后截图;
                      异常时录制器也会收尾,再由本方法照常抛出异常。

        Raises:
            StepError: 步骤非法时抛出。
            AdbError: 点击/截图命令执行失败时抛出。
        """
        steps = validate_steps(steps)
        total = len(steps)

        def emit(idx: int, msg: str) -> None:
            logger.info("[StepRunner] (%d/%d) %s", idx, total, msg)
            if on_event is not None:
                on_event(idx, total, msg)

        try:
            for idx, step in enumerate(steps, 1):
                if step["delay_before"] > 0:
                    emit(idx, f"执行前等待 {step['delay_before']:g} 秒…")
                    self._wait(step["delay_before"], idx, emit, wait_tick)

                # 非 UI 步骤(App 生命周期/系统按键):无坐标与锚点,走独立分支
                if step["type"] in NON_UI_STEP_TYPES:
                    client = self._ensure_client()
                    if recorder is not None:
                        recorder.before_step(client, step, 0, 0)
                    self._execute_non_ui_step(client, step, idx, emit)
                    if step["wait_after"] > 0:
                        emit(idx, f"执行后等待 {step['wait_after']:g} 秒…")
                        self._wait(step["wait_after"], idx, emit, wait_tick)
                    if recorder is not None:
                        recorder.after_step(client, step)
                    continue

                # 开始条件校验(仅告警)
                if step["before_image"]:
                    self._check_start_condition(step["before_image"], idx, emit)

                cell = step["cell"]
                # 精确像素坐标(AI 定位)优先;否则按格子编号换算
                if step.get("x") is not None and step.get("y") is not None:
                    x, y = int(step["x"]), int(step["y"])
                else:
                    x, y = self.marker.ref_to_point(cell, self.image_shape)
                label = cell if cell else f"({x},{y})"
                client = self._ensure_client()
                # 录制:动作前采集一帧
                if recorder is not None:
                    recorder.before_step(client, step, x, y)

                if step["type"] == "input":
                    # 输入步骤:先点目标(聚焦输入框),再 adb 输入文本
                    text = step["text"]
                    emit(idx, f"输入 {label} -> ({x}, {y}),文本: {text!r}")
                    client.tap(x, y)
                    time.sleep(0.8)  # 等输入框聚焦/键盘弹出
                    client.text(text)
                else:
                    emit(idx, f"点击 {label} -> ({x}, {y})")
                    client.tap(x, y)

                # 完成条件:本步 after_image 优先,缺省链接到下一步的 before_image
                next_step = steps[idx] if idx < total else None
                target_rel = step["after_image"] or (
                    next_step["before_image"] if next_step else "")
                target_path = self._resolve_expect_path(target_rel) if target_rel else ""

                if target_path:
                    if not os.path.isfile(target_path):
                        emit(idx, f"[告警] 执行后标准图不存在: {target_rel},退化为固定等待")
                        target_path = ""
                    elif step["wait_after"] <= 0:
                        emit(idx, "[告警] 配置了完成条件但 wait_after=0,无等待时间,跳过画面检查")
                        target_path = ""

                if target_path:
                    reached = self._wait_for_expected_screen(
                        target_path, step["wait_after"], idx, emit)
                    if not reached:
                        emit(idx, f"[告警] 等待 {step['wait_after']:g}s 后画面仍未到达执行后标准图,"
                                  f"可能未跳转成功,继续执行下一步")
                elif step["wait_after"] > 0:
                    emit(idx, f"执行后等待 {step['wait_after']:g} 秒…")
                    self._wait(step["wait_after"], idx, emit, wait_tick)

                # 录制:等待结束后采集一帧
                if recorder is not None:
                    recorder.after_step(client, step)

            emit(total, "全部步骤执行完成")
        except BaseException as exc:
            # 异常时先收尾录制,再照常抛出(不吞异常)
            if recorder is not None:
                try:
                    recorder.fail(exc)
                    recorder.finish("failed")
                except Exception:
                    logger.exception("[StepRunner] 录制器异常收尾失败(忽略)")
            raise
        else:
            if recorder is not None:
                recorder.finish("finished")


__all__ = ["StepRunner", "StepError", "validate_steps", "load_steps", "save_steps"]
