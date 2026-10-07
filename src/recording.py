"""
src/recording.py - 步骤执行录制器
==================================
在步骤执行过程中采集"每步执行前/后截图 + 点击坐标 + 描述",落盘为自描述的
录制目录,供离屏渲染成回放视频(见 replay_render.py)。

录制产物布局(runner/runs/<run-id>/):
    recording.json        动作序列与帧索引(本模块自有格式,不耦合 midscene dump)
    frames/00001.jpg      设备截图(jpg q90,比 PNG 小一个量级)

设计说明:
    - 与执行解耦:Recorder 只被动接收钩子调用,不改变步骤执行语义;
      截图失败只告警不阻断主流程(录制是附属能力,不能拖垮执行)
    - 每个 Device/执行会话使用独立的 Recorder 与独立运行目录
    - 截图经由 AdbClient 完成,统一走 adb,无新增依赖

典型用法:
    >>> rec = Recorder.create(device_id="emulator-5554")
    >>> rec.before_step(client, step, x, y)
    >>> rec.after_step(client, step)
    >>> rec.finish()                # 写出 recording.json
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import cvio

logger = logging.getLogger("imgloc.recording")

# 项目根目录:src/recording.py 向上两级
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RUNS_DIR = os.path.join(_PROJECT_ROOT, "runner", "runs")

RECORDING_VERSION = 1
DEFAULT_FPS = 30
CANVAS_WIDTH = 720
JPG_QUALITY = 90


def new_run_id(now: Optional[float] = None) -> str:
    """生成运行 id:YYYYMMDD-HHMMSS-<4位随机>。"""
    t = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    return f"{t}-{uuid.uuid4().hex[:4]}"


class Recorder:
    """
    步骤录制器:绑定一个运行目录,记录一次步骤执行的完整过程。

    Args:
        run_dir: 本次运行目录(recording.json 与 frames/ 的父目录)。
        device_id: 设备 id,仅作元数据记录。
        fps: 回放帧率元数据,默认 30。
        canvas_width: 回放画布宽度元数据,默认 720。

    典型用法见模块说明。用 Recorder.create() 构造可自动创建带时间戳的目录。
    """

    def __init__(
        self,
        run_dir: str,
        device_id: Optional[str] = None,
        fps: int = DEFAULT_FPS,
        canvas_width: int = CANVAS_WIDTH,
    ):
        self.run_dir = os.path.abspath(run_dir)
        self.device_id = device_id
        self.fps = fps
        self.canvas_width = canvas_width
        self.frames_dir = os.path.join(self.run_dir, "frames")
        self.run_id = os.path.basename(self.run_dir.rstrip(os.sep))
        self.created_at = time.time()

        self._events: List[Dict[str, Any]] = []
        self._frame_seq = 0
        self._current: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        device_id: Optional[str] = None,
        runs_dir: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> "Recorder":
        """
        在 runs_dir 下新建一个带时间戳的运行目录并构造 Recorder。

        Args:
            device_id: 设备 id(元数据)。
            runs_dir: 运行根目录,默认 <项目根>/runner/runs。
            run_id: 自定义运行 id,缺省按时间戳生成。

        Returns:
            Recorder 实例(目录与 frames/ 子目录已创建)。
        """
        base = runs_dir or DEFAULT_RUNS_DIR
        rid = run_id or new_run_id()
        run_dir = os.path.join(base, rid)
        os.makedirs(os.path.join(run_dir, "frames"), exist_ok=True)
        rec = cls(run_dir, device_id=device_id)
        logger.info("[Recorder] 开始录制: %s", run_dir)
        return rec

    # ------------------------------------------------------------------
    # 帧采集
    # ------------------------------------------------------------------
    def capture_frame(self, client: Any) -> Optional[Dict[str, Any]]:
        """
        通过 client 截一张设备截图,转存 jpg 到 frames/,返回帧描述。

        Args:
            client: 具备 screenshot(save_path=...) 方法的 AdbClient(或兼容对象)。

        Returns:
            {"frame": "frames/00001.jpg", "width": int, "height": int};
            截图/编码失败时返回 None(只告警,不抛)。
        """
        self._frame_seq += 1
        seq = self._frame_seq
        png_path = os.path.join(self.frames_dir, f"_raw_{seq:05d}.png")
        jpg_name = f"{seq:05d}.jpg"
        jpg_path = os.path.join(self.frames_dir, jpg_name)
        try:
            client.screenshot(save_path=png_path)
            img = cvio.imread(png_path)
            if img is None:
                raise RuntimeError(f"截图读取失败: {png_path}")
            height, width = int(img.shape[0]), int(img.shape[1])
            # cv2 jpg 编码质量参数;imencode 后由 cvio 落盘(兼容中文路径)
            ok, buf = _encode_jpg(img)
            if not ok:
                raise RuntimeError("jpg 编码失败")
            with open(jpg_path, "wb") as f:
                f.write(buf.tobytes())
            return {
                "frame": f"frames/{jpg_name}",
                "width": width,
                "height": height,
            }
        except Exception as exc:  # 录制失败不阻断执行
            logger.warning("[Recorder] 采集第 %d 帧失败(忽略): %s", seq, exc)
            return None
        finally:
            try:
                if os.path.isfile(png_path):
                    os.remove(png_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 步骤钩子(由 StepRunner.run 调用)
    # ------------------------------------------------------------------
    def before_step(
        self, client: Any, step: Dict[str, Any], x: int, y: int
    ) -> None:
        """
        一步开始:采集执行前截图,开启一个 tap 事件。

        Args:
            client: AdbClient。
            step: 规范化后的步骤(取 desc 等)。
            x, y: 本步实际点击的像素坐标。
        """
        before = self.capture_frame(client)
        # 事件类型跟步骤走:tap / input / app_stop / app_start / app_clear / keyevent
        # (回放据此决定是否画点击标记)
        self._current = {
            "type": str(step.get("type", "tap")),
            "index": len(self._events) + 1,
            "desc": str(step.get("desc", "") or ""),
            "x": int(x),
            "y": int(y),
            "before": before,
            "after": None,
            "status": "pending",
            "error": None,
            "wait_after": float(step.get("wait_after", 0) or 0),
            # 非 UI 步骤专有参数(回放叠加文字用)
            "package": str(step.get("package", "") or ""),
            "key": str(step.get("key", "") or ""),
        }

    def after_step(self, client: Any, _step: Dict[str, Any]) -> None:
        """一步结束:采集执行后截图,收尾当前 tap 事件并入列。"""
        if self._current is None:
            return
        self._current["after"] = self.capture_frame(client)
        self._current["status"] = "finished"
        self._events.append(self._current)
        self._current = None

    def fail(self, error: BaseException) -> None:
        """
        执行异常:把当前步骤标记为失败并入列(不重新抛出,由调用方处理异常)。
        """
        msg = str(error) or error.__class__.__name__
        if self._current is not None:
            self._current["status"] = "failed"
            self._current["error"] = msg
            self._events.append(self._current)
            self._current = None
        else:
            # 异常发生在两步骤之间,记录一个独立失败标记
            self._events.append({
                "type": "error",
                "index": len(self._events) + 1,
                "desc": "",
                "status": "failed",
                "error": msg,
            })

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def finish(self, status: str = "finished") -> str:
        """
        写出 recording.json。

        Args:
            status: 整体执行状态(finished / failed)。

        Returns:
            recording.json 的完整路径。
        """
        # 防御:存在未收尾事件(正常流程不会)时强制入列
        if self._current is not None:
            self._current["status"] = status
            self._events.append(self._current)
            self._current = None

        data = {
            "version": RECORDING_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "device_id": self.device_id,
            "fps": self.fps,
            "canvas_width": self.canvas_width,
            "status": status,
            "event_count": len(self._events),
            "events": self._events,
        }
        path = os.path.join(self.run_dir, "recording.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(
            "[Recorder] 录制结束(%s): %s,共 %d 个事件",
            status, path, len(self._events),
        )
        return path

    @property
    def recording_path(self) -> str:
        """recording.json 的预期路径(无论是否已写出)。"""
        return os.path.join(self.run_dir, "recording.json")


def _encode_jpg(img: Any):
    """cv2 编码 jpg(质量 JPG_QUALITY),返回 (ok, buf)。延迟导入 cv2。"""
    import cv2

    params = [int(cv2.IMWRITE_JPEG_QUALITY), JPG_QUALITY]
    return cv2.imencode(".jpg", img, params)


__all__ = ["Recorder", "new_run_id", "DEFAULT_RUNS_DIR", "DEFAULT_FPS", "CANVAS_WIDTH"]
