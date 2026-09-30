"""
src/aiclient.py - AI API 客户端(OpenAI 兼容)
================================================
轻量封装,仅依赖标准库 urllib,不引入 requests/openai 等第三方包。

支持:
    - 纯文本对话
    - 图片输入(vision):传入图片文件路径或 base64 编码,自动转为 data URL

用法:
    >>> from aiclient import AIClient
    >>> ai = AIClient()
    >>> ai.chat("你好")
    '你好！有什么可以帮你的？'
    >>> ai.chat_with_image("截图里有什么按钮?", "runner/tmp/image/screenshot.png")
    '画面中可以看到"登录"、"设置"等按钮...'
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Union

# ---- 配置(可按需修改或通过构造参数覆盖) ----
DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
DEFAULT_API_KEY = "sk-JVlPWd1Mjt8sjzNKciiQrsDIEFSO1rxvWEuMzYv9rNNuYEAu"
DEFAULT_MODEL = "agnes-3.0-flash"
DEFAULT_TIMEOUT = 120  # 秒(vision 请求图片大,需留足时间)
DEFAULT_MAX_TOKENS = 2048


class AIError(Exception):
    """AI API 调用异常。"""


class AIClient:
    """OpenAI 兼容 API 的轻量客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = DEFAULT_API_KEY,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------
    def _call_chat(self, messages: List[Dict[str, Any]], **kwargs: Any) -> str:
        """发送 chat completions 请求,返回 assistant 回复文本。"""
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }
        if "temperature" in kwargs:
            payload["temperature"] = kwargs["temperature"]

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise AIError(f"API 返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AIError(f"网络请求失败: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise AIError(f"响应 JSON 解析失败: {exc}") from exc

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise AIError(f"响应结构异常: {body}") from exc

    # ------------------------------------------------------------------
    # 图片编码工具
    # ------------------------------------------------------------------
    @staticmethod
    def _encode_image(path: str, max_width: int = 768, quality: int = 85) -> str:
        """
        读取图片文件,压缩后编码为 data URL(base64)。
        用 cv2 压缩为 JPEG 并限制宽度,大幅减小 payload 加速 API 响应。
        cv2 不可用时回退为原始文件读取。
        """
        if not os.path.isfile(path):
            raise AIError(f"图片文件不存在: {path}")

        # 尝试用 cv2 压缩
        try:
            import cv2
            import numpy as np
            img = cv2.imread(path)
            if img is not None:
                h, w = img.shape[:2]
                if w > max_width:
                    scale = max_width / w
                    img = cv2.resize(img, (max_width, int(h * scale)),
                                     interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", img,
                                       [cv2.IMWRITE_JPEG_QUALITY, quality])
                if ok:
                    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                    return f"data:image/jpeg;base64,{b64}"
        except Exception:
            pass  # cv2 不可用,回退到原始读取

        # 回退:原始文件直接 base64
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mime = "image/png" if ext in ("png",) else "image/jpeg"
        return f"data:{mime};base64,{b64}"

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def chat(
        self,
        prompt: str,
        system: str = "",
        history: Optional[List[Dict[str, str]]] = None,
        **kwargs: Any,
    ) -> str:
        """
        纯文本对话。

        Args:
            prompt:    用户输入。
            system:    可选 system prompt。
            history:   可选历史消息列表,格式 [{"role":"user","content":"..."},
                       {"role":"assistant","content":"..."}, ...]。
        Returns:
            assistant 回复文本。
        """
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        return self._call_chat(messages, **kwargs)

    def chat_with_image(
        self,
        prompt: str,
        image_path: str,
        system: str = "",
        max_width: int = 768,
        quality: int = 85,
        **kwargs: Any,
    ) -> str:
        """
        带图片的对话(vision)。图片自动压缩(JPEG,限宽)后发送。

        Args:
            prompt:      针对图片的提问。
            image_path:  图片文件路径(PNG/JPG)。
            system:      可选 system prompt。
            max_width:   压缩后最大宽度(像素),默认 768。
            quality:     JPEG 质量(1-100),默认 85。
        Returns:
            assistant 回复文本。
        """
        data_url = self._encode_image(image_path, max_width, quality)
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        })
        return self._call_chat(messages, **kwargs)
