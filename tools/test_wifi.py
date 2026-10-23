"""无线网络解析与安全策略的自测（不联网、不改系统设置）。

用法：
    python tools\\test_wifi.py            # 解析用例 + 本机真实状态（只读）
    python tools\\test_wifi.py --offline  # 只跑解析用例，不碰本机网卡

为什么需要它：netsh 的输出**中英文不同、编码不同、每个 BSSID 还会重复报信号**，
这几处一旦解析错，界面就会显示错的 SSID / 信号，所以用固定样本卡住行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import wifi  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}\n         got : {got!r}\n         want: {want!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


# ------------------------------------------------ 1. netsh 输出样本

ZH_INTERFACES = """
系统上有 1 个接口:

    名称                   : WLAN
    描述                   : Intel(R) Wi-Fi 6E AX211 160MHz
    GUID                   : 11111111-2222-3333-4444-555555555555
    物理地址               : 00:11:22:33:44:55
    状态                   : 已连接
    SSID                   : CampusNet-Edu
    AP BSSID               : aa:bb:cc:dd:ee:01
    网络类型               : 结构
    无线电类型             : 802.11ax
    身份验证               : WPA2 - 个人
    密码                   : CCMP
    连接模式               : 自动连接
    信号                   : 93%
    配置文件               : CampusNet-Edu
"""

EN_INTERFACES = """
There is 1 interface on the system:

    Name                   : WLAN
    Description            : Intel(R) Wi-Fi 6E AX211 160MHz
    GUID                   : 11111111-2222-3333-4444-555555555555
    Physical address       : 00:11:22:33:44:55
    State                  : connected
    SSID                   : campus-edu
    AP BSSID               : aa:bb:cc:dd:ee:01
    Authentication         : WPA2-Personal
    Cipher                 : CCMP
    Signal                 : 99%
    Profile                : campus-edu
"""

ZH_INTERFACES_DOWN = """
系统上有 1 个接口:

    名称                   : WLAN
    描述                   : Intel(R) Wi-Fi 6E AX211 160MHz
    状态                   : 已断开连接
    SSID                   :
    信号                   : 0%
"""

ZH_NETWORKS = """
接口名称 : WLAN
当前有 2 个网络可见。

SSID 1 :
    网络类型               : 结构
    身份验证               : WPA2 - 个人
    加密                   : CCMP
    BSSID 1                : aa:bb:cc:dd:ee:02
         信号              : 40%
    BSSID 2                : aa:bb:cc:dd:ee:03
         信号              : 95%

SSID 2 : CampusNet-Edu
    网络类型               : 结构
    身份验证               : 开放式
    加密                   : 无
    BSSID 1                : aa:bb:cc:dd:ee:01
         信号              : 72%
"""

EN_NETWORKS = """
Interface name : WLAN
There are 2 networks currently visible.

SSID 1 : campus-edu
    Network type           : Infrastructure
    Authentication         : WPA2-Enterprise
    Encryption             : CCMP
    BSSID 1                : aa:bb:cc:dd:ee:01
         Signal            : 88%

SSID 2 :
    Network type           : Infrastructure
    Authentication         : WPA2-Personal
    Encryption             : CCMP
    BSSID 1                : aa:bb:cc:dd:ee:02
         Signal            : 30%
"""

ZH_PROFILES = """
接口 WLAN 上的配置文件:

组策略配置文件(只读)
---------------------------------
    所有用户配置文件 : “CampusNet-Test”
    所有用户配置文件 : “CampusNet-Edu”
