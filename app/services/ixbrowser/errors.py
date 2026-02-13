"""ixBrowser 服务异常类型（轻量模块，避免引入重依赖）。"""


class IXBrowserServiceError(Exception):
    """ixBrowser 服务通用异常"""


class IXBrowserConnectionError(IXBrowserServiceError):
    """ixBrowser 连接异常"""


class IXBrowserAPIError(IXBrowserServiceError):
    """ixBrowser 业务异常"""

    def __init__(self, code: int, message: str):
        self.code = int(code)
        self.message = str(message)
        super().__init__(f"ixBrowser API error {self.code}: {self.message}")


class IXBrowserNotFoundError(IXBrowserServiceError):
    """ixBrowser 资源不存在"""
