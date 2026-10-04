"""
src/replay_render.py - 录制数据离屏渲染为回放帧
==================================================
把 recording.json 渲染成统一画布的 BGR 帧序列:
    - 每步:执行前画面静止 -> 触控圆点 + 波纹扩散 -> 新旧画面 crossfade -> 执行后静止
    - 底部半透明字幕条显示步骤描述(中文,微软雅黑;字体缺失时降级为不绘文字)
    - 语义时长:等待的墙钟时长不进视频(执行后画面已包含等待结果),避免长等待拖长视频

移植自 midscene 的 export-branded-video.ts 思路,但用触控波纹代替鼠标指针,
纯 Python(cv2 + PIL)实现,无外部 ffmpeg/无新增依赖。

入口:
    render_frames(recording)              -> Iterator[np.ndarray]
    render_to_video(recording_path, ...)   -> 直接产出 mp4
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

import cv2
import numpy as np

from video_export import VideoWriter, canvas_size_for_source

logger = logging.getLogger("imgloc.replay_render")

# ── 时序(帧,30fps)──────────────────────────────
HOLD_BEFORE_FRAMES = 9   # 300ms 执行前静止
RIPPLE_FRAMES = 14        # ~467ms 波纹扩散
CROSSFADE_FRAMES = 10     # ~333ms 新旧画面淡入淡出
HOLD_AFTER_FRAMES = 12    # 400ms 执行后静止
FAIL_HOLD_FRAMES = 36     # 1.2s 失败停留
END_FRAMES = 15            # 0.5s 结尾黑场

# ── 视觉参数 ─────────────────────────────────────
SUBTITLE_BAR_HEIGHT = 56
SUBTITLE_ALPHA = 0.55
TOUCH_RADIUS = 11
RIPPLE_MIN_R = 15.0
RIPPLE_MAX_R = 70.0
RIPPLE_COLOR_BGR = (7, 89, 253)   # #FD5907 的 BGR
TOUCH_DOT_BGR = (7, 89, 253)
FAIL_COLOR_BGR = (54, 54, 232)     # 暗红 #E83636 的 BGR

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)
_font_cache: Dict[int, Any] = {}
_pil_available: Optional[bool] = None


# ── 录制加载 ─────────────────────────────────────

def load_recording(path: str) -> Dict[str, Any]:
    """
    读取 recording.json。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: JSON 非法或不含事件。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"录制文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"录制文件 JSON 解析失败: {exc}") from exc
    if not isinstance(data.get("events"), list):
        raise ValueError("录制文件缺少 events 列表")
    return data


def _first_frame_info(recording: Dict[str, Any]) -> Tuple[int, int]:
    """从事件中找第一帧的源分辨率,用于确定画布尺寸。"""
    for ev in recording["events"]:
        for key in ("before", "after"):
            fr = ev.get(key)
            if fr and fr.get("width") and fr.get("height"):
                return int(fr["width"]), int(fr["height"])
    raise ValueError("录制数据中没有任何有效截图帧,无法渲染")


# ── 帧素材 ─────────────────────────────────────

class _FrameStore:
    """按相对路径加载截图,缩放至画布尺寸并缓存。"""

    def __init__(self, base_dir: str, canvas: Tuple[int, int]):
        self.base_dir = base_dir
        self.canvas = canvas  # (w, h)
        self._cache: Dict[str, Optional[np.ndarray]] = {}

    def get(self, frame_info: Optional[Dict[str, Any]]) -> Optional[np.ndarray]:
        if not frame_info or not frame_info.get("frame"):
            return None
        rel = frame_info["frame"]
        if rel not in self._cache:
            path = os.path.join(self.base_dir, rel.replace("/", os.sep))
            img = None
            if os.path.isfile(path):
                # 经 numpy 解码,兼容中文路径
                data = np.fromfile(path, dtype=np.uint8)
                decoded = cv2.imdecode(data, cv2.IMREAD_COLOR)
                if decoded is not None:
                    img = cv2.resize(decoded, self.canvas, interpolation=cv2.INTER_AREA)
            if img is None:
                logger.warning("[render] 帧文件丢失或解码失败: %s", path)
            self._cache[rel] = img
        return self._cache[rel]


# ── 字幕(PIL)──────────────────────────────────

def _load_font(size: int) -> Optional[Any]:
    global _pil_available
    if _pil_available is False:
        return None
    if size in _font_cache:
        return _font_cache[size]
    try:
        from PIL import ImageFont
    except ImportError:
        _pil_available = False
        return None
    for path in _FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[size] = font
                return font
            except Exception:
                continue
    logger.info("[render] 未找到可用中文字体,字幕将不显示文字")
    _font_cache[size] = None
    return None


def _truncate_to_width(text: str, font: Any, max_width: int) -> str:
    """按像素宽度截断文字,超出加省略号。"""
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    if probe.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    out = text
    while out and probe.textlength(out + ellipsis, font=font) > max_width:
        out = out[:-1]
    return out + ellipsis


