"""
src/adbtools - ADB 设备工具包
=============================
封装 adb 命令行,为 imgloc 业务场景提供设备连接与截图能力。

设计目标:
    - 轻量:仅依赖系统 adb 可执行文件,不引入第三方 adb 库
    - 解耦:独立于 imgloc 核心库,可单独使用;异常体系自带 AdbError,不继承 ImgLocError
    - 跨平台:subprocess 用 list 形式传参(不启用 shell),Windows/Linux 均可

典型使用流程:
    >>> from adbtools import AdbClient
    >>> client = AdbClient()
    >>> client.connect("192.168.1.100:5555")  # 1. 连接网络调试设备
    >>> client.screenshot()                    # 2. 截图保存到 runner/tmp/image/screenshot.png(大图)
    '/path/to/runner/tmp/image/screenshot.png'
    # 3. 用户手动从大图截取按钮区域 -> runner/tmp/image/template.png(小图)
    # 4. python runner/main.py -> 输出坐标 + adb tap 命令
"""

from adbtools.client import AdbClient, AdbError

__all__ = ["AdbClient", "AdbError"]
