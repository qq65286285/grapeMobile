"""
runner/run_steps.py - 步骤文件命令行执行入口
=============================================
直接执行一个脚本 JSON 文件(GUI 保存的格式),无需打开界面。

用法:
    python runner/run_steps.py                                       # 默认 runner/scripts/steps/steps.json
    python runner/run_steps.py runner/scripts/steps-1/steps-1.json   # 执行指定脚本

脚本目录约定(每个脚本一个同名文件夹,JSON 与 images/ 分置):
    runner/scripts/steps-1/steps-1.json
    runner/scripts/steps-1/images/0001.png
    runner/scripts/steps-1/images/0002.png

步骤文件格式(见 src/steps.py):
    [
        {"type": "tap", "cell": "F11", "delay_before": 0, "wait_after": 10,
         "before_image": "images/before_0001.png",
         "after_image": "images/after_0001.png"},
        {"type": "tap", "cell": "U9"}
    ]
    before_image 为执行前标准图(开始条件),after_image 为执行后标准图(完成条件),
    均相对脚本 JSON 所在目录解析。after_image 缺省时引擎自动用"下一步的
    before_image"作为本步完成条件:点击后轮询截图比对,画面一致立即继续,
    wait_after 为超时上限。标准图由 gui_steps.py 录制步骤时自动/手动保存。
    旧字段 expect_image 仍兼容(等价于 before_image)。

执行前提:runner/device_id.txt / tmp/image/grid_meta.json / tmp/image/screenshot.png 存在
(python runner/capture.py 或 gui_steps.py 已跑过一次)。
"""

from __future__ import annotations

import os
import sys

# 将 src/ 加入 sys.path(与其他 runner 脚本一致)
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

from steps import StepError, StepRunner, load_steps  # noqa: E402
from adbtools import AdbError  # noqa: E402

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STEPS_FILE = os.path.join(_BASE_DIR, "scripts", "steps", "steps.json")


def main() -> int:
    args = sys.argv[1:]
    # 默认录制并自动出片;--no-record 关闭
    record_enabled = True
    if "--no-record" in args:
        record_enabled = False
        args.remove("--no-record")
    path = args[0] if args else DEFAULT_STEPS_FILE

    try:
        steps = load_steps(path)
        runner = StepRunner.from_capture_artifacts(_BASE_DIR)
        # 标准图相对路径以脚本 JSON 所在目录为基准
        runner.script_dir = os.path.dirname(os.path.abspath(path))
    except StepError as exc:
        print(f"[ERROR] {exc}")
        return 1

    # 录制器:执行前创建,执行结束自动收尾(异常也会收尾)
    recorder = None
    if record_enabled:
        from recording import Recorder

        recorder = Recorder.create(device_id=runner.device_id)

    print("=" * 56)
    print(f"步骤文件: {os.path.abspath(path)}")
    print(f"设备: {runner.device_id} | 共 {len(steps)} 步")
    print("-" * 56)
    for i, s in enumerate(steps, 1):
        waits = []
        if s["delay_before"]:
            waits.append(f"前等 {s['delay_before']:g}s")
        if s["wait_after"]:
            # 本步或下一步带标准图时,wait_after 是"等画面跳转"的超时上限而非固定等待
            nxt = steps[i] if i < len(steps) else None
            has_target = bool(s.get("after_image")) or (
                nxt and nxt.get("before_image"))
            if has_target:
                waits.append(f"等画面≤{s['wait_after']:g}s")
            else:
                waits.append(f"后等 {s['wait_after']:g}s")
        suffix = f" ({', '.join(waits)})" if waits else ""
        marks = (" ▶" if s.get("before_image") else "") + \
                (" ⏹" if s.get("after_image") else "")
        print(f"  {i:2d}. 点击 {s['cell']}{marks}{suffix}")
    print("=" * 56)

    try:
        runner.run(
            steps,
            on_event=lambda i, n, m: print(f"[{i}/{n}] {m}"),
            recorder=recorder,
        )
    except (StepError, AdbError) as exc:
        print(f"[ERROR] 执行失败: {exc}")
        if recorder is not None:
            print(f"失败录制记录: {recorder.recording_path}")
        return 1

    # 自动渲染回放视频
    if recorder is not None:
        from replay_render import render_to_video

        print("-" * 56)
        print("正在渲染回放视频…")
        out = render_to_video(recorder.recording_path)
        print(f"回放视频: {out} ({os.path.getsize(out)/1048576:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