def draw_subtitle(
    frame: np.ndarray,
    text: str,
    color_bgr: Tuple[int, int, int] = (255, 255, 255),
) -> np.ndarray:
    """
    在帧底部绘制半透明字幕条 + 文字(原地修改并返回 frame)。
    文字为空或 PIL/字体不可用时只不画文字,字幕条仍按需要调用方决定。
    """
    h = frame.shape[0]
    y0 = h - SUBTITLE_BAR_HEIGHT

    # 半透明黑条
    overlay = frame
    bar = np.zeros_like(frame[y0:h])
    blended = cv2.addWeighted(frame[y0:h], 1 - SUBTITLE_ALPHA, bar, SUBTITLE_ALPHA, 0)
    frame[y0:h] = blended

    text = (text or "").strip()
    if text:
        font = _load_font(24)
        if font is not None:
            from PIL import Image, ImageDraw

            # BGR -> RGB -> PIL
            strip = frame[y0:h]
            rgb = cv2.cvtColor(strip, cv2.COLOR_BGR2RGB)
            im = Image.fromarray(rgb)
            draw = ImageDraw.Draw(im)
            max_w = frame.shape[1] - 32
            text = _truncate_to_width(text, font, max_w)
            # 文字在条内垂直居中:按字体 bbox 估算
            bbox = draw.textbbox((0, 0), text, font=font)
            text_h = bbox[3] - bbox[1]
            ty = (SUBTITLE_BAR_HEIGHT - text_h) // 2 - bbox[1]
            draw.text((16, ty), text, font=font, fill=(color_bgr[2], color_bgr[1], color_bgr[0]))
            frame[y0:h] = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
    return frame


# ── 触控绘制 ───────────────────────────────────

def _map_point(ev: Dict[str, Any], frame_info: Dict[str, Any], canvas: Tuple[int, int]) -> Tuple[int, int]:
    scale = canvas[0] / int(frame_info["width"])
    return int(round(int(ev["x"]) * scale)), int(round(int(ev["y"]) * scale))


def draw_touch_dot(frame: np.ndarray, x: int, y: int, alpha: float = 1.0) -> None:
    """在 (x,y) 绘制触控圆点(白底橙心)。alpha<1 时用混合绘制。"""
    if alpha >= 1.0:
        cv2.circle(frame, (x, y), TOUCH_RADIUS + 2, (255, 255, 255), -1, lineType=cv2.LINE_AA)
        cv2.circle(frame, (x, y), TOUCH_RADIUS, TOUCH_DOT_BGR, -1, lineType=cv2.LINE_AA)
        return
    overlay = frame.copy()
    cv2.circle(overlay, (x, y), TOUCH_RADIUS + 2, (255, 255, 255), -1, lineType=cv2.LINE_AA)
    cv2.circle(overlay, (x, y), TOUCH_RADIUS, TOUCH_DOT_BGR, -1, lineType=cv2.LINE_AA)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)


def draw_ripple(frame: np.ndarray, x: int, y: int, progress: float) -> None:
    """
    绘制一帧波纹:半径随 progress 扩大,透明度随 progress 衰减。
    progress ∈ [0,1]。
    """
    radius = int(round(RIPPLE_MIN_R + (RIPPLE_MAX_R - RIPPLE_MIN_R) * progress))
    alpha = 0.85 * (1.0 - progress)
    if alpha <= 0.02:
        return
    overlay = frame.copy()
    cv2.circle(overlay, (x, y), radius, RIPPLE_COLOR_BGR, 3, lineType=cv2.LINE_AA)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)


# ── 事件渲染 ───────────────────────────────────

def _subtitle_text(ev: Dict[str, Any]) -> str:
    """字幕文案:优先步骤描述,否则用动作坐标兜底。"""
    desc = str(ev.get("desc", "") or "").strip()
    if desc:
        return desc
    return f"点击 ({ev.get('x')}, {ev.get('y')})"


