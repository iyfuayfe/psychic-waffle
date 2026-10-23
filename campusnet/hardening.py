"""进程加固（可选功能）—— 3.1.0 起**默认关闭**。

## 为什么默认关闭（有证据，不是猜测）

3.0.4/3.0.5 曾默认尝试施加两项 Windows 进程缓解策略，用来"挡住第三方 DLL 注入"：

* ``ProcessExtensionPointDisablePolicy``（禁用 AppInit_DLLs / 旧式 hook 注入）
* ``ProcessSignaturePolicy = MicrosoftSignedOnly``（此后只允许加载**微软签名**的 DLL）

实测证据表明后者的代价不可接受：

1. **它连"签名有效"的组件也拒绝**。打包产物里的 ``python313.dll``、
   ``_tkinter.pyd``、``_ssl.pyd``、``unicodedata.pyd`` 的 Authenticode 状态都是
   **Valid**，签名者是 ``CN=Python Software Foundation`` —— 只是不是微软。
   策略只认微软，所以这些**合法已签名**的二进制被判为不可加载。
2. **它会在运行时陆续炸掉本程序自己的功能**（因为在策略施加前没法预加载所有
   未来会用到的扩展模块）。在施加策略后逐个 import 未预热的扩展模块，实测：

       sqlite3   -> ImportError: DLL load failed while importing _sqlite3:
                    Windows 无法验证此文件的数字签名…
       asyncio   -> ImportError: DLL load failed while importing _overlapped: …
       winsound  -> ImportError: DLL load failed while importing winsound: …

   这就是"改完之后部分功能损坏""出现莫名其妙故障"的机制来源：任何一条之前没走到的
   代码路径，第一次用到就会失败，而且错误信息与业务毫无关系。
3. **它会对用户弹出「损坏的映像 0xc0000428」系统对话框**（挂在我们的窗口上），
   实际上是第三方注入被拒时加载器的提示 —— 用户看到的是"程序出错了"。
4. **收益未被证实**：A/B 对照（加固开/关各 6 次）时第三方软件并未注入，
   无法证明它能阻止真实崩溃；而第三方注入也可能发生在进程创建阶段，
   那时策略根本来不及施加。

结论（按任务书"先修复根因、最小可验证、保留确有必要且经验证的措施"）：
**默认不施加任何进程缓解策略**，回到与 3.0.0 一致的中立行为；保留只读的诊断能力
（注入模块检测、会话取证、诊断包），把加固改成**显式可选**。

## 现在的模式

* ``off``（默认）：不调用任何 ``SetProcessMitigationPolicy``，不改 ``SetErrorMode``。
* ``opt-in``：用户显式要求时才施加（``CAMPUSNET_HARDEN=1``，或 GUI 传入
  ``--harden``）。此时才做模块预热、自检、弹窗抑制，并在日志里写明"这是可选加固"。
* ``diag``：只读诊断（检测已注入模块 / 已运行的叠加软件），永远可用且无副作用。

## 仍然有效的安全设计（与加固无关，保持不变）

密码 DPAPI 加密、日志脱敏、不写系统安全排除项、不要求管理员权限、TLS 校验证书、
用户数据只写 ``%LOCALAPPDATA%``。这些才是本程序真正的安全属性；
进程缓解策略只是"兼容性变通手段"，不该被当成安全特性。
"""

from __future__ import annotations

import ctypes
import os
import sys

POLICY_EXTENSION_POINT_DISABLE = 6
POLICY_SIGNATURE = 8

# 微软签名策略取值：0=None 1=MicrosoftSignedOnly 2=StoreSignedOnly 3=MitigationOptIn
SIGNATURE_MICROSOFT_ONLY = 1

# SetErrorMode 标志位
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOOPENFILEERRORBOX = 0x8000

