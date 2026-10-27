"""Windows 原生 WLAN API（wlanapi.dll）封装 —— 纯 ctypes，零第三方依赖。

## 为什么需要它（真实故障）

用 ``netsh wlan show networks`` 取 Wi-Fi 列表在两个场景下会**整体失败**：

1. **位置权限**：Windows 10/11 把 WLAN 信息列为"位置相关数据"。实测中文系统
   上 ``netsh wlan show interfaces`` 直接返回退出码 1 并打印：

       此 shell 命令需要位置权限才能访问 WLAN 信息。
       在「隐私和安全性」中的「位置」页面中启用位置服务。

   我们的解析自然一个字段都拿不到，界面上就变成"探测不到附近网络"。
2. **本地化文本**：netsh 是给人看的，键名随系统语言变化（``Description`` /
   ``说明`` / ``描述``），任何一处没覆盖到就解析失败。

``WlanGetAvailableNetworkList`` 这类 API 是**结构化数据**：不涉及本地化文本，
也不走 netsh 的命令行权限检查，因此更可靠也更快（几十毫秒）。

## 设计取舍

* 只读：扫描、读取当前连接状态；**连接/断开仍交给 netsh**
  （``WlanConnect`` 需要构造 profile XML，风险和工作量都不值得在这里做）。
* 任何一步失败都不抛异常，返回空结果 —— 由 :mod:`campusnet.wifi` 决定回落到 netsh。
* 非 Windows 平台、或 wlanapi.dll 不可用时，:func:`available` 返回 False。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, Structure, byref, c_bool, c_char, c_uint, c_ulong, c_void_p, c_wchar
from dataclasses import dataclass, field

ERROR_SUCCESS = 0
WLAN_API_VERSION_2 = 2

# WLAN_INTERFACE_STATE
_STATE_NAMES = {
    0: "not_ready",
    1: "connected",
    2: "ad_hoc_network_formed",
    3: "disconnecting",
    4: "disconnected",
    5: "associating",
    6: "discovering",
    7: "authenticating",
}
STATE_CONNECTED = 1
STATE_DISCONNECTED = 4

# WLAN_INTF_OPCODE
_OPCODE_AUTOCONF_ENABLED = 0
_OPCODE_INTERFACE_STATE = 1
_OPCODE_CURRENT_CONNECTION = 7

# DOT11_AUTH_ALGORITHM / DOT11_CIPHER_ALGORITHM 中表示"开放/不加密"的取值
_DOT11_AUTH_OPEN = 1
_DOT11_CIPHER_NONE = 1


class GUID(Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", c_char * 8),
    ]


class WLAN_INTERFACE_INFO(Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", c_wchar * 256),
        ("isState", c_uint),
    ]


class WLAN_INTERFACE_INFO_LIST(Structure):
    _fields_ = [
        ("dwNumberOfItems", c_ulong),
        ("dwIndex", c_ulong),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),   # 变长，按数量手工取
    ]


class DOT11_SSID(Structure):
    _fields_ = [("uSSIDLength", c_ulong), ("ucSSID", c_char * 32)]


class WLAN_AVAILABLE_NETWORK(Structure):
    _fields_ = [
        ("strProfileName", c_wchar * 256),
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", c_uint),
        ("uNumberOfBssids", c_ulong),
        ("bNetworkConnectable", c_bool),
        ("wlanNotConnectableReason", c_ulong),
        ("uNumberOfPhyTypes", c_ulong),
        ("dot11PhyTypes", c_uint * 8),
        ("bMorePhyTypes", c_bool),
        ("wlanSignalQuality", c_ulong),
        ("bSecurityEnabled", c_bool),
        ("dot11DefaultAuthAlgorithm", c_uint),
        ("dot11DefaultCipherAlgorithm", c_uint),
        ("dwFlags", c_ulong),
        ("dwReserved", c_ulong),
    ]


class WLAN_AVAILABLE_NETWORK_LIST(Structure):
    _fields_ = [
        ("dwNumberOfItems", c_ulong),
        ("dwIndex", c_ulong),
        ("Network", WLAN_AVAILABLE_NETWORK * 1),      # 变长
    ]


class WLAN_ASSOCIATION_ATTRIBUTES(Structure):
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", c_uint),
        ("dot11Bssid", c_char * 6),
        ("dot11PhyType", c_uint),
        ("uDot11PhyIndex", c_ulong),
        ("wlanSignalQuality", c_ulong),
        ("ulRxRate", c_ulong),
        ("ulTxRate", c_ulong),
    ]


class WLAN_SECURITY_ATTRIBUTES(Structure):
    _fields_ = [
        ("bSecurityEnabled", c_bool),
        ("bOneXEnabled", c_bool),
        ("dot11AuthAlgorithm", c_uint),
        ("dot11CipherAlgorithm", c_uint),
    ]


class WLAN_CONNECTION_ATTRIBUTES(Structure):
    _fields_ = [
        ("isState", c_uint),
        ("wlanConnectionMode", c_uint),
        ("strProfileName", c_wchar * 256),
        ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
        ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES),
    ]


@dataclass
class InterfaceInfo:
    description: str = ""
    state: int = 0
    ssid: str = ""
    signal: int = 0
    profile: str = ""

    @property
    def connected(self) -> bool:
        return self.state == STATE_CONNECTED and bool(self.ssid)

    @property
    def state_text(self) -> str:
        return _STATE_NAMES.get(self.state, str(self.state))


@dataclass
class AvailableNetwork:
    ssid: str
    signal: int = 0
    encrypted: bool = False
    profile: str = ""


_last_error = ""


def last_error() -> str:
    """最近一次失败的可读原因（给界面/CLI 显示用）。"""
    return _last_error


def _load():
    if sys.platform != "win32":
        return None
    try:
        dll = ctypes.WinDLL("wlanapi", use_last_error=True)
    except Exception:  # noqa: BLE001
        return None
    dll.WlanOpenHandle.argtypes = [
        c_ulong, c_void_p, POINTER(c_ulong), POINTER(c_void_p),
    ]
    dll.WlanOpenHandle.restype = c_ulong
    dll.WlanCloseHandle.argtypes = [c_void_p, c_void_p]
    dll.WlanCloseHandle.restype = c_ulong
    dll.WlanEnumInterfaces.argtypes = [
        c_void_p, c_void_p, POINTER(POINTER(WLAN_INTERFACE_INFO_LIST)),
    ]
    dll.WlanEnumInterfaces.restype = c_ulong
    dll.WlanQueryInterface.argtypes = [
        c_void_p, POINTER(GUID), c_uint, c_void_p,
        POINTER(c_ulong), POINTER(c_void_p), POINTER(c_uint),
    ]
    dll.WlanQueryInterface.restype = c_ulong
    dll.WlanGetAvailableNetworkList.argtypes = [
        c_void_p, POINTER(GUID), c_ulong,
        c_void_p, POINTER(POINTER(WLAN_AVAILABLE_NETWORK_LIST)),
    ]
    dll.WlanGetAvailableNetworkList.restype = c_ulong
    dll.WlanFreeMemory.argtypes = [c_void_p]
    dll.WlanFreeMemory.restype = None
    return dll


_dll = None
_loaded = False


def _api():
    global _dll, _loaded
    if not _loaded:
        _dll = _load()
        _loaded = True
    return _dll


def available() -> bool:
    """wlanapi 是否可用（非 Windows / DLL 缺失时为 False）。"""
    return _api() is not None


class _Handle:
    def __init__(self, dll):
        self.dll = dll
        self.handle = c_void_p()
        negotiated = c_ulong()
        code = dll.WlanOpenHandle(WLAN_API_VERSION_2, None, byref(negotiated), byref(self.handle))
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            if self.handle:
                self.dll.WlanCloseHandle(self.handle, None)
        except Exception:  # noqa: BLE001
            pass
        return False


def _ssid_text(ssid: DOT11_SSID) -> str:
    try:
        raw = bytes(ssid.ucSSID[: ssid.uSSIDLength])
        for encoding in ("utf-8", "gbk", "mbcs"):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def interfaces() -> list[InterfaceInfo]:
    """枚举无线接口及当前连接状态。失败时返回空列表并设置 :func:`last_error`。"""
    global _last_error
    dll = _api()
    if dll is None:
        _last_error = "wlanapi.dll 不可用"
        return []
    try:
        with _Handle(dll) as opened:
            if opened.code != ERROR_SUCCESS:
                _last_error = f"WlanOpenHandle 失败（错误码 {opened.code}）"
                return []
            plist = POINTER(WLAN_INTERFACE_INFO_LIST)()
            code = dll.WlanEnumInterfaces(opened.handle, None, byref(plist))
            if code != ERROR_SUCCESS or not plist:
                _last_error = f"WlanEnumInterfaces 失败（错误码 {code}）"
                return []
            try:
                count = plist.contents.dwNumberOfItems
                size = ctypes.sizeof(WLAN_INTERFACE_INFO)
                base = ctypes.addressof(plist.contents) + ctypes.sizeof(c_ulong) * 2
                out: list[InterfaceInfo] = []
                for index in range(count):
                    raw = (WLAN_INTERFACE_INFO * 1).from_address(base + index * size)
                    info = raw[0]
                    item = InterfaceInfo(
                        description=str(info.strInterfaceDescription),
                        state=int(info.isState),
                    )
                    _fill_connection(dll, opened.handle, info.InterfaceGuid, item)
                    out.append(item)
                return out
            finally:
                dll.WlanFreeMemory(plist)
    except Exception as exc:  # noqa: BLE001
        _last_error = f"读取无线接口异常：{exc}"
        return []


def _fill_connection(dll, handle, guid, item: InterfaceInfo) -> None:
    """填充当前连接的 SSID / 信号（未连接时 WlanQueryInterface 返回 5023）。"""
    size = c_ulong()
    data = c_void_p()
    opcode_type = c_uint()
    code = dll.WlanQueryInterface(
        handle, byref(guid), _OPCODE_CURRENT_CONNECTION, None,
        byref(size), byref(data), byref(opcode_type),
    )
    if code != ERROR_SUCCESS or not data:
        return
    try:
        attributes = ctypes.cast(data, POINTER(WLAN_CONNECTION_ATTRIBUTES)).contents
        item.state = int(attributes.isState)
        item.ssid = _ssid_text(attributes.wlanAssociationAttributes.dot11Ssid)
        item.signal = int(attributes.wlanAssociationAttributes.wlanSignalQuality)
        item.profile = str(attributes.strProfileName)
    finally:
        dll.WlanFreeMemory(data)


def scan() -> list[AvailableNetwork]:
    """扫描附近网络（按信号降序）。失败时返回空列表并设置 :func:`last_error`。"""
    global _last_error
    dll = _api()
    if dll is None:
        _last_error = "wlanapi.dll 不可用"
        return []
    found: dict[str, AvailableNetwork] = {}
    try:
        with _Handle(dll) as opened:
            if opened.code != ERROR_SUCCESS:
                _last_error = f"WlanOpenHandle 失败（错误码 {opened.code}）"
                return []
            plist_ifaces = POINTER(WLAN_INTERFACE_INFO_LIST)()
            code = dll.WlanEnumInterfaces(opened.handle, None, byref(plist_ifaces))
            if code != ERROR_SUCCESS or not plist_ifaces:
                _last_error = f"WlanEnumInterfaces 失败（错误码 {code}）"
                return []
            try:
                count = plist_ifaces.contents.dwNumberOfItems
                size = ctypes.sizeof(WLAN_INTERFACE_INFO)
                base = ctypes.addressof(plist_ifaces.contents) + ctypes.sizeof(c_ulong) * 2
                for index in range(count):
                    raw = (WLAN_INTERFACE_INFO * 1).from_address(base + index * size)
                    _scan_interface(dll, opened.handle, raw[0].InterfaceGuid, found)
            finally:
                dll.WlanFreeMemory(plist_ifaces)
    except Exception as exc:  # noqa: BLE001
        _last_error = f"扫描无线网络异常：{exc}"
        return []
    return sorted(found.values(), key=lambda n: (-n.signal, n.ssid.lower()))


def _scan_interface(dll, handle, guid, found: dict) -> None:
    plist = POINTER(WLAN_AVAILABLE_NETWORK_LIST)()
    # 0x0000_0001 = WLAN_AVAILABLE_NETWORK_INCLUDE_ALL_ADHOC_PROFILES? 这里用默认 0
    code = dll.WlanGetAvailableNetworkList(handle, byref(guid), 0, None, byref(plist))
    if code != ERROR_SUCCESS or not plist:
        _last_error_set(f"WlanGetAvailableNetworkList 失败（错误码 {code}）")
        return
    try:
        count = plist.contents.dwNumberOfItems
        size = ctypes.sizeof(WLAN_AVAILABLE_NETWORK)
        base = ctypes.addressof(plist.contents) + ctypes.sizeof(c_ulong) * 2
        for index in range(count):
            raw = (WLAN_AVAILABLE_NETWORK * 1).from_address(base + index * size)
            net = raw[0]
            ssid = _ssid_text(net.dot11Ssid)
            if not ssid:
                continue      # 隐藏网络
            signal = int(net.wlanSignalQuality)
            security = bool(net.bSecurityEnabled)
            open_network = (
                not security
                or (
                    int(net.dot11DefaultAuthAlgorithm) == _DOT11_AUTH_OPEN
                    and int(net.dot11DefaultCipherAlgorithm) == _DOT11_CIPHER_NONE
                )
            )
            item = found.get(ssid)
            if item is None:
                found[ssid] = AvailableNetwork(
                    ssid=ssid, signal=signal, encrypted=not open_network,
                    profile=str(net.strProfileName),
                )
            else:
                # 同一 SSID 有多个热点时取最强信号
                item.signal = max(item.signal, signal)
    finally:
        dll.WlanFreeMemory(plist)


def _last_error_set(message: str) -> None:
    global _last_error
    if not _last_error:
        _last_error = message
