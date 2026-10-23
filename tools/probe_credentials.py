"""用门户"自服务 self 接口"校验账号密码（不影响在线会话，不发登录）。

依据 a40.js:161 `self` 接口：
    url  = portal_api + 'self'
    data = { self_type, user_account, user_password, wlan_user_mac(大写),
             wlan_user_ip(十进制), jsVersion }
    _jsonp 会把 data 整体 AES 加密进 params（login_method=1）

返回 result==1 => 凭据正确；否则 msg 会给出"账号或密码错误"之类的原因。
本工具只看响应、不跟随 self_auth_url，因此不会改变当前会话。

用法：
    python tools\\probe_credentials.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import aes, crypto  # noqa: E402
from campusnet.config import Config  # noqa: E402
from campusnet.drcom import DrcomAdapter, ip_to_int, local_ip_for, parse_jsonp  # noqa: E402
from campusnet.http import HttpClient  # noqa: E402
from campusnet.logs import setup_logging  # noqa: E402


def qs(fields: dict) -> str:
    import urllib.parse
    return "&".join(
        f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in fields.items()
    )


def main() -> int:
    cfg = Config.load()
    log = setup_logging(cfg, console=True)
    host = cfg.drcom.host or "192.0.2.10"
    port = cfg.drcom.port or 801
    api = f"http://{host}:{port}/eportal/portal/"
    ip = cfg.drcom.wlan_user_ip or local_ip_for(host) or ""

    try:
        password = crypto.unprotect(cfg.password_enc)
    except Exception as exc:  # noqa: BLE001
        print(f"密码解密失败：{exc}")
        return 2

    client = HttpClient(timeout=10, insecure_tls=True, use_system_proxy=False)

    print("=" * 70)
    print(f"账号：{cfg.username}  密码长度：{len(password)}  本机IP：{ip}（十进制 {ip_to_int(ip)}）")
    print("=" * 70)

    mac = "0A1B2C3D4E5F"
    accounts = {
        "带@cmcc后缀": cfg.username + "@cmcc",
        "不带后缀": cfg.username,
        "带,0,前缀": ",0," + cfg.username + "@cmcc",
    }
    for st in ("1", "2"):
        for label, account in accounts.items():
            data = {
                "self_type": st,
                "user_account": account,
                "user_password": password,
                "wlan_user_mac": mac,
                "wlan_user_ip": ip_to_int(ip),
                "jsVersion": "4.2.1",
            }
            token = aes.encrypt_b64(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
            url = api + "self?" + qs({
                "callback": "dr1003", "params": token, "jsVersion": "4.2.1", "lang": "zh",
            })
            try:
                resp = client.get(url, follow_redirects=False)
            except Exception as exc:  # noqa: BLE001
                print(f"[self_type={st} {label}] 请求异常：{exc}")
                continue
            parsed = parse_jsonp(resp.text)
            result = parsed.get("result")
            msg = str(parsed.get("msg", "") or "")
            auth_url = str(parsed.get("self_auth_url", "") or "")
            verdict = "[OK] 凭据正确" if result in (1, "1", "ok") else ("❌ " + (msg or f"result={result}"))
            print(f"[self_type={st}] {label:12s} -> result={result!r} msg={msg!r} url={'有' if auth_url else '无'}  {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
