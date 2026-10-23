"""测试公共设施：临时数据目录、假 HTTP 客户端、断言辅助。

原则：

* 测试**不对真实校园网发起任何请求**，也不使用真实账号；
* 需要"有一个能收发的服务器"时，用本机 127.0.0.1 上的临时 HTTP 服务器；
* 所有测试都在临时目录里跑，不会污染用户数据目录（``CAMPUSNET_DATA_DIR``）。
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# --------------------------------------------------------------------------
# 全局保险：任何测试都不许碰用户真实的 %LOCALAPPDATA%\CampusNet
# --------------------------------------------------------------------------
# 光靠逐个测试用 ``isolated_data_dir()`` 是不够的：只要某条路径（比如
# ``Daemon._tick`` 里写心跳、认证成功时写 state.json）没被包住，就会落到真实
# 数据目录里 —— 实测就是靠这个办法才发现 tests/ 会漏写 heartbeat.json。
# 所以这里在 import 阶段就把环境变量指到一个进程级临时目录，
# 需要"默认路径"行为的测试再自己 pop/恢复（见 test_paths.py）。
_ORIGINAL_DATA_DIR_ENV = os.environ.get("CAMPUSNET_DATA_DIR")
if not _ORIGINAL_DATA_DIR_ENV:
    _RUNTIME_DIR = PROJECT_ROOT / ".tmp" / f"test-data-{os.getpid()}"
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        os.environ["CAMPUSNET_DATA_DIR"] = str(_RUNTIME_DIR)
        import shutil as _shutil

        atexit.register(lambda: _shutil.rmtree(_RUNTIME_DIR, ignore_errors=True))
    except Exception:  # noqa: BLE001
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def data_dir():
    """把用户数据目录临时指到一个隔离目录。"""
    previous = os.environ.get("CAMPUSNET_DATA_DIR")
    with temp_dir(prefix="data-") as tmp:
        os.environ["CAMPUSNET_DATA_DIR"] = str(tmp)
        try:
            yield tmp
        finally:
            if previous is None:
                os.environ.pop("CAMPUSNET_DATA_DIR", None)
            else:
                os.environ["CAMPUSNET_DATA_DIR"] = previous


@contextlib.contextmanager
def temp_dir(prefix: str = "tmp-"):
    """工作区内的临时目录。

    刻意不用 ``tempfile.TemporaryDirectory``：它以 0o700 建目录，在受限账户
    （比如沙箱）下清理时会因为 ACL 拿不到而失败。建在 ``.tmp/`` 下、用普通
    mkdir 继承父目录权限，清理也做成"尽力而为"。
    """
    import shutil
    import uuid

    root = PROJECT_ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{prefix}{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


class FakeResponse:
    """够用的假响应（和 campusnet.http.Response 的接口一致）。"""

    def __init__(self, status=200, text="", url="http://portal.test/login",
                 headers=None, body=None):
        self.status = status
        self.url = url
        self.headers = headers or {}
        self.body = (body if body is not None else (text or "").encode("utf-8"))
        self.elapsed = 0.0
        self._text = text
        self.text = text
        self.location = self.headers.get("Location", "")

    @property
    def content_type(self):
        return self.headers.get("Content-Type", "")

    @property
    def is_redirect(self):
        return self.status in (301, 302, 303, 307, 308) and bool(self.location)


class FakeClient:
    """记录请求的假客户端：用来断言"账号密码被发到了哪里"。

    ``responses`` 的键是 ``"GET url"`` / ``"POST url"`` / ``"*"``；
    值是 :class:`FakeResponse`（直接返回）或异常实例（**抛出**，模拟网络错误）。
    """

    def __init__(self, responses=None, default=None):
        self.requests: list = []
        self.responses = responses or {}
        self.default = default if default is not None else FakeResponse(200, "")
        self._method = {}

    def _lookup(self, method, url):
        for key in (f"{method} {url}", url, "*"):
            if key in self.responses:
                return self.responses[key]
        return self.default

    def _record(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        result = self._lookup(method, url)
        if isinstance(result, BaseException):
            raise result
        return result

    def request(self, url, *, method=None, data=None, headers=None,
                follow_redirects=False, max_redirects=5, timeout=None):
        return self._record(method or "GET", url, data=data, headers=headers,
                            follow_redirects=follow_redirects, timeout=timeout)

    def get(self, url, *, follow_redirects=True, headers=None, timeout=None):
        return self._record("GET", url, headers=headers,
                            follow_redirects=follow_redirects, timeout=timeout)

    def post_form(self, url, fields, *, encoding=None, follow_redirects=True,
                  headers=None, timeout=None):
        return self._record("POST", url, data=fields, headers=headers,
                            follow_redirects=follow_redirects, timeout=timeout)

    def get_query(self, url, fields, *, encoding=None, follow_redirects=True, timeout=None):
        return self._record("GET", url, data=fields,
                            follow_redirects=follow_redirects, timeout=timeout)


class Silence:
    """一个丢弃所有日志的 logger，避免测试输出刷屏。"""

    def debug(self, *a, **k):
        pass

    info = warning = error = critical = exception = debug

    def log(self, *a, **k):
        pass


@contextlib.contextmanager
def http_server(routes):
    """起一个本机 HTTP 服务器。

    ``routes`` 形如 ``{("GET", "/path"): callable(request) -> (status, headers, body)}``。
    返回 base_url。
    """
    hits: list = []

    class Handler(BaseHTTPRequestHandler):
        server_version = "TestPortal/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: A003
            pass

        def _handle(self, method):
            parsed = self.path.split("?")[0]
            hits.append((method, self.path, dict(self.headers)))
            handler = routes.get((method, parsed))
            if handler is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status, headers, body = handler(self)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self._body = self.rfile.read(length) if length else b""
            self._handle("POST")

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_until(predicate, timeout=5.0, interval=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# 便于 `from tests.support import isolated_data_dir` 这种更明确的写法
isolated_data_dir = data_dir

