"""本机服务的来源防线：Host 白名单 + 被拒请求的限流日志。

WebUI（8321）与 QQ 反向 WS（8866）都是只该被本机访问的服务，两边共用这一份，
口径不漂移：

- **Host 白名单**（防 DNS rebinding）：攻击页把自己的域名解析到 127.0.0.1 后，
  Origin 与 Host 是同一个攻击域名，同源检查照样通过；只有 Host 白名单能在最外层
  把"不是用本机名字访问的请求"挡掉。
- **限流日志**：rebinding 页面可以每秒发几百个请求，每个都打一条 WARNING 会刷满
  data/logs/ 和 /logs 页的实时流。同一个来源每个窗口只记第一条，之后计数，
  下一个窗口再记时带上"又拒绝了 N 次"。

hostname 一律走 urlparse（去端口、去 IPv6 方括号、转小写），与 web 适配器的
`_origin_allowed` 同口径。不用 Starlette 自带的 TrustedHostMiddleware：它用
`split(":")` 取主机名，`[::1]:8000` 会被切坏。
"""

import logging
import time
from typing import Callable
from urllib.parse import urlparse

from fastapi.responses import PlainTextResponse
from starlette.datastructures import Headers

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: 本机名字。只比 hostname（已去端口、去方括号、转小写）
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 同一来源的拒绝日志，多久记一条（秒）
REJECT_LOG_WINDOW = 60.0


def host_allowed(host_header: str, allowed: frozenset[str]) -> bool:
    """Host 头的 hostname 是否在白名单里。缺 Host / 解析失败一律拒。"""
    if not host_header:
        return False
    try:
        return urlparse(f"//{host_header}").hostname in allowed
    except ValueError:
        return False


def is_loopback(host: str) -> bool:
    """监听地址是不是只对本机开放（127.0.0.0/8、localhost、::1）。"""
    h = (host or "").strip().lower().strip("[]")
    return h in LOCAL_HOSTS or h.startswith("127.")


class RejectLog:
    """被拒请求的限流日志：同一个 key 每 `window` 秒只记一条。"""

    def __init__(self, what: str, window: float = REJECT_LOG_WINDOW,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.what = what
        self.window = window
        self.clock = clock
        #: key → [本窗口起点, 本窗口里没记的次数]
        self._seen: dict[str, list[float]] = {}

    def note(self, key: str, detail: str = "") -> None:
        now = self.clock()
        entry = self._seen.get(key)
        if entry is not None and now - entry[0] < self.window:
            entry[1] += 1
            return
        suppressed = int(entry[1]) if entry is not None else 0
        tail = f"（上一分钟里又拒绝了 {suppressed} 次）" if suppressed else ""
        logger.warning("拒绝%s：%r %s%s", self.what, key, detail, tail)
        self._seen[key] = [now, 0]
        # 防止攻击方换着 Host 把字典撑大：超过一定数量就清掉过期的
        if len(self._seen) > 256:
            self._seen = {k: v for k, v in self._seen.items() if now - v[0] < self.window}


class TrustedHostMiddleware:
    """只放行 Host 头在白名单里的 HTTP / WebSocket 请求（纯 ASGI）。

    HTTP 回 400；WebSocket 在握手前发 close（ASGI 规范：服务器回 403）。
    """

    def __init__(self, app, allowed: frozenset[str],
                 reject_log: RejectLog | None = None) -> None:
        self.app = app
        self.allowed = frozenset(allowed)
        self.reject_log = reject_log or RejectLog("Host 不在白名单的请求")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = Headers(scope=scope).get("host", "")
            if not host_allowed(host, self.allowed):
                self.reject_log.note(host, scope.get("path", ""))
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await PlainTextResponse("Invalid host header", status_code=400)(
                        scope, receive, send
                    )
                return
        await self.app(scope, receive, send)
