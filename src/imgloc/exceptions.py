"""
imgloc 自定义异常体系
=====================
统一的异常类型，方便上层调用方做精确的错误处理。
"""


class ImgLocError(Exception):
    """imgloc 库所有自定义异常的基类。"""


class ImageLoadError(ImgLocError):
    """图像加载/解码失败时抛出（例如路径不存在、字节流损坏、格式不支持）。"""


class MatcherNotAvailableError(ImgLocError):
    """
    某个匹配器所需的可选依赖未安装时抛出。

    典型场景：LightGlueMatcher 需要 torch/kornia/lightglue，
    未安装 imgloc[deep] 时调用该匹配器会抛出此异常；
    MultiStrategyLocator 会捕获此异常并自动跳过该层，继续尝试下一层算法。
    """


class ConfigError(ImgLocError):
    """配置文件/配置字典解析失败，或关键配置项缺失/类型错误时抛出。"""


class NoMatcherAvailableError(ImgLocError):
    """责任链中所有算法都不可用（例如全部依赖缺失或全部匹配失败且未设置兜底）时抛出。"""