ENV_DISABLE = "CAMPUSNET_NO_HARDENING"   # 兼容 3.0.4/3.0.5 的旧开关：强制关闭
ENV_FORCE = "CAMPUSNET_HARDEN"           # 显式开启：1/true/all 为全量，extpoint 为仅扩展点

MODE_OFF = "off"
MODE_OPT_IN = "opt-in"

_last_report: dict = {}


# ---------------------------------------------------------------- 模式判定


def _env_flag(name: str) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _disabled_by_env() -> bool:
    return _env_flag(ENV_DISABLE)


def _forced_by_env() -> bool:
    return _env_flag(ENV_FORCE)


def _env_full_mode() -> bool:
    """``CAMPUSNET_HARDEN=all`` 与 ``=1`` 等价；``extpoint`` 只做扩展点那一项。"""
    value = (os.environ.get(ENV_FORCE) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "all", "full")


def _env_extension_only() -> bool:
    return (os.environ.get(ENV_FORCE) or "").strip().lower() in ("extpoint", "extension")


def requested_mode() -> str:
    """本进程**应当**施加什么级别的加固（默认 ``off``）。

    这是 3.1.0 的核心行为变化：不再"检测到第三方软件就自动上锁"。
    以前那种做法把第三方进程名当成开关（任务书明确禁止），
    而且会在用户毫无预期时改变进程的安全属性。
    """
    if sys.platform != "win32":
        return MODE_OFF
    if _disabled_by_env():
        return MODE_OFF
    if _env_full_mode() or _env_extension_only():
        return MODE_OPT_IN
    return MODE_OFF


# ---------------------------------------------------------------- 策略原语


class _FlagsPolicy(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32)]


class _SignaturePolicy(ctypes.Structure):
    _fields_ = [("Policy", ctypes.c_int)]


def _set_policy(index: int, struct) -> tuple[bool, str]:
    """施加一项进程缓解策略。失败返回 (False, 原因)，绝不抛异常。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ok = bool(
            kernel32.SetProcessMitigationPolicy(
                index, ctypes.byref(struct), ctypes.sizeof(struct)
            )
        )
        if ok:
            return True, "ok"
        return False, f"WinError {ctypes.get_last_error()}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _suppress_loader_dialogs() -> str:
    """抑制"损坏的映像 / Bad Image"这类加载失败弹窗（仅可选加固路径使用）。

    只在启用签名策略时才有意义：策略拒绝第三方 DLL 时，加载器会在我们的窗口上
    弹模态错误框（``0xc0000428``）。默认模式不施加策略，也就不需要改 ErrorMode ——
    保持系统默认行为，任何真实的依赖缺失都能正常提示。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
        kernel32.SetErrorMode.restype = ctypes.c_uint
        current = kernel32.SetErrorMode(0)
        wanted = current | SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX
        kernel32.SetErrorMode(wanted)
        return f"ok(0x{current:x}->0x{wanted:x})"
    except Exception as exc:  # noqa: BLE001
        return f"error({exc})"


def _preload_own_dependencies() -> list:
    """把本程序会用到的模块先加载好（仅可选加固路径需要）。

    策略一旦施加就不可撤销，而它只认微软签名；所以如果把某个 CPython 扩展留到
    运行时再加载，就会以"无法验证数字签名"失败。实测 ``_sqlite3`` / ``_overlapped``
    / ``winsound`` 都会这样死掉 —— 这也是本功能默认关闭的原因。

    返回导入失败的模块名列表（不抛异常）。
    """
    modules = (
        "ssl", "socket", "select", "ctypes", "ctypes.wintypes",
        "tkinter", "tkinter.ttk", "tkinter.font", "tkinter.messagebox",
        "http.client", "urllib.request", "urllib.parse", "urllib.error",
        "winreg", "hashlib", "zlib", "json", "struct", "array", "bisect",
        "random", "datetime", "time", "queue", "threading", "subprocess",
        "tempfile", "shutil", "pathlib", "logging", "logging.handlers",
        "dataclasses", "ipaddress", "base64", "binascii", "unicodedata",
        "math", "re", "copy", "collections", "functools", "itertools",
        "textwrap", "traceback", "warnings", "weakref", "codecs",
        "encodings.idna", "encodings.punycode", "encodings.utf_8",
        "encodings.ascii", "encodings.latin_1", "encodings.gbk",
        "encodings.gb2312", "encodings.big5",
    )
    failed: list[str] = []
    for name in modules:
        try:
            __import__(name)
        except Exception:  # noqa: BLE001
            failed.append(name)
    return failed


