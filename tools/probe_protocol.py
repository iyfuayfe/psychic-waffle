"""Dr.COM / 哆点 ePortal 协议探测（排障用，只用假账号，不影响真实在线会话）。

背景：门户 JS（a41.js 的 util._jsonp）在 page.login_method != 0 时，会把整个登录
参数 JSON 用 AES-ECB 加密后塞进单个 `params` 字段：

    GET <portal_api>login?callback=drN&params=<base64(AES(JSON))>&jsVersion=..&lang=zh

所以"裸发明文参数"会被服务端拒掉。本工具把浏览器真实会走的每一步都复现一遍，
并把服务端原文打印出来，用来确认：

  1. page/loadConfig 拿到的 login_method / account_prefix / account_suffix 到底是多少
  2. chkstatus 当前是否在线
  3. 哪种报文格式被服务端"看懂"了（msg 不是"认证出现异常"）

判读方法：
  - "认证出现异常！" / "参数错误"        -> 格式不对
  - "账号或密码错误" / "用户不存在"       -> 格式对了，只是假账号
  - "当前页面已超时"                     -> 格式被解析了，但没有待认证会话

用法：
    python tools\\probe_protocol.py
    python tools\\probe_protocol.py --host 192.0.2.10 --port 801
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import aes  # noqa: E402
from campusnet.http import HttpClient  # noqa: E402

JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.S)
JS_VERSION = "4.2.1"
FAKE_ACCOUNT = "00000000000"
FAKE_PASSWORD = "zzzz1111"
FAKE_SUFFIX = "@cmcc"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def unwrap(text: str):
    """剥掉 JSONP 信封 drNNNN({...}) ，返回 dict。"""
    stripped = (text or "").strip()
    m = JSONP_RE.match(stripped)
    body = m.group(1) if m else stripped
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return {"_raw": stripped[:400]}
    return obj if isinstance(obj, dict) else {"_raw": str(obj)[:400]}


def local_ip(host: str) -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect((host, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return ""


def show(title: str, url: str, resp) -> dict:
    data = unwrap(resp.text)
    msg = str(data.get("msg", "") or "")
    print(f"\n--- {title}")
    print(f"    GET {url}")
    print(f"    HTTP {resp.status}  result={data.get('result')!r}  ret_code={data.get('ret_code')!r}")
    if msg:
        print(f"    msg={msg!r}")
    if "_raw" in data:
        print(f"    raw={data['_raw'][:200]!r}")
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.0.2.10")
    ap.add_argument("--port", type=int, default=801)
    ap.add_argument("--account", default=FAKE_ACCOUNT)
    ap.add_argument("--password", default=FAKE_PASSWORD)
    ap.add_argument("--suffix", default=FAKE_SUFFIX)
    args = ap.parse_args(argv)

    client = HttpClient(timeout=12, insecure_tls=True, use_system_proxy=False)
    api = f"http://{args.host}:{args.port}/eportal/portal/"
    ip = local_ip(args.host)

    print("=" * 76)
    print(f"门户 API : {api}")
    print(f"本机 IP  : {ip or '(未知)'}")
    print(f"测试账号 : {args.account}{args.suffix}（假账号，不会影响真实会话）")
    print("=" * 76)

    # ---------------------------------------------------------------- 1. loadConfig
    print("\n[1] 取门户配置 page/loadConfig（浏览器第一步，未加密）")
    cfg_data = {
        "program_index": "",
        "wlan_vlan_id": "1",
        "wlan_user_ip": b64(ip),
        "wlan_user_ipv6": b64("::"),
        "wlan_user_ssid": "",
        "wlan_user_areaid": "",
        "wlan_ac_ip": b64(""),
        "wlan_ap_mac": "000000000000",
        "gw_id": "",
    }
    fields = {"callback": "dr1003", **cfg_data, "jsVersion": JS_VERSION, "lang": "zh"}
    url = api + "page/loadConfig?" + urllib.parse.urlencode(fields)
    try:
        resp = client.get(url, follow_redirects=False)
        cfg_json = show("loadConfig", url, resp)
    except Exception as exc:  # noqa: BLE001
        print(f"    请求失败：{exc}")
        cfg_json = {}

    inner = cfg_json.get("data") if isinstance(cfg_json.get("data"), dict) else {}
    if inner:
        keys = (
            "login_method", "account_prefix", "account_suffix", "en_md5", "enable_r3",
            "register_mode", "enable_new_drcom_srv", "check_online_method", "ac_logout",
            "program_index", "page_index", "online_monitor", "en_perceive",
        )
        print("\n    关键配置：")
        for k in keys:
            if k in inner:
                print(f"      {k:24s}= {inner[k]!r}")
    else:
        print("\n    (没拿到 data，服务端可能只允许门户 IP 访问该接口)")

    # ---------------------------------------------------------------- 2. chkstatus
    print("\n[2] 查询在线状态 chkstatus")
    for base in (f"http://{args.host}/drcom/", api):
        u = base + "chkstatus?" + urllib.parse.urlencode(
            {"callback": "dr1003", "jsVersion": JS_VERSION, "lang": "zh"}
        )
        try:
            show(f"chkstatus @ {base}", u, client.get(u, follow_redirects=False))
        except Exception as exc:  # noqa: BLE001
            print(f"\n--- chkstatus @ {base}\n    请求失败：{exc}")

    # ---------------------------------------------------------------- 3. 登录格式对比
    login_method = inner.get("login_method")
    prefix_on = inner.get("account_prefix")
    suffix = inner.get("account_suffix") or args.suffix
    account = f"{args.account}{suffix}"

    print("\n[3] 登录报文格式对比（全部用假账号）")
    print(f"    login_method={login_method!r}  account_prefix={prefix_on!r}  suffix={suffix!r}")

    def try_login(title: str, url: str) -> None:
        try:
            show(title, url, client.get(url, follow_redirects=False))
        except Exception as exc:  # noqa: BLE001
            print(f"\n--- {title}\n    请求失败：{exc}")

    # A. 浏览器真实格式：callback + params=<AES(JSON)> + jsVersion + lang
    for lm in ([login_method] if isinstance(login_method, int) else []) + [1, 9]:
        if lm is None:
            continue
        payload = {
            "login_method": lm,
            "user_account": (",0," if prefix_on != 0 else "") + account,
            "user_password": args.password,
            "wlan_user_ip": ip,
            "wlan_user_ipv6": "",
            "wlan_user_mac": "000000000000",
            "wlan_ac_ip": "",
            "wlan_ac_name": "",
            "jsVersion": JS_VERSION,
        }
        token = aes.encrypt_b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        fields = {
            "callback": "dr1003",
            "params": token,
            "jsVersion": JS_VERSION,
            "lang": "zh",
        }
        try_login(
            f"A AES加密params (login_method={lm})",
            api + "login?" + urllib.parse.urlencode(fields, safe=""),
        )

    # B. 明文 user_account（对照）
    plain = {
        "callback": "dr1003",
        "login_method": "1",
        "user_account": ",0," + account,
        "user_password": args.password,
        "wlan_user_ip": ip,
        "wlan_user_ipv6": "",
        "wlan_user_mac": "000000000000",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": JS_VERSION,
        "lang": "zh",
    }
    try_login("B 明文 user_account（对照）", api + "login?" + urllib.parse.urlencode(plain))

    # C. 明文 DDDDD/upass（login_method==0 的老式写法）
    legacy = {
        "callback": "dr1003",
        "DDDDD": account,
        "upass": args.password,
        "0MKKey": "123456",
        "R1": "",
        "R2": "",
        "R3": "",
        "R6": "0",
        "para": "",
        "v6ip": "",
        "jsVersion": JS_VERSION,
        "lang": "zh",
    }
    try_login("C 明文 DDDDD/upass", api + "login?" + urllib.parse.urlencode(legacy))

    # D. 老式 /drcom/login（page.path + 'login'）
    try_login(
        "D 老式 /drcom/login",
        f"http://{args.host}/drcom/login?" + urllib.parse.urlencode(legacy),
    )

    print("\n" + "=" * 76)
    print("判读：msg 是「认证出现异常」=格式不对；是「账号或密码」=格式对了；")
    print("      是「当前页面已超时」=格式被解析但没有待认证会话（在线时常见）。")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
