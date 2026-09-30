"""
src/executor.py - adb 执行类(业务编排层)
=========================================
封装 adbtools.AdbClient 的调用流程,提供面向业务的"连接设备 + 截图"编排。

分层位置:
    - src/adbtools/        adb 工具层(只管 adb 命令)
    - src/executor.py      业务编排层(本文件,组合 adbtools 完成 connect+screenshot)
    - src/service.py       imgloc 定位服务层
    - runner/              入口层(capture.py 截图入口 / main.py 定位入口)

本文件只定义 AdbExecutor 类,不含可执行入口(无 DEVICE_ID / 无 main 块)。
直接执行入口见 runner/capture.py,通过 import 本模块的 AdbExecutor 完成截图流程。

典型用法(被入口调用):
    >>> from executor import AdbExecutor
    >>> executor = AdbExecutor("192.168.1.100:5555")
    >>> executor.run()                # 连接 + 截图到 runner/tmp/image/screenshot.png
    '/path/to/runner/tmp/image/screenshot.png'
    # 或分步调用:
    >>> executor.connect()
    >>> executor.screenshot()
"""

from __future__ import annotations

import logging

from adbtools import AdbClient, AdbError

logger = logging.getLogger("imgloc.executor")


class AdbExecutor:
    """
    adb 执行类:封装"连接设备 + 截图"流程,用户只需传入 device_id。

    Args:
        device_id: 设备地址。网络调试形如 "ip:port"(例如 "192.168.1.100:5555"),
                   USB 直连为设备序列号,模拟器形如 "emulator-5554"。
        adb_path:  adb 可执行文件路径。默认 "adb"(依赖系统 PATH);
                   Windows 下未加入 PATH 时可显式指定,如 "C:/platform-tools/adb.exe"。

    Attributes:
        device_id: 已传入的设备 id。

    示例:
        >>> executor = AdbExecutor("192.168.1.100:5555")
        >>> executor.run()     # 一步到位:连接 + 截图
        '/path/to/runner/tmp/image/screenshot.png'
    """

    def __init__(self, device_id: str, adb_path: str = "adb"):
        self.device_id = device_id
        self._client = AdbClient(adb_path=adb_path)
        logger.debug("[AdbExecutor] 初始化: device_id=%s adb_path=%s", device_id, adb_path)

    def connect(self) -> bool:
        """
        连接设备:委托 AdbClient.connect(device_id)。

        Returns:
            True 表示连接成功。

        Raises:
            AdbError: adb 未找到 / 超时 / 返回非零状态或连接失败时抛出。
        """
        logger.info("[AdbExecutor] 连接设备: %s", self.device_id)
        return self._client.connect(self.device_id)

    def screenshot(self) -> str:
        """
        截图到固定位置 runner/tmp/image/screenshot.png(对接 main.py 读取的大图)。

        不接受 save_path 参数,强制使用 AdbClient 的默认固定路径,
        保证"截图 -> 定位"流程的路径一致,避免调用方传错路径导致 main.py 读不到。

        Returns:
            保存的截图完整路径(E:/GrapeMobile/runner/tmp/image/screenshot.png)。

        Raises:
            AdbError: 截图过程任一 adb 子命令失败时抛出。
        """
        logger.info("[AdbExecutor] 截图 -> 固定位置 runner/tmp/image/screenshot.png")
        return self._client.screenshot()  # 不传 save_path,用 AdbClient 默认固定路径

    def run(self) -> str:
        """
        一步到位:连接/挂载设备 + 截图到固定位置。

        按 device_id 形态自动选择连接方式:
            - 含 ":"(网络地址,如 "192.168.1.100:5555"):走 adb connect;
            - 不含 ":"(模拟器 "emulator-5554" 或 USB 序列号):走 attach,
              仅校验在线,不做网络连接(adb 已自动发现这类设备)。

        Returns:
            保存的截图完整路径。

        Raises:
            AdbError: 连接/挂载或截图任一步骤失败时抛出。
        """
        if ":" in self.device_id:
            self.connect()
        else:
            self._client.attach(self.device_id)
        return self.screenshot()


__all__ = ["AdbExecutor"]
