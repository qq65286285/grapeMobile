"""
runner/replay.py - 历史录制回放渲染 CLI
=======================================
对一次已完成的运行记录(runner/runs/<run-id>/recording.json)离屏渲染,
产出 replay.mp4。用于:
    - 对历史记录重新出片(调整渲染逻辑后无需重跑设备流程)
    - 执行时未自动出片的补渲染

用法:
    python runner/replay.py <run-id>
    python runner/replay.py runner/runs/<run-id>
    python runner/replay.py path/to/recording.json

    示例:
        python runner/replay.py 20261002-153012-a1b2

不连接设备,只读取录制目录。
"""

from __future__ import annotations

import os
import sys

# 将 src/ 加入 sys.path(与其他 runner 脚本一致)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(_BASE_DIR, "..", "src")
sys.path.insert(0, os.path.abspath(_SRC_DIR))

from replay_render import render_to_video  # noqa: E402

_RUNS_DIR = os.path.join(_BASE_DIR, "runs")


def resolve_recording_path(arg: str) -> str:
    """
    把用户输入解析为 recording.json 的完整路径,支持三种形态:
        - run-id(在 runner/runs/ 下查找)
        - 运行目录(含 recording.json)
        - recording.json 文件路径

    Raises:
        FileNotFoundError: 无法定位到 recording.json。
    """
    # 直接给文件
    if os.path.isfile(arg):
        return os.path.abspath(arg)

    # 给目录
    candidate_dir = arg
    if not os.path.isabs(candidate_dir):
        # 相对路径先按 cwd,再按 runner/runs/<arg> 兜底
        if not os.path.isdir(candidate_dir):
            candidate_dir = os.path.join(_RUNS_DIR, arg)
    rec = os.path.join(candidate_dir, "recording.json")
    if os.path.isfile(rec):
        return os.path.abspath(rec)
    raise FileNotFoundError(
        f"找不到录制记录: {arg}\n"
        f"已尝试: 文件路径 / 目录 {os.path.abspath(arg)} / "
        f"{os.path.join(_RUNS_DIR, arg)}"
    )


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python runner/replay.py <run-id | 运行目录 | recording.json>")
        return 2

    try:
        rec_path = resolve_recording_path(sys.argv[1])
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 1

    print("=" * 56)
    print(f"录制记录: {rec_path}")

    last_pct = -1

    def on_progress(done: int, total: int) -> None:
        nonlocal last_pct
        pct = min(100, int(done * 100 / max(total, 1)))
        if pct != last_pct and pct % 5 == 0:
            last_pct = pct
            print(f"渲染进度: {pct}% ({done}/{total})")

    try:
        out = render_to_video(rec_path, progress=on_progress)
    except Exception as exc:
        print(f"[ERROR] 渲染失败: {exc}")
        return 1

    size_mb = os.path.getsize(out) / (1024 * 1024)
    print("-" * 56)
    print(f"回放视频已生成: {out}")
    print(f"大小: {size_mb:.2f} MB")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
