"""
src/video_export.py - mp4 视频写出封装
======================================
封装 cv2.VideoWriter,把渲染好的同尺寸 BGR 帧序列编码为 mp4(mp4v)。

设计约束:
    - VideoWriter 要求所有帧尺寸完全一致;帧尺寸在构造时固定,
      渲染层(replay_render.py)负责把截图统一缩放到该画布并加黑边
    - 帧高取偶数:部分编码器对奇数高度兼容差
    - mp4v 不依赖外部 ffmpeg(本机实测可用);压缩率中等,回放场景足够

典型用法:
    >>> writer = VideoWriter("replay.mp4", size=(720, 1280), fps=30)
    >>> for frame in render_frames(recording):
    ...     writer.write(frame)
    >>> writer.close()
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger("imgloc.video")

Size = Tuple[int, int]  # (width, height)


def ensure_even(value: int) -> int:
    """向下取最近偶数。"""
    return int(value) - (int(value) % 2)


def canvas_size_for_source(
    src_width: int, src_height: int, canvas_width: int = 720
) -> Size:
    """
    按目标宽度等比计算画布尺寸,高度取偶数。

    Args:
        src_width, src_height: 源截图分辨率。
        canvas_width: 画布目标宽度,默认 720。

    Returns:
        (canvas_width, canvas_height),高度为偶数。
    """
    if src_width <= 0 or src_height <= 0:
        raise ValueError(f"源分辨率非法: {src_width}x{src_height}")
    h = ensure_even(round(canvas_width * src_height / src_width))
    # 极端宽高比下保证最小高度
    h = max(2, ensure_even(h))
    return (int(canvas_width), h)


class VideoWriter:
    """
    mp4 视频写出器。

    Args:
        path: 输出 mp4 路径。
        size: (width, height),所有写入帧必须与此一致。
        fps: 帧率,默认 30。
        fourcc: 四字符编码,默认 mp4v。

    Raises:
        RuntimeError: 编码器无法打开(尺寸/编码不被支持)时抛出。
    """

    def __init__(self, path: str, size: Size, fps: int = 30, fourcc: str = "mp4v"):
        import cv2

        self.path = os.path.abspath(path)
        self.size = size
        self.fps = fps
        self._frame_count = 0
        self._closed = False

        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        cc = cv2.VideoWriter_fourcc(*fourcc)
        self._writer = cv2.VideoWriter(self.path, cc, fps, size)
        if not self._writer.isOpened():
            raise RuntimeError(
                f"无法打开视频编码器: path={self.path} size={size} fourcc={fourcc}"
            )
        logger.info("[VideoWriter] 开始写出: %s %s @%dfps", self.path, size, fps)

    def write(self, frame) -> None:
        """
        写入一帧 BGR ndarray。

        Raises:
            ValueError: 帧尺寸与画布不一致时抛出(编程错误,应在渲染层修正)。
        """
        h, w = frame.shape[0], frame.shape[1]
        if (w, h) != self.size:
            raise ValueError(
                f"帧尺寸 {(w, h)} 与画布 {self.size} 不一致"
            )
        self._writer.write(frame)
        self._frame_count += 1

    def close(self) -> None:
        """释放编码器;重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        self._writer.release()
        logger.info("[VideoWriter] 完成: %s,共 %d 帧", self.path, self._frame_count)
        if not os.path.isfile(self.path) or os.path.getsize(self.path) == 0:
            raise RuntimeError(f"视频写出失败,文件不存在或为空: {self.path}")

    # 上下文管理器支持
    def __enter__(self) -> "VideoWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def frame_count(self) -> int:
        return self._frame_count


__all__ = ["VideoWriter", "canvas_size_for_source", "ensure_even", "Size"]