def self_test() -> dict:
    """功能自检：施加可选加固之后，这些能力必须仍然可用。"""
    results: dict = {}

    def check(name: str, fn) -> None:
        try:
            results[name] = "ok" if fn() else "fail"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"{type(exc).__name__}: {exc}"[:120]

    def https_ready() -> bool:
        import ssl

        return ssl.create_default_context() is not None

    def idna_ready() -> bool:
        return "例子.test".encode("idna").decode("ascii") != ""

    def gbk_ready() -> bool:
        return "中文".encode("gbk").decode("gbk") == "中文"

    def hashlib_ready() -> bool:
        import hashlib

        return len(hashlib.sha256(b"x").hexdigest()) == 64

    def json_ready() -> bool:
        import json

        return json.loads(json.dumps({"a": 1}))["a"] == 1

    def zlib_ready() -> bool:
        import zlib

        return len(zlib.compress(b"compress-me")) > 0

    def subprocess_ready() -> bool:
        import subprocess

        proc = subprocess.run(
            ["cmd", "/c", "echo ok"], capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0

    check("https", https_ready)
    check("idna", idna_ready)
    check("gbk", gbk_ready)
    check("hashlib", hashlib_ready)
    check("json", json_ready)
    check("zlib", zlib_ready)
    check("subprocess", subprocess_ready)
    return results


# ---------------------------------------------------------------- 对外接口


def early_protect() -> dict:
    """进程启动早期调用。默认**什么都不做**，只记录决策。

    保持这个入口是为了让启动流程稳定：调用方不必关心"现在还要不要加固"，
    是否施加策略由 :func:`requested_mode` 决定，且写进日志便于事后诊断。
    """
    global _last_report
    mode = requested_mode()
    report: dict = {
        "stage": "early",
        "mode": mode,
        "extension_points": "skipped",
        "error_mode": "skipped",
        "reason": "",
    }

    if mode == MODE_OFF:
        report["reason"] = (
            "默认不施加进程缓解策略（3.1.0 起）。"
            "如确需加固请设置环境变量 CAMPUSNET_HARDEN=1。"
        )
        _last_report = report
        return report

    # 可选路径：先抑制加载失败弹窗，再禁用扩展点注入
    report["error_mode"] = _suppress_loader_dialogs()
    flags = _FlagsPolicy()
    flags.Flags = 1
    ok, info = _set_policy(POLICY_EXTENSION_POINT_DISABLE, flags)
    report["extension_points"] = "ok" if ok else info
    if not ok:
        report["reason"] = f"扩展点加固未启用：{info}"
    _last_report = report
    return report


def self_protect(*, aggressive: bool | None = None) -> dict:
    """施加可选加固（GUI 在 tkinter 加载完之后调用）。

    参数语义（3.1.0 起）：

    * ``None``（默认）—— 跟随 :func:`requested_mode`，默认不施加任何策略；
    * ``True``  —— 强制施加全量加固（仅供显式选择加固的用户与测试使用）；
    * ``False`` —— 强制不施加。

    返回报告字典，同时记入 :func:`last_report`。**任何失败都不会抛异常**：
    加固是可选优化，不能让界面打不开。
    """
    global _last_report
    report: dict = {
        "mode": MODE_OFF,
        "extension_points": "skipped",
        "signature": "skipped",
        "error_mode": "skipped",
        "reason": "",
    }

    if sys.platform != "win32":
        report["reason"] = "非 Windows 平台"
        _last_report = report
        return report

    if aggressive is None:
        enabled = requested_mode() == MODE_OPT_IN
    else:
        enabled = bool(aggressive)

    if not enabled:
        report["reason"] = (
            "默认不施加进程缓解策略（3.1.0 起）。"
            "如确需加固请设置环境变量 CAMPUSNET_HARDEN=1。"
        )
        _last_report = report
        return report

    report["mode"] = MODE_OPT_IN
    preload_failed = _preload_own_dependencies()
    if preload_failed:
        report["preload_failed"] = preload_failed

    report["error_mode"] = _suppress_loader_dialogs()

    flags = _FlagsPolicy()
    flags.Flags = 1
    ok, info = _set_policy(POLICY_EXTENSION_POINT_DISABLE, flags)
    report["extension_points"] = "ok" if ok else info

    signature = _SignaturePolicy()
    signature.Policy = SIGNATURE_MICROSOFT_ONLY
    ok, info = _set_policy(POLICY_SIGNATURE, signature)
    report["signature"] = "ok" if ok else info
    if not ok:
        report["reason"] = f"签名策略未能启用：{info}"
    else:
        checks = self_test()
        report["self_test"] = checks
        broken = {name: value for name, value in checks.items() if value != "ok"}
        if broken:
            report["reason"] = (
                "签名策略已生效，但自检发现它影响到了本程序自身功能："
                + "；".join(f"{name}={value}" for name, value in broken.items())
                + "。这是该策略的已知代价；如需关闭加固，去掉 CAMPUSNET_HARDEN 后重启。"
            )

    _last_report = report
    return report


def last_report() -> dict:
    return dict(_last_report)


def describe(report: dict | None = None) -> str:
    """把加固状态说成一句人话（日志/诊断用）。"""
    report = report if report is not None else _last_report
    if not report:
        return "尚未执行进程加固检查。"
    if report.get("mode") == MODE_OFF or (
        report.get("extension_points") == "skipped" and report.get("signature") == "skipped"
    ):
        reason = report.get("reason") or "默认不施加进程缓解策略。"
        return f"进程加固未启用：{reason}"
    parts = []
    if report.get("extension_points") == "ok":
        parts.append("已禁用扩展点注入")
    if report.get("signature") == "ok":
        parts.append("已限制为仅加载微软签名 DLL（可选加固，已知会拒绝合法第三方组件）")
    if str(report.get("error_mode", "")).startswith("ok"):
        parts.append("已抑制加载失败弹窗")
    if not parts:
        parts.append("未施加任何加固")
    if report.get("reason"):
        parts.append(report["reason"])
    return "；".join(parts) + "。"


def overlay_present() -> bool:
    """是否有已知的叠加类软件在运行。

    **只用于诊断与提示**，不再用于决定是否施加策略（任务书禁止把第三方进程名
    当成行为开关）。
    """
    try:
        from . import conflicts

        return bool(conflicts.overlay_processes_running()) or bool(conflicts.detect())
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    """诊断用：当前模式、是否被环境变量关闭、叠加软件与注入情况、上次报告。"""
    try:
        from . import conflicts

        overlays = [
            {"process": name, "vendor": vendor}
            for name, vendor in conflicts.overlay_processes_running()
        ]
        injected = [hit.module for hit in conflicts.detect()]
        detect_error = ""
    except Exception as exc:  # noqa: BLE001
        overlays, injected = [], []
        detect_error = str(exc)
    payload = {
        "mode": requested_mode(),
        "disabled_by_env": _disabled_by_env(),
        "forced_by_env": _forced_by_env(),
        "overlay_processes": overlays,
        "injected_modules": injected,
        "last_report": last_report(),
    }
    if detect_error:
        payload["detect_error"] = detect_error
    return payload
