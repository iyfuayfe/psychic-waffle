"""Wi-Fi 探测的兜底与"位置权限"处理测试。

背景（真实故障）：用户反馈"无法探测附近无线网络"。实测原因有两个：

1. Windows 把 WLAN 信息（SSID 列表、当前连接）归为**位置相关数据**。
   ``netsh wlan show interfaces`` 因此在受限环境下直接返回退出码 1 并打印
   "此 shell 命令需要位置权限才能访问 WLAN 信息…"，我们一个字段都解析不到。
2. netsh 是**本地化文本**：中文系统用「说明」而不是「描述」，早期版本没覆盖，
   网卡型号显示成 '-'。

对策：优先使用 Windows 原生 WLAN API（结构化、无本地化文本），netsh 作为兜底；
两种方式都失败时，把系统级限制翻译成用户能照做的提示。
"""

from __future__ import annotations

import unittest

from tests.support import PROJECT_ROOT  # noqa: F401  (确保 sys.path 就绪)

from campusnet import wifi, wlanapi

LOCATION_TEXT = """
系统上有 1 个接口:
此 shell 命令需要位置权限才能访问 WLAN 信息。在「隐私和安全性」中的「位置」页面中启用位置服务。
请打开「设置」应用中的「位置」页面 URI:ms-settings:privacy-location
"""

EN_LOCATION_TEXT = (
    "There is 1 interface on the system:\n"
    "This command requires location permission to access WLAN information.\n"
)


class LocationPermissionTests(unittest.TestCase):
    def test_chinese_location_message_is_recognised(self):
        self.assertTrue(wifi.location_permission_blocked(LOCATION_TEXT))

    def test_english_location_message_is_recognised(self):
        self.assertTrue(wifi.location_permission_blocked(EN_LOCATION_TEXT))

    def test_normal_output_is_not_misdetected(self):
        self.assertFalse(wifi.location_permission_blocked("    SSID : campus\n    信号 : 88%"))
        self.assertFalse(wifi.location_permission_blocked(""))
        self.assertFalse(wifi.location_permission_blocked(None))

    def test_hint_tells_user_where_to_fix_it(self):
        self.assertIn("位置", wifi.LOCATION_PERMISSION_HINT)
        self.assertIn("隐私和安全性", wifi.LOCATION_PERMISSION_HINT)
        self.assertIn("让桌面应用访问你的位置", wifi.LOCATION_PERMISSION_HINT)


class LocalisedKeyTests(unittest.TestCase):
    """中文 netsh 的键名必须被识别（实测用词：名称/说明/状态/信号/身份验证/加密）。"""

    def test_chinese_keys_are_mapped(self):
        self.assertIn("说明", wifi._KEY_DESC)
        self.assertIn("描述", wifi._KEY_DESC)
        self.assertIn("description", wifi._KEY_DESC)
        self.assertIn("状态", wifi._KEY_STATE)
        self.assertIn("信号", wifi._KEY_SIGNAL)
        self.assertIn("身份验证", wifi._KEY_AUTH)
        self.assertIn("加密", wifi._KEY_ENCRYPTION)

    def test_split_pair_handles_chinese_output(self):
        pair = wifi._split_pair("    说明            : Intel(R) Wi-Fi 6E AX211 160MHz")
        self.assertIsNotNone(pair)
        self.assertEqual(pair[0], "说明")
        self.assertIn("Intel", pair[1])

    def test_split_pair_does_not_split_bssid_value(self):
        pair = wifi._split_pair("    BSSID 1                 : aa:bb:cc:dd:ee:01")
        self.assertEqual(pair[0], "bssid 1")
        self.assertEqual(pair[1], "aa:bb:cc:dd:ee:01")

    def test_netsh_parser_reads_describe_state_ssid(self):
        """用真实的中文 netsh 输出片段，验证解析结果（回归『网卡型号显示 -』）。"""
        sample = """
系统上有 1 个接口:

    名称                   : WLAN
    说明            : Intel(R) Wi-Fi 6E AX211 160MHz
    GUID                   : 11111111-2222-3333-4444-555555555555
    物理地址       : 00:11:22:33:44:55
    界面类型         : 主要
    状态                  : 已连接
    SSID                   : CampusNet-Test
    AP BSSID               : aa:bb:cc:dd:ee:01
    波段                   : 5 GHz
    身份验证                : WPA2 - 个人
    加密                    : CCMP
    信号                 : 88%
    配置文件              : CampusNet-Test
"""
        parsed = {}
        for line in sample.splitlines():
            pair = wifi._split_pair(line)
            if not pair:
                continue
            key = wifi._norm_key(pair[0])
            parsed[key] = pair[1].strip()
        self.assertEqual(parsed.get("名称"), "WLAN")
        self.assertIn("Intel", parsed.get("说明", ""))
        self.assertEqual(parsed.get("状态"), "已连接")
        self.assertEqual(parsed.get("ssid"), "CampusNet-Test")
        self.assertEqual(wifi._parse_percent(parsed.get("信号", "")), 88)
        self.assertEqual(parsed.get("配置文件"), "CampusNet-Test")


class NativeApiTests(unittest.TestCase):
    """原生 WLAN API 在本机应当可用；不可用时相关断言自动跳过。"""

    def test_module_imports_and_reports_availability(self):
        self.assertIsInstance(wlanapi.available(), bool)

    def test_last_error_is_a_string(self):
        self.assertIsInstance(wlanapi.last_error(), str)

    def test_scan_returns_structured_objects(self):
        networks = wlanapi.scan()
        self.assertIsInstance(networks, list)
        for item in networks:
            self.assertTrue(hasattr(item, "ssid"))
            self.assertGreaterEqual(item.signal, 0)
            self.assertLessEqual(item.signal, 100)
            self.assertIsInstance(item.encrypted, bool)

    def test_interfaces_report_state_and_ssid(self):
        items = wlanapi.interfaces()
        self.assertIsInstance(items, list)
        for item in items:
            self.assertIsInstance(item.description, str)
            self.assertIsInstance(item.connected, bool)
            self.assertIsInstance(item.state_text, str)

    def test_public_wifi_helpers_fall_back_gracefully(self):
        """两个公开入口都不能抛异常（要么有数据，要么给出可读原因）。"""
        status = wifi.interface_status()
        self.assertIsInstance(status.available, bool)
        try:
            networks = wifi.scan(timeout=10)
            self.assertIsInstance(networks, list)
        except wifi.WifiError as exc:
            # 允许失败，但原因必须能看懂（不能是空字符串或纯异常号）
            self.assertTrue(str(exc).strip())
            self.assertGreater(len(str(exc)), 10)

    def test_scan_results_are_sorted_by_signal(self):
        networks = wlanapi.scan()
        if len(networks) < 2:
            self.skipTest("附近网络少于 2 个，无法验证排序")
        signals = [item.signal for item in networks]
        self.assertEqual(signals, sorted(signals, reverse=True))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