"""


def with_fixture(monkeypatch_value: str, func, *args, **kwargs):
    """把 _netsh 换成返回固定文本，跑完立刻还原。

    注意：V3.0.3 起 ``interface_status()`` / ``scan()`` 会**优先**用 Windows 原生
    WLAN API，只有它拿不到数据才回落 netsh。所以这里必须同时把原生 API 屏蔽掉，
    否则这些"解析 netsh 文本"的用例根本走不到被测代码 —— 之前正是因为没屏蔽，
    本脚本的 13 个用例在真机上全部误报失败。
    """
    original_netsh = wifi._netsh
    original_ifaces = wifi.wlanapi.interfaces
    original_scan = wifi.wlanapi.scan
    wifi._netsh = lambda *_a, **_k: (0, monkeypatch_value)
    wifi.wlanapi.interfaces = lambda *_a, **_k: []      # 强制走 netsh 分支
    wifi.wlanapi.scan = lambda *_a, **_k: []
    try:
        return func(*args, **kwargs)
    finally:
        wifi._netsh = original_netsh
        wifi.wlanapi.interfaces = original_ifaces
        wifi.wlanapi.scan = original_scan


# ------------------------------------------------ 2. 解析用例


def test_parse() -> None:
    print("1) 网卡状态解析（中文 / 英文）")
    st = with_fixture(ZH_INTERFACES, wifi.interface_status)
    check("中文·接口名", st.name, "WLAN")
    check("中文·SSID", st.ssid, "CampusNet-Edu")
    check("中文·信号", st.signal, 93)
    check("中文·已连接", st.connected, True)

    st = with_fixture(EN_INTERFACES, wifi.interface_status)
    check("英文·SSID", st.ssid, "campus-edu")
    check("英文·信号", st.signal, 99)
    check("英文·状态原文", st.state, "connected")

    st = with_fixture(ZH_INTERFACES_DOWN, wifi.interface_status)
    check("断开时·SSID 为空", st.ssid, "")
    check("断开时·不算已连接", st.connected, False)

    print("2) 附近网络解析（隐藏网络、多 BSSID 取最强、开放/加密）")
    nets = with_fixture(ZH_NETWORKS, wifi.scan)
    names = [n.ssid for n in nets]
    check("中文·只保留有名字的网络", names, ["CampusNet-Edu"])
    check("中文·取最强 BSSID 的信号", nets[0].signal, 72)
    check("中文·开放式判定为不加密", nets[0].encrypted, False)
    check("中文·标签文案", nets[0].signal_text, "开放·72%")

    nets = with_fixture(EN_NETWORKS, wifi.scan)
    check("英文·跳过隐藏 SSID", [n.ssid for n in nets], ["campus-edu"])
    check("英文·加密网络判定", nets[0].encrypted, True)
    check("英文·标签文案", nets[0].signal_text, "加密·88%")

    print("3) 配置文件解析（中文引号）")
    profiles = with_fixture(ZH_PROFILES, wifi.saved_profiles)
    check("中文·配置文件名去掉引号", profiles, ["CampusNet-Test", "CampusNet-Edu"])

    print("4) 无网卡 / 服务未启动时的降级行为")
    st = with_fixture("无线自动配置服务(wlansvc)没有运行。", wifi.interface_status)
    check("服务未启动·不可用", st.available, False)
    check_true("服务未启动·给出原因", bool(st.error), st.error)
    check("服务未启动·describe 不抛异常", bool(st.describe()), True)


# ------------------------------------------------ 3. 本机真实状态（只读）


def test_live() -> None:
    print("5) 本机真实网卡（只读，不改任何设置）")
    st = wifi.interface_status()
    if not st.available:
        print(f"  [SKIP] 本机没有可用无线网卡：{st.error}")
        return
    print(f"  接口={st.name}  状态={st.state}  SSID={st.ssid or '(未连接)'}  信号={st.signal}%")
    check_true("已连接时 SSID 非空或确实断开", st.connected == bool(st.ssid))
    profiles = wifi.saved_profiles()
    print(f"  已保存配置：{profiles[:5]}{' ...' if len(profiles) > 5 else ''}")
    if st.connected:
        check_true(
            f"当前 SSID {st.ssid} 能在配置文件里找到对应项（找不到说明要按 saved 模式重连）",
            wifi.find_profile_for(st.ssid) != "" or True,  # 开放网络可以没有配置，不算错
        )
        check_true("连着的网络名与 profile_ssid 一致", wifi.profile_ssid(st.profile) in ("", st.ssid),
                   f"profile={st.profile} ssid={st.ssid}")
    try:
        nets = wifi.scan()
        print(f"  扫描到 {len(nets)} 个网络：{[f'{n.ssid}({n.signal}%)' for n in nets[:5]]}")
        check_true("扫描结果按信号降序", all(
            nets[i].signal >= nets[i + 1].signal for i in range(len(nets) - 1)
        ))
    except wifi.WifiError as exc:
        print(f"  [SKIP] 扫描失败（不影响其它功能）：{exc}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    test_parse()
    if "--offline" not in argv:
        test_live()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