def _render_tap_event(
    ev: Dict[str, Any], store: _FrameStore, canvas: Tuple[int, int]
) -> Iterator[np.ndarray]:
    before_img = store.get(ev.get("before"))
    after_img = store.get(ev.get("after"))
    subtitle = _subtitle_text(ev)

    # 执行失败:红条停留,不做波纹/跳转
    if ev.get("status") == "failed" or ev.get("type") == "error":
        base = before_img if before_img is not None else _black(canvas)
        for _ in range(FAIL_HOLD_FRAMES):
            fr = base.copy()
            draw_subtitle(fr, f"执行失败: {ev.get('error') or subtitle}", color_bgr=(70, 70, 255))
            yield fr
        return

    # 缺帧兜底(前/后截图丢失):停留展示,不做动画
    if before_img is None and after_img is None:
        for _ in range(HOLD_BEFORE_FRAMES + RIPPLE_FRAMES + HOLD_AFTER_FRAMES):
            yield _black(canvas)
        return
    if before_img is None:
        before_img = after_img
    if after_img is None:
        # 执行后截图丢失:执行前画面 + 完整停留,不做 crossfade
        pt = _map_point(ev, ev["before"], canvas) if ev.get("before") else None
        for _ in range(HOLD_BEFORE_FRAMES):
            fr = before_img.copy()
            draw_subtitle(fr, subtitle)
            yield fr
        for i in range(RIPPLE_FRAMES):
            fr = before_img.copy()
            if pt is not None:
                draw_touch_dot(fr, *pt)
                draw_ripple(fr, *pt, (i + 1) / RIPPLE_FRAMES)
            draw_subtitle(fr, subtitle)
            yield fr
        for _ in range(HOLD_AFTER_FRAMES + CROSSFADE_FRAMES):
            fr = before_img.copy()
            draw_subtitle(fr, subtitle)
            yield fr
        return

    pt = _map_point(ev, ev.get("before") or ev.get("after"), canvas)

    # 1. 执行前静止
    for _ in range(HOLD_BEFORE_FRAMES):
        fr = before_img.copy()
        draw_subtitle(fr, subtitle)
        yield fr

    # 2. 触控点 + 波纹
    for i in range(RIPPLE_FRAMES):
        fr = before_img.copy()
        draw_touch_dot(fr, *pt)
        draw_ripple(fr, *pt, (i + 1) / RIPPLE_FRAMES)
        draw_subtitle(fr, subtitle)
        yield fr

    # 3. crossfade;前半段圆点淡出
    for i in range(CROSSFADE_FRAMES):
        t = i / (CROSSFADE_FRAMES - 1)
        fr = cv2.addWeighted(after_img, t, before_img, 1 - t, 0)
        if i < 5:
            draw_touch_dot(fr, *pt, alpha=1.0 - i / 5)
        draw_subtitle(fr, subtitle)
        yield fr

    # 4. 执行后静止
    for _ in range(HOLD_AFTER_FRAMES):
        fr = after_img.copy()
        draw_subtitle(fr, subtitle)
        yield fr


def _black(canvas: Tuple[int, int]) -> np.ndarray:
    return np.zeros((canvas[1], canvas[0], 3), dtype=np.uint8)


# ── 公开入口 ───────────────────────────────────

def render_frames(recording: Dict[str, Any]) -> Iterator[np.ndarray]:
    """
    把录制数据渲染为统一画布的 BGR 帧序列(生成器)。

    Args:
        recording: load_recording 得到的 dict。

    Yields:
        np.ndarray(BGR,画布尺寸 720 宽,高度按源比例)。
    """
    src_w, src_h = _first_frame_info(recording)
    canvas_w = int(recording.get("canvas_width", 720) or 720)
    canvas = canvas_size_for_source(src_w, src_h, canvas_w)
    base_dir = os.path.dirname(os.path.abspath(recording.get("_path", "recording.json")))
    store = _FrameStore(base_dir, canvas)

    for ev in recording["events"]:
        if ev.get("type") in ("tap", "error"):
            yield from _render_tap_event(ev, store, canvas)

    # 结尾黑场
    for _ in range(END_FRAMES):
        yield _black(canvas)


def render_to_video(
    recording_path: str,
    out_path: Optional[str] = None,
    fps: Optional[int] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """
    从 recording.json 直接渲染并编码为 mp4。

    Args:
        recording_path: recording.json 路径。
        out_path: 输出 mp4 路径;默认与 recording.json 同目录下 replay.mp4。
        fps: 帧率;默认取录制元数据(30)。
        progress: 可选进度回调 (frames_written, total_estimate),total 为预估。

    Returns:
        产出的 mp4 完整路径。
    """
    recording = load_recording(recording_path)
    recording["_path"] = os.path.abspath(recording_path)
    rate = int(fps or recording.get("fps", 30) or 30)
    out = out_path or os.path.join(os.path.dirname(os.path.abspath(recording_path)), "replay.mp4")

    src_w, src_h = _first_frame_info(recording)
    canvas = canvas_size_for_source(src_w, src_h, int(recording.get("canvas_width", 720) or 720))
    # 预估总帧数,仅用于进度展示
    n_events = len(recording["events"])
    estimated = n_events * (
        HOLD_BEFORE_FRAMES + RIPPLE_FRAMES + CROSSFADE_FRAMES + HOLD_AFTER_FRAMES
    ) + END_FRAMES

    written = 0
    with VideoWriter(out, size=canvas, fps=rate) as writer:
        for frame in render_frames(recording):
            writer.write(frame)
            written += 1
            if progress is not None:
                progress(written, estimated)
    logger.info("[render] 回放视频已生成: %s (%d 帧)", out, written)
    return out


__all__ = [
    "load_recording",
    "render_frames",
    "render_to_video",
]
