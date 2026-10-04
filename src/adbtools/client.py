"""
src/adbtools/client.py - ADB 设备客户端
=======================================
封装 adb 命令行,提供面向 imgloc 业务场景的设备控制能力:
    - 连接网络调试设备(adb connect <ip:port>)
    - 截取当前设备屏幕并保存为本地 PNG(作为 imgloc 定位流程的"大图")

设计说明:
    - 仅依赖系统 PATH 中的 adb 可执行文件(或通过 adb_path 参数显式指定),
      不引入第三方 adb 库,保持轻量
    - 通过 subprocess 调用 adb 命令(list 形式,不启用 shell,安全且跨平台)
    - 截图采用 "shell screencap -> pull -> rm" 三段式,避免 exec-out 在
      Windows 上对 stdout 二进制流做 \\n->\\r\\n 转换导致 PNG 文件损坏
    - 命令失败时抛 AdbError,由上层决定重试/退出策略
    - 设备 id 会在 connect 后被记录,后续命令自动带 -s 参数指定目标设备,
      避免多设备时因未指定目标而报错 "more than one device/emulator"

典型流程:
    >>> client = AdbClient()
    >>> client.connect("192.168.1.100:5555")  # 1. 连接网络调试设备
    >>> client.screenshot()                    # 2. 截图保存到 runner/tmp/image/screenshot.png(大图)
    '/path/to/runner/tmp/image/screenshot.png'
    # 3. 用户手动从大图截取按钮区域,保存为 runner/tmp/image/template.png(小图)
    # 4. 运行 python runner/main.py 输出坐标 + adb tap 命令
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from typing import List, Optional, Sequence

logger = logging.getLogger("imgloc.adbtools")

# 项目根目录:src/adbtools/client.py 向上三级
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 默认截图保存路径:对接 runner/main.py 读取的 screenshot.png(设备截图)
DEFAULT_SCREENSHOT_PATH = os.path.join(_PROJECT_ROOT, "runner", "tmp", "image", "screenshot.png")


class AdbError(Exception):
    """
    adb 命令执行失败时抛出。

    典型场景:adb 可执行文件未找到、设备未连接/未响应、命令超时、
    命令返回非零状态码等。独立于 imgloc.exceptions.ImgLocError,
    使 adbtools 包可脱离 imgloc 核心库独立使用。
    """


class AdbClient:
    """
    ADB 设备客户端:封装 adb 命令,提供设备连接与截图能力。

    Args:
        adb_path: adb 可执行文件路径。默认 "adb",依赖系统 PATH。
                  Windows 下若 adb 未加入 PATH,可显式指定,
                  例如 "C:/platform-tools/adb.exe"。
        default_screenshot_path: screenshot() 未显式指定 save_path 时的默认保存路径。
                  默认 <项目根>/runner/tmp/image/screenshot.png,对接 runner/main.py 读取的大图。

    Attributes:
        device_id: 已连接的设备 id(connect 后记录,后续命令带 -s 指定目标设备)。

    示例:
        >>> client = AdbClient()
        >>> client.connect("192.168.1.100:5555")   # 连接网络调试设备
        >>> client.screenshot()                     # 截图保存到 runner/tmp/image/screenshot.png
        '/path/to/runner/tmp/image/screenshot.png'
    """

    def __init__(
        self,
        adb_path: str = "adb",
        default_screenshot_path: Optional[str] = None,
    ):
        self.adb_path = adb_path
        self.default_screenshot_path = default_screenshot_path or DEFAULT_SCREENSHOT_PATH
        self.device_id: Optional[str] = None
        # 截图串行锁:GUI 自动刷新线程与步骤执行线程可能并发截图,
        # 同一设备上的 screencap/pull 必须串行,否则会互相拉到半成品文件
        self._shot_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def connect(self, device_id: str, timeout: float = 10.0) -> bool:
        """
        连接网络调试设备:adb connect <device_id>。

        Args:
            device_id: 设备地址,形如 "ip:port"(例如 "192.168.1.100:5555")。
                       对于 USB 直连设备,通常无需调用本方法(直接调用 screenshot 即可);
                       若传入 USB 设备序列号,本方法会尝试连接并以 -s 方式记录,供后续命令使用。
            timeout: 命令超时时间(秒),默认 10 秒。

        Returns:
            True 表示连接成功(adb 输出含 "connected" 且无 "failed"/"unable")。

        Raises:
            AdbError: adb 可执行文件未找到、命令超时、返回非零状态或连接失败时抛出。
        """
        logger.info("[AdbClient] 连接设备: %s", device_id)
        # adb connect 命令本身不能用 -s(尚未建立连接),走 _run_raw
        result = self._run_raw(["connect", device_id], timeout=timeout)
        # subprocess 已用 utf-8/replace 解码,正常 stdout/stderr 均为 str;
        # 此处再防御 None,避免极端情况下拼接抛 TypeError
        output = ((result.stdout or "") + (result.stderr or "")).lower()
        ok = "connected" in output and "failed" not in output and "unable" not in output
        if not ok:
            raise AdbError(
                f"adb connect {device_id} 失败: "
                f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
            )
        self.device_id = device_id
        logger.info("[AdbClient] 设备已连接: %s", device_id)
        return True

    def list_devices(self, timeout: float = 10.0) -> List[tuple]:
        """
        枚举当前 adb 可见的设备:adb devices。

        Returns:
            [(serial, state), ...] 列表,保持 adb 输出顺序。state 常见取值:
                - "device":       在线可用
                - "offline":      离线
                - "unauthorized": 未授权(手机上未点"允许 USB 调试")
                - "recovery":     恢复模式
            无设备时返回空列表 []。

        Raises:
            AdbError: adb 不可执行 / 命令超时 / 返回非零。
        """
        logger.info("[AdbClient] 枚举设备: adb devices")
        result = self._run_raw(["devices"], timeout=timeout)
        return self.parse_devices_output(result.stdout or "")

    @staticmethod
    def parse_devices_output(output: str) -> List[tuple]:
        """
        解析 `adb devices` 文本输出为 [(serial, state), ...]。

        典型输出:
            List of devices attached
            emulator-5554   device
            192.168.1.10:5555       offline
            ABC123XYZ       unauthorized

        防御:跳过标题行/空行/以 '*' 开头的 daemon 提示行;每行按空白拆分,
        取前两段(serial, state),其余描述信息(如 usb:xxx product:xxx)忽略。
        """
        devices: List[tuple] = []
        for line in (output or "").splitlines():
            line = line.strip()
            if not line or line.startswith("*") or line.lower().startswith("list of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            devices.append((parts[0], parts[1]))
        return devices

    def attach(self, device_id: str, timeout: float = 5.0) -> bool:
        """
        挂载直连设备(模拟器 / USB):不执行 adb connect,校验在线后记录 device_id。

        模拟器(emulator-xxxx)与 USB 设备(序列号)由 adb 自动发现,
        无需(也不能)走 adb connect——那会把它当作网络主机去解析而失败。
        本方法用 adb -s <id> get-state 校验设备在线,之后截图等命令即可带 -s 定向。

        Args:
            device_id: 设备 id,模拟器形如 "emulator-5554",USB 为设备序列号。
            timeout: 校验命令超时时间(秒),默认 5 秒。

        Returns:
            True 表示设备在线且已记录。

        Raises:
            AdbError: 设备不存在/离线,或 adb 命令执行失败时抛出。
        """
        logger.info("[AdbClient] 挂载直连设备: %s", device_id)
        result = self._execute(
            [self.adb_path, "-s", device_id, "get-state"], timeout=timeout
        )
        state = (result.stdout or "").strip()
        if state != "device":
            raise AdbError(
                f"设备 {device_id} 不在线(state={state!r}),"
                f"请检查设备是否已连接/模拟器是否已启动/USB 调试是否已授权"
            )
        self.device_id = device_id
        logger.info("[AdbClient] 设备已挂载: %s", device_id)
        return True

    def screenshot(self, save_path: Optional[str] = None, timeout: float = 30.0) -> str:
        """
        截取当前设备屏幕并保存为本地 PNG 文件。

        采用 "shell screencap -> pull -> rm" 三段式,避免 exec-out 在 Windows 上
        对 stdout 二进制流做换行符转换导致 PNG 文件损坏。

        Args:
            save_path: 截图保存路径(含文件名)。None 时使用 self.default_screenshot_path
                       (默认 <项目根>/runner/tmp/image/screenshot.png,作为 imgloc 定位流程的大图,
                       对接 runner/main.py)。
            timeout: 单条 adb 子命令的超时时间(秒),默认 30 秒。

        Returns:
            保存的截图完整路径。

        Raises:
            AdbError: 任一 adb 子命令失败(设备未连接/screencap 失败/pull 失败)时抛出。
        """
        target_path = save_path or self.default_screenshot_path
        target_dir = os.path.dirname(os.path.abspath(target_path))
        os.makedirs(target_dir, exist_ok=True)

        # 每次截图使用唯一远端临时文件名:并发截图(GUI 刷新 + 执行轮询)即使
        # 绕过锁也不会因同名文件互相覆盖/误删
        remote_path = f"/sdcard/__imgloc_capture_{uuid.uuid4().hex[:12]}.png"
        logger.info("[AdbClient] 截图 -> %s", target_path)
        # 串行化完整的 screencap -> pull -> rm 流程
        with self._shot_lock:
            try:
                self._run(["shell", "screencap", "-p", remote_path], timeout=timeout)
                # pull 偶发 returncode=1(设备侧文件系统短暂繁忙),重试 2 次
                last_err: Optional[AdbError] = None
                for attempt in range(3):
                    try:
                        self._run(["pull", remote_path, target_path], timeout=timeout)
                        last_err = None
                        break
                    except AdbError as exc:
                        last_err = exc
                        logger.warning(
                            "[AdbClient] pull 失败(第 %d 次): %s", attempt + 1, exc)
                        time.sleep(0.3)
                if last_err is not None:
                    raise last_err
            finally:
                # 无论 pull 成功与否,都尝试清理本次远端临时文件
                try:
                    self._run(["shell", "rm", "-f", remote_path], timeout=timeout)
                except AdbError as exc:
                    logger.warning("[AdbClient] 清理远端临时文件失败(可忽略): %s", exc)

        if not os.path.isfile(target_path) or os.path.getsize(target_path) == 0:
            raise AdbError(f"截图保存失败,文件未生成或为空: {target_path}")
        logger.info("[AdbClient] 截图完成: %s", target_path)
        return target_path

    def tap(self, x: int, y: int, timeout: float = 10.0) -> None:
        """
        在已连接设备上点击坐标 (x, y):adb -s <device_id> shell input tap x y。

        Args:
            x, y: 设备屏幕像素坐标。
            timeout: 命令超时时间(秒),默认 10 秒。

        Raises:
            AdbError: 尚未 connect/attach(无 device_id),或命令执行失败时抛出。
        """
        if not self.device_id:
            raise AdbError("尚未连接设备,请先调用 connect() 或 attach()")
        logger.info("[AdbClient] 点击 (%d, %d) @ %s", x, y, self.device_id)
        self._run(["shell", "input", "tap", str(x), str(y)], timeout=timeout)

    def text(self, text: str, timeout: float = 15.0) -> None:
        """
        在当前焦点输入框输入文本:adb -s <device_id> shell input text <text>。

        说明:
            - 系统 `input text` 仅支持 ASCII 可见字符;空格用 %s 转义;
            - 中文等非 ASCII 字符设备端会被忽略,需借助 ADBKeyBoard 等
              输入法方案(本项目暂未集成),建议仅用于英文/数字/符号输入;
            - 特殊字符 & | ; $ < > 等经单引号包裹 + 转义后传递,防止被设备 shell 解释。

        Args:
            text: 要输入的文本(空串直接返回,不发命令)。
            timeout: 命令超时时间(秒),默认 15 秒。

        Raises:
            AdbError: 尚未 connect/attach(无 device_id),或命令执行失败时抛出。
        """
        if not self.device_id:
            raise AdbError("尚未连接设备,请先调用 connect() 或 attach()")
        if not text:
            return
        # input text 约定:空格必须写成 %s
        escaped = text.replace(" ", "%s")
        # 单引号包裹,内部单引号按 POSIX 规则转义为 '\'' ,防设备 shell 展开 $ & | ; 等
        escaped = "'" + escaped.replace("'", "'\\''") + "'"
        logger.info("[AdbClient] 输入文本 (%d 字符) @ %s", len(text), self.device_id)
        self._run(["shell", "input", "text", escaped], timeout=timeout)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _run(self, args: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        """
        执行 adb 子命令,已连接设备时自动带 -s <device_id>。

        Raises:
            AdbError: 命令返回非零状态、超时或 adb 不可执行时抛出。
        """
        cmd: List[str] = [self.adb_path]
        if self.device_id:
            cmd += ["-s", self.device_id]
        cmd += list(args)
        return self._execute(cmd, timeout=timeout)

    def _run_raw(self, args: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        """
        执行 adb 子命令,不带 -s(用于 connect 等尚未建立连接的命令)。

        Raises:
            AdbError: 命令返回非零状态、超时或 adb 不可执行时抛出。
        """
        cmd: List[str] = [self.adb_path] + list(args)
        return self._execute(cmd, timeout=timeout)

    @staticmethod
    def _execute(cmd: List[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        """
        实际执行 adb 命令并统一处理错误。

        Args:
            cmd: 完整命令列表(含 adb_path 与所有参数)。
            timeout: 命令超时时间(秒)。

        Returns:
            subprocess.CompletedProcess: 成功完成的子进程结果对象。

        Raises:
            AdbError: adb 未找到 / 超时 / 返回非零状态时抛出,错误信息含完整命令与输出。
        """
        logger.debug("[AdbClient] 执行命令: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",      # adb 输出按 utf-8 解码,避免中文 Windows 默认 GBK 解码崩溃
                errors="replace",      # 遇非法字节用替换符,绝不抛 UnicodeDecodeError
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise AdbError(
                f"adb 可执行文件未找到: {cmd[0]!r}(请检查 adb_path 参数或系统 PATH)"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise AdbError(
                f"adb 命令超时({timeout}s): {' '.join(cmd)}"
            ) from e
        if result.returncode != 0:
            raise AdbError(
                f"adb 命令失败(returncode={result.returncode}): {' '.join(cmd)}\n"
                f"stdout: {(result.stdout or '').strip()}\n"
                f"stderr: {(result.stderr or '').strip()}"
            )
        return result
