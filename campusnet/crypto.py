"""用 Windows DPAPI 加密保存密码。

为什么用它：
- 密文与本机 + 当前 Windows 用户绑定，把 config.json 拷到别的机器/别的用户下解不开。
- 不需要自己造密钥，也不需要用户记主密码。
- 调用时强制 CRYPTPROTECT_UI_FORBIDDEN，即使解密失败也**不会弹出任何系统对话框**。

纯 ctypes 实现，不依赖 pywin32。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import sys

# 禁止 DPAPI 弹出任何 UI（这是"绝不弹窗"要求的关键一环）
CRYPTPROTECT_UI_FORBIDDEN = 0x01

# 附加熵：让密文只对本程序有意义
_ENTROPY = b"campus-net-login/v1"
_DESCRIPTION = "campus-net-login"
ERROR_ALREADY_EXISTS = 183


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


if sys.platform == "win32":  # pragma: no cover - 仅在 Windows 生效
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob),
        wt.LPCWSTR,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptProtectData.restype = wt.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.POINTER(wt.LPWSTR),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptUnprotectData.restype = wt.BOOL

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
else:  # pragma: no cover
    _crypt32 = None
    _kernel32 = None


def _make_blob(data: bytes):
    """返回 (keepalive, blob)。keepalive 必须由调用方持有，否则内存会被回收。"""
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return buf, blob


def _blob_bytes(blob: _Blob) -> bytes:
    if not blob.pbData or not blob.cbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob: _Blob) -> None:
    if blob.pbData:
        _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def available() -> bool:
    return _crypt32 is not None


def protect(plaintext: str) -> str:
    """明文 -> base64 密文。失败抛 OSError。"""
    if _crypt32 is None:
        raise OSError("DPAPI 仅在 Windows 上可用")
    data = plaintext.encode("utf-8")
    keep_in, blob_in = _make_blob(data)
    keep_ent, blob_ent = _make_blob(_ENTROPY)
    blob_out = _Blob()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        _DESCRIPTION,
        ctypes.byref(blob_ent),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 调用失败")
    try:
        return base64.b64encode(_blob_bytes(blob_out)).decode("ascii")
    finally:
        _free(blob_out)
        del keep_in, keep_ent


def unprotect(ciphertext_b64: str) -> str:
    """base64 密文 -> 明文。失败抛 OSError。"""
    if _crypt32 is None:
        raise OSError("DPAPI 仅在 Windows 上可用")
    try:
        raw = base64.b64decode(ciphertext_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise OSError("密文不是合法的 base64") from exc

    keep_in, blob_in = _make_blob(raw)
    keep_ent, blob_ent = _make_blob(_ENTROPY)
    blob_out = _Blob()
    descr = wt.LPWSTR()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        ctypes.byref(descr),
        ctypes.byref(blob_ent),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(
            ctypes.get_last_error(),
            "CryptUnprotectData 调用失败（通常是换了 Windows 用户或换了电脑）",
        )
    try:
        return _blob_bytes(blob_out).decode("utf-8")
    finally:
        _free(blob_out)
        if descr:
            _kernel32.LocalFree(ctypes.cast(descr, ctypes.c_void_p))
        del keep_in, keep_ent


def acquire_single_instance(name: str = "Local\\CampusNetAutoLogin") -> object | None:
    """用命名互斥体保证只有一个后台实例。已有实例时返回 None。

    返回的句柄必须一直持有，否则互斥体会被释放。
    """
    if _kernel32 is None:
        return object()  # 非 Windows：不做限制
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle
