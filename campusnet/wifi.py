"""Windows 无线网络操作：扫描、连接、状态查询（封装 netsh wlan）。

三条设计约定，和项目其它模块保持一致：
- 只用标准库（subprocess + netsh），不引入任何第三方依赖；
- 所有子进程调用都带 CREATE_NO_WINDOW，不会闪出控制台窗口；
- netsh 的输出是**本地化**的（中文系统出中文、英文系统出英文），编码还随系统
  代码页变化，所以解析时按"键值对"归一化处理，而不是死记某个语言的文案。

对外接口：
    interface_status()            当前无线网卡状态（是否连着、连的哪个、信号多少）
    scan()                        扫描附近网络（按信号强度降序）
    saved_profiles()              Windows 里已保存的无线配置文件名称
    find_profile_for(ssid)        按 SSID 找可用的配置文件
    connect(ssid, mode)           连接到指定 SSID（mode = "open" / "saved"）
    ensure_connected(ssid, mode)  已连上就直接返回，否则尝试连接
    open_wifi_settings()          打开 Windows 的 Wi-Fi 设置页
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import wlanapi

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# netsh 输出里的"位置权限"提示（中文/英文），用来把系统级限制翻译成可操作的说明
_LOCATION_HINTS = (
    "位置权限",
    "位置服务",
    "privacy-location",
    "location permission",
    "location service",
)


def location_permission_blocked(text: str) -> bool:
    """netsh 是否因为"缺位置权限"拒绝提供 WLAN 信息。"""
    low = (text or "").lower()
    return any(hint.lower() in low for hint in _LOCATION_HINTS)


LOCATION_PERMISSION_HINT = (
    "Windows 把无线网络信息归为「位置相关数据」，当前系统设置不允许本程序读取。"
    "请在「设置 → 隐私和安全性 → 位置」中打开「位置服务」，并打开"
    "「让桌面应用访问你的位置」，然后重试。"
)

# netsh 的输出编码随系统代码页走：中文系统是 cp936，UTF-8 beta 选项下是 utf-8。
# 先试 utf-8 再试 OEM/GBK，最后兜底 replace，保证任何系统上都读得出文字。
_ENCODINGS = ("utf-8", "oem", "cp936", "gbk", "mbcs")

_OPEN_WORDS = ("open", "开放式", "开放", "无")  # "身份验证 = 开放式" 表示不加密


class WifiError(RuntimeError):
    """netsh 调用失败（服务没开、没有无线网卡等）。"""


def _decode(raw: bytes) -> str:
    if not raw:
        return ""
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _netsh(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """跑一条 netsh 命令，返回 (退出码, 文本输出)。"""
    try:
        proc = subprocess.run(
            ["netsh", *args],
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:  # 理论上 Windows 一定有 netsh
        raise WifiError("找不到 netsh.exe") from exc
    except subprocess.TimeoutExpired as exc:
        raise WifiError(f"netsh 超时：{' '.join(args)}") from exc
    except Exception as exc:  # noqa: BLE001
        raise WifiError(f"netsh 调用失败：{exc}") from exc
    text = _decode((proc.stdout or b"") + (proc.stderr or b""))
    return proc.returncode, text


def _split_pair(line: str) -> tuple[str, str] | None:
    """把 "    名称 : WLAN" 拆成 ("名称", "WLAN")。全角冒号也认。"""
    for sep in (" : ", "：", ":"):
        idx = line.find(sep)
        if idx <= 0:
            continue
        key = line[:idx].strip()
        if not key or key.endswith(":"):
            continue
        # "BSSID 1 : f4:fc:..." 这种值里带冒号的不会被切错（只切第一个分隔符）
        return key.lower(), line[idx + len(sep):].strip()
    return None


def _norm_key(key: str) -> str:
    """去掉键名里的空白，方便中文/英文一起比对。"""
    return re.sub(r"\s+", " ", key).strip().lower()


# netsh 有时会给 SSID 加引号，而且中英文系统的引号还不一样（"x" / “x” / 「x」）
_QUOTES = "\"'“”‘’「」"


def _unquote(text: str) -> str:
    return (text or "").strip().strip(_QUOTES)


# 键名表：左边是 netsh 的实际输出（实测中英文，含简繁差异），右边是内部字段名
# 注意：中文 netsh 用的是「名称 / 说明 / 状态 / 信号 / 身份验证 / 加密」，
# 不是「描述」——早期只写了「描述」，导致网卡型号显示成 '-'。
_KEY_NAME = {"name", "名称", "接口名称"}
_KEY_DESC = {"description", "描述", "说明", "說明", "描述说明"}
_KEY_STATE = {"state", "状态", "狀態"}
_KEY_SSID = {"ssid"}
_KEY_SIGNAL = {"signal", "信号", "訊號"}
_KEY_AUTH = {"authentication", "身份验证", "身份驗證", "验证"}
_KEY_ENCRYPTION = {"encryption", "加密", "加密方式"}
_KEY_PROFILE = {"profile", "配置文件", "設定檔"}
_KEY_GUID = {"guid"}
_RE_SSID_INDEX = re.compile(r"^ssid\s*(\d+)$")          # "ssid 1"


@dataclass
class InterfaceStatus:
    """无线网卡当前状态。"""

    available: bool = False        # 有没有无线网卡 / netsh 能不能用
    name: str = ""                 # 接口名，如 WLAN
    description: str = ""          # 网卡型号
    state: str = ""                # netsh 原文（connected / 已连接 / disconnected ...）
    ssid: str = ""
    signal: int = 0                # 0-100
    authentication: str = ""
    profile: str = ""
    error: str = ""

    @property
    def connected(self) -> bool:
        """是否已经连上某个网络。"""
        return bool(self.ssid) and not self.error

    def describe(self) -> str:
        if not self.available:
            return self.error or "未检测到可用的无线网卡"
        if self.connected:
            return f"已连接 {self.ssid}"
        return "无线网络未连接"


@dataclass
class Network:
    """扫描到的一个网络。"""

    ssid: str
    signal: int = 0
    authentication: str = ""
    encryption: str = ""
    hidden: bool = False

    @property
    def encrypted(self) -> bool:
        text = f"{self.authentication} {self.encryption}".lower()
        if any(w in text for w in _OPEN_WORDS):
            return False
        return bool(text.strip())

    @property
    def kind_text(self) -> str:
        return "加密" if self.encrypted else "开放"

    @property
    def signal_text(self) -> str:
        return f"{self.kind_text}·{self.signal}%"


def interface_status() -> InterfaceStatus:
    """读取无线网卡状态。任何异常都转换成 error 字段，不抛。

    取值顺序：
    1. **Windows 原生 WLAN API**（结构化数据，不受位置权限与本地化文本影响）；
    2. 回落 ``netsh wlan show interfaces``（老系统 / DLL 不可用时）。
    """
    # ---- 1. 原生 API ----
    try:
        interfaces = wlanapi.interfaces()
    except Exception:  # noqa: BLE001
        interfaces = []
    if interfaces:
        first = next((item for item in interfaces if item.connected), interfaces[0])
        return InterfaceStatus(
            available=True,
            name="WLAN",
            description=first.description,
            state=first.state_text,
            ssid=first.ssid,
            signal=first.signal,
            profile=first.profile,
        )

    # ---- 2. 回落 netsh ----
    st = InterfaceStatus()
    try:
        code, text = _netsh(["wlan", "show", "interfaces"])
    except WifiError as exc:
        st.error = str(exc)
        return st

    if location_permission_blocked(text):
        st.error = LOCATION_PERMISSION_HINT
        return st

    low = text.lower()
    if code != 0 or ("no wireless interface" in low) or ("没有无线" in text) or ("无线自动配置服务" in text and "没有运行" in text):
        st.error = _first_meaningful_line(text) or "未检测到可用的无线网卡"
        return st

    st.available = True
    for line in text.splitlines():
        pair = _split_pair(line)
        if not pair:
            continue
        key, val = _norm_key(pair[0]), pair[1].strip()
        if key in _KEY_NAME and not st.name:
            st.name = val
        elif key in _KEY_DESC and not st.description:
            st.description = val
        elif key in _KEY_STATE:
            st.state = val
        elif key in _KEY_SSID:          # 精确匹配，避免把 "AP BSSID" 当成 SSID
            st.ssid = val
        elif key in _KEY_SIGNAL:
            st.signal = _parse_percent(val)
        elif key in _KEY_AUTH:
            st.authentication = val
        elif key in _KEY_PROFILE:
            st.profile = val

    # 有些版本在断开时仍会打印 "SSID : "（空值），上面已经取到空字符串了
    st.ssid = _unquote(st.ssid)
    return st


def _first_meaningful_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _parse_percent(val: str) -> int:
    m = re.search(r"(\d{1,3})", val or "")
    if not m:
        return 0
    try:
        return max(0, min(100, int(m.group(1))))
    except Exception:  # noqa: BLE001
        return 0


def scan(timeout: float = 25.0) -> list[Network]:
    """扫描附近网络，按信号强度降序返回。

    取值顺序同 :func:`interface_status`：**先原生 WLAN API**，失败再回落 netsh。
    两者都失败时抛 :class:`WifiError`，并把系统级限制（如位置权限）翻译成
    可照做的提示 —— 而不是丢一句 "netsh 没有返回内容"。
    """
    # ---- 1. 原生 API ----
    try:
        native = wlanapi.scan()
    except Exception:  # noqa: BLE001
        native = []
    if native:
        return [
            Network(
                ssid=item.ssid,
                signal=item.signal,
                authentication="加密网络" if item.encrypted else "open",
                encryption="encrypted" if item.encrypted else "none",
            )
            for item in native
        ]

    # ---- 2. 回落 netsh ----
    code, text = _netsh(["wlan", "show", "networks", "mode=bssid"], timeout=timeout)
    if location_permission_blocked(text):
        raise WifiError(LOCATION_PERMISSION_HINT)
    if code != 0 and not text.strip():
        raise WifiError("扫描失败：netsh 没有返回内容（无线服务可能没启动）")

    found: dict[str, Network] = {}
    current: Network | None = None
    for raw_line in text.splitlines():
        pair = _split_pair(raw_line)
        if not pair:
            continue
        key, val = _norm_key(pair[0]), pair[1].strip()

        m = _RE_SSID_INDEX.match(key)
        if m:
            # 新的一块：SSID 1 : 名字     （名字可能为空 = 隐藏网络）
            name = _unquote(val)
            current = None if not name else found.get(name)
            if current is None and name:
                current = Network(ssid=name)
                found[name] = current
            if current is None and not name:
                current = Network(ssid="", hidden=True)  # 占位，后面的信号都忽略
            continue

        if current is None or current.hidden:
            continue

        if key in _KEY_AUTH:
            current.authentication = val
        elif key in _KEY_ENCRYPTION:
            current.encryption = val
        elif key in _KEY_SIGNAL:
            # 信号是**每个 BSSID 各报一条**，同一个 SSID 有多个热点时取最强的那个
            current.signal = max(current.signal, _parse_percent(val))

    return sorted(found.values(), key=lambda n: (-n.signal, n.ssid.lower()))


def saved_profiles() -> list[str]:
    """Windows 已保存的无线配置文件名（通常等于 SSID）。"""
    try:
        code, text = _netsh(["wlan", "show", "profiles"])
    except WifiError:
        return []
    if code != 0:
        return []
    out: list[str] = []
    for line in text.splitlines():
        pair = _split_pair(line)
        if not pair:
            continue
        key, val = _norm_key(pair[0]), pair[1].strip()
        if key in ("all user profile", "所有用户配置文件", "当前用户配置文件", "current user profile"):
            if val:
                out.append(_unquote(val))
    return out


def profile_ssid(profile: str) -> str:
    """读某个配置文件里真正的 SSID（配置文件名不保证等于 SSID）。"""
    if not profile:
        return ""
    try:
        code, text = _netsh(["wlan", "show", "profile", f"name={profile}"])
    except WifiError:
        return ""
    if code != 0:
        return ""
    for line in text.splitlines():
        pair = _split_pair(line)
        if not pair:
            continue
        key, val = _norm_key(pair[0]), pair[1].strip()
        # "SSID 名称 : xxx" / "SSID name : xxx"（英文版会给 SSID 加引号，中文版加「」）
        if key in ("ssid name", "ssid 名称"):
            return _unquote(val)
    return ""


def find_profile_for(ssid: str) -> str:
    """按 SSID 找到可以直连的配置文件名称；没有就返回空串。"""
    ssid = (ssid or "").strip()
    if not ssid:
        return ""
    names = saved_profiles()
    # 绝大多数情况下配置文件名就是 SSID，先走这条快路
    for name in names:
        if name == ssid:
            return name
    for name in names:
        if profile_ssid(name) == ssid:
            return name
    return ""


_OPEN_PROFILE_XML = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{name}</name>
    <SSIDConfig>
        <SSID>
            <name>{name}</name>
        </SSID>
    </SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM>
        <security>
            <authEncryption>
                <authentication>open</authentication>
                <encryption>none</encryption>
                <useOneX>false</useOneX>
            </authEncryption>
        </security>
    </MSM>
</WLANProfile>
"""


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&apos;")
    )


def _add_open_profile(ssid: str) -> tuple[str, str]:
    """给"开放网络"临时建一个配置文件（Windows 直连开放网络必须先有配置）。

    返回 (配置文件名, 说明)。失败时配置文件名是空串。
    """
    xml = _OPEN_PROFILE_XML.format(name=_xml_escape(ssid))
    tmp = Path(tempfile.gettempdir()) / f"campusnet_wifi_{abs(hash(ssid)) & 0xFFFFFF}.xml"
    try:
        tmp.write_text(xml, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return "", f"写临时配置文件失败：{exc}"
    try:
        code, text = _netsh(["wlan", "add", "profile", f"filename={tmp}", "user=current"])
    finally:
        try:
            tmp.unlink()
        except Exception:  # noqa: BLE001
            pass
    if code != 0:
        return "", _first_meaningful_line(text) or "创建开放网络配置文件失败。"
    return ssid, ""


def connect(ssid: str, mode: str = "open", *, wait: float = 9.0) -> tuple[bool, str]:
    """连接到指定 SSID。返回 (是否连上, 说明)。

    mode="open"  ：开放网络 —— 没有已保存配置时会临时建一个
    mode="saved" ：只用 Windows 里已保存的配置（要密码的网络必须走这条）
    """
    ssid = (ssid or "").strip()
    if not ssid:
        return False, "未设置目标网络"

    st = interface_status()
    if not st.available:
        return False, st.error or "未检测到可用的无线网卡"
    if st.connected and st.ssid == ssid:
        return True, "已连接到该网络。"

    profile = find_profile_for(ssid)
    if not profile:
        if mode != "open":
            return False, f"Windows 中没有 {ssid} 的已保存配置，请先在系统设置中连接一次。"
        profile, err = _add_open_profile(ssid)
        if not profile:
            return False, err or "无法为该网络创建配置文件。"

    try:
        code, text = _netsh(["wlan", "connect", f"name={profile}", f"ssid={ssid}"], timeout=30)
    except WifiError as exc:
        return False, str(exc)

    # netsh 即使失败也常返回 0，所以只以"网卡状态真的变了"为准
    deadline = time.time() + max(2.0, wait)
    while time.time() < deadline:
        time.sleep(0.7)
        now = interface_status()
        if now.connected and now.ssid == ssid:
            return True, "已连接。"
    detail = _first_meaningful_line(text)
    if code != 0 and detail:
        return False, detail
    return False, f"连接超时：{ssid} 可能不在范围内，或已保存的配置不正确。"


def disconnect() -> tuple[bool, str]:
    try:
        code, text = _netsh(["wlan", "disconnect"], timeout=20)
    except WifiError as exc:
        return False, str(exc)
    return code == 0, _first_meaningful_line(text) or "已断开"


def ensure_connected(
    ssid: str,
    mode: str = "open",
    *,
    log=None,
    wait: float = 9.0,
) -> tuple[bool, str]:
    """保证连着目标 SSID：已连上就立刻返回，否则先看范围里有没有，再尝试连接。

    给后台守护用，所以不抛异常，所有失败都变成返回值。
    """
    ssid = (ssid or "").strip()
    if not ssid:
        return False, "未设置目标网络"
    try:
        st = interface_status()
        if st.connected and st.ssid == ssid:
            return True, "已连接。"
        if not st.available:
            return False, st.error or "未检测到可用的无线网卡"

        visible = True
        try:
            visible = any(n.ssid == ssid for n in scan())
        except WifiError:
            visible = True  # 扫描不出来不代表没在范围里，交给 connect 判断
        if not visible and not find_profile_for(ssid):
            return False, f"未扫描到 {ssid}"
        return connect(ssid, mode, wait=wait)
    except Exception as exc:  # noqa: BLE001
        if log is not None:
            try:
                log.warning("WiFi 操作异常：%s", exc)
            except Exception:  # noqa: BLE001
                pass
        return False, str(exc)


def open_wifi_settings() -> bool:
    """打开 Windows 自带的 Wi-Fi 设置页。"""
    try:
        os.startfile("ms-settings:network-wifi")  # noqa: S606  (Windows 专用)
        return True
    except Exception:  # noqa: BLE001
        return False


def wireless_service_hint() -> str:
    """给界面用的一句排障提示。"""
    return "如果一直连不上，请确认「WLAN AutoConfig（WlanSvc）」服务在运行，且没开飞行模式。"
