"""真浏览器驱动：用本机已装的 Edge/Chrome 开一个无地址栏窗口，通过 CDP 控制它。

为什么要这一步：纯 HTTP 提交搞不定 JS 渲染的门户（`<form>` 都没有的页面本来就没法
"填写"）。参考项目 CampusConnect 是用内置 WebView2 + 注入 JS 解决的；我们不想为了
一个浏览器内核引入第三方依赖，于是改用系统自带的 Edge/Chrome：

    msedge --app=URL --remote-debugging-port=PORT --user-data-dir=<临时目录>

`--app` 让窗口没有地址栏（看起来像内嵌在程序里），`--remote-debugging-port` 让
Python 用 Chrome DevTools Protocol（CDP）在页面里执行 JS、读回结果。
CDP 的传输是 WebSocket，标准库里没有客户端，所以这里自己实现一个**最小**版本
（只做握手、文本帧收发、ping/pong、续帧拼接，够用即可）。

安全约定：
* 只在页面的 origin 与保存的认证页一致时才注入脚本；密码只出现在内存里的 JS
  文本中，不落盘、不进日志。
* 调试端口只监听回环地址，并且**校验这个端口确实是我们自己启动的进程**在监听
  （防止误连到用户已有的浏览器实例，或被别的程序抢注端口）。
* 一次会话用独立的临时用户目录，退出时按进程树清理，不残留 cookie。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import paths

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BrowserError(RuntimeError):
    """浏览器没装、起不来、或 CDP 连不上。"""


# ---------------------------------------------------------------- 找浏览器

_CANDIDATES = (
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)


def find_browser() -> str:
    """找一个可用的 Chromium 内核浏览器（Edge 优先：Win10/11 基本都有）。"""
    override = os.environ.get("CAMPUSNET_BROWSER", "").strip()
    if override and Path(override).exists():
        return override
    for raw in _CANDIDATES:
        expanded = os.path.expandvars(raw)
        if "%" in expanded:  # 变量没展开（例如 32 位系统没有 ProgramFiles(x86)）
            continue
        if Path(expanded).exists():
            return expanded
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise BrowserError("未找到 Edge 或 Chrome，无法打开认证页面。")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------- 最小 WebSocket

class _WebSocket:
    """够 CDP 用的最小 WebSocket 客户端（文本帧、ping/pong、续帧）。"""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        parts = urlsplit(ws_url)
        if parts.scheme != "ws":
            raise BrowserError(f"只支持 ws:// 的调试地址：{ws_url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path + (("?" + parts.query) if parts.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(handshake.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("CDP 握手失败：连接被关闭")
            header += chunk
            if len(header) > 65536:
                raise BrowserError("CDP 握手失败：响应过长")
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise BrowserError(f"CDP 握手被拒绝：{header.split(b'\r\n', 1)[0].decode('latin-1')}")
        self._buffer = b""

    # -- 发送 --
    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        mask_bit = 0x80  # 客户端必须加掩码
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack(">H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    # -- 接收 --
    def _recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BrowserError("CDP 连接已断开")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + masked)

    def recv_message(self) -> str:
        """读一条完整文本消息（自动处理 ping/pong 和续帧）。"""
        fragments: list[bytes] = []
        while True:
            first, second = self._recv_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x9:      # ping -> pong
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:      # pong
                continue
            if opcode == 0x8:      # close
                raise BrowserError("CDP 连接被对端关闭")
            fragments.append(payload)
            if fin:
                return b"".join(fragments).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_control(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- CDP 客户端

class DevTools:
    """连到某个标签页的 CDP 会话。"""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        self.ws = _WebSocket(ws_url, timeout=timeout)
        self._next_id = 1
        self.pending_events: list[dict] = []

    def call(self, method: str, timeout: float = 15.0, **params) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        self.ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(self.ws.recv_message())
            if data.get("id") == msg_id:
                if "error" in data:
                    raise BrowserError(f"{method} 失败：{data['error']}")
                return data.get("result") or {}
            if "method" in data:
                self.pending_events.append(data)
        raise BrowserError(f"{method} 超时（{timeout}s）")

    def evaluate(self, expression: str, timeout: float = 15.0):
        """在页面里执行 JS，返回它的值（按值返回）。"""
        result = self.call(
            "Runtime.evaluate",
            timeout=timeout,
            expression=expression,
            returnByValue=True,
            awaitPromise=False,
            userGesture=True,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text") or "未知错误"
            raise BrowserError(f"页面脚本执行出错：{text.splitlines()[0]}")
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        self.ws.close()


# ---------------------------------------------------------------- 浏览器进程

@dataclass
class BrowserSession:
    process: subprocess.Popen
    devtools: DevTools
    profile_dir: Path
    port: int

    def close(self) -> None:
        try:
            self.devtools.close()
        except Exception:  # noqa: BLE001
            pass
        _kill_tree(self.process)
        # 临时用户目录要清掉：里面带着这次会话的 cookie。
        # 浏览器进程退出后句柄还会占一会儿，所以重试几次直到真的没了。
        for _ in range(8):
            if not self.profile_dir.exists():
                break
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            if self.profile_dir.exists():
                time.sleep(0.4)


def _http_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (本机回环)
        return json.loads(resp.read().decode("utf-8", "replace"))


def profile_root() -> Path:
    """浏览器临时用户目录的根：放在用户数据目录下，退出时好清理。"""
    root = paths.browser_profile_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001  退回到系统临时目录
        root = Path(tempfile.gettempdir()) / "campusnet_browser"
        root.mkdir(parents=True, exist_ok=True)
    return root


def cleanup_stale_profiles(keep: Path | None = None, max_age: float = 3600.0) -> int:
    """清理上次崩溃残留的用户目录（里面的 cookie 不该留到下次）。"""
    removed = 0
    root = profile_root()
    now = time.time()
    try:
        entries = list(root.iterdir())
    except Exception:  # noqa: BLE001
        return 0
    for item in entries:
        if keep is not None and item == keep:
            continue
        try:
            if item.is_dir() and now - item.stat().st_mtime > max_age:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        except Exception:  # noqa: BLE001
            continue
    return removed


def _new_profile_dir() -> Path:
    """建一个本次会话专用的用户目录。

    刻意**不用** ``tempfile.mkdtemp``：它以 0o700 建目录，在某些受限账户下会得到
    只看得到"当前用户"的权限，浏览器自己反而写不进去。这里用普通 mkdir 让它继承
    父目录权限，再用随机名保证唯一。
    """
    root = profile_root()
    for _ in range(20):
        candidate = root / f"session-{os.getpid()}-{int(time.time() * 1000)}-{os.urandom(3).hex()}"
        try:
            candidate.mkdir(mode=0o777)
            return candidate
        except FileExistsError:
            continue
    raise BrowserError("无法创建浏览器临时用户目录")


def port_owner_pid(port: int) -> int:
    """查某个本地端口是哪个进程在监听（Windows：netstat -ano）。

    查不到就返回 0 —— 调用方据此判断"这个调试端口不是我们的"。
    """
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        return 0
    for raw in (proc.stdout or b"").decode("mbcs", "replace").splitlines():
        line = raw.strip()
        if not line.upper().startswith("TCP"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local, remote, state, pid = parts[1], parts[2], parts[3], parts[4]
        if not local.endswith(f":{port}"):
            continue
        if state.upper() != "LISTENING":
            continue
        try:
            return int(pid)
        except ValueError:
            continue
    return 0


def _wait_for_page_target(port: int, deadline: float, expect_pid: int | None = None) -> str:
    """等浏览器把调试端口和页面标签准备好，返回该标签的 ws 地址。

    ``expect_pid`` 给定时会核对监听该端口的进程；不是我们启动的那个就报错，
    避免把 CDP 会话连到用户现成的浏览器上（那等于把密码交给另一个页面）。
    """
    last_error = ""
    while time.time() < deadline:
        try:
            version = _http_json(f"http://127.0.0.1:{port}/json/version")
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(0.25)
            continue
        ws_debugger = str((version or {}).get("webSocketDebuggerUrl") or "")
        if ws_debugger and f":{port}" not in ws_debugger:
            raise BrowserError("调试端口返回了非预期地址，已中止（可能被其它程序占用）")
        if expect_pid:
            owner = port_owner_pid(port)
            if owner and owner != expect_pid:
                raise BrowserError(
                    f"调试端口 {port} 属于另一个进程（PID {owner}），"
                    "为避免误连到已有浏览器，已中止自动填写"
                )
        try:
            targets = _http_json(f"http://127.0.0.1:{port}/json/list")
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(0.25)
            continue
        for item in targets:
            if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                return item["webSocketDebuggerUrl"]
        last_error = "还没有页面标签"
        time.sleep(0.25)
    raise BrowserError(f"浏览器调试端口没就绪：{last_error or '超时'}")


def open_portal(url: str, *, width: int = 900, height: int = 720,
                start_timeout: float = 25.0) -> BrowserSession:
    """打开一个无地址栏窗口加载 url，并返回可执行 JS 的会话。"""
    exe = find_browser()
    cleanup_stale_profiles()
    port = _free_port()
    profile_dir = _new_profile_dir()
    args = [
        exe,
        f"--app={url}",
        f"--remote-debugging-port={port}",
        # 只监听回环：调试接口暴露在 0.0.0.0 上等于把浏览器的控制权交给局域网
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--disable-sync",
        "--disable-background-networking",
        # 登录窗口不该加载用户装的扩展：既减少攻击面，也避免扩展的 background page
        # 混进 CDP 的目标列表（实测临时 profile 里会带出多个 chrome-extension 页面）
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-client-side-phishing-detection",
        f"--window-size={width},{height}",
    ]
    try:
        process = subprocess.Popen(  # noqa: S603
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise BrowserError(f"浏览器启动失败：{exc}") from exc

    try:
        deadline = time.time() + start_timeout
        ws_url = _wait_for_page_target(port, deadline, expect_pid=process.pid)
        devtools = DevTools(ws_url)
        return BrowserSession(process=process, devtools=devtools,
                              profile_dir=profile_dir, port=port)
    except Exception:
        _kill_tree(process)
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise


def _kill_tree(process: subprocess.Popen) -> None:
    """按进程树结束浏览器（只 terminate 主进程会留下渲染进程占着目录）。"""
    try:
        process.terminate()
        process.wait(timeout=8)
    except Exception:  # noqa: BLE001
        pass
    try:
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, timeout=15, creationflags=_NO_WINDOW,
            )
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
