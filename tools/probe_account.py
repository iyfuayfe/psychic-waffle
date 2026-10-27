"""离线状态下，把所有账号格式都试一遍，定位「Authentication fail」到底是账号格式还是密码。

会先注销制造离线；用真密码配合每种账号格式发一次登录，打印 result/ret_code/msg。
用法：
    python tools\\probe_account.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import crypto, daemon  # noqa: E402
from campusnet.config import Config  # noqa: E402
from campusnet.drcom import DrcomAdapter, parse_jsonp  # noqa: E402
from campusnet.logs import setup_logging  # noqa: E402
from campusnet.portal import PortalPage  # noqa: E402


def main() -> int:
    cfg = Config.load()
    log = setup_logging(cfg, console=True)
    try:
        pwd = crypto.unprotect(cfg.password_enc)
    except Exception as exc:  # noqa: BLE001
        print(f"密码解密失败：{exc}")
        return 2

    client = daemon.build_client(cfg)
    from campusnet.checker import check
    if check(client, cfg).online:
        print("在线，先注销……")
        try:
            page = PortalPage(client.get(cfg.portal_url, follow_redirects=True).text, cfg.portal_url)
        except Exception:  # noqa: BLE001
            page = None
        a = DrcomAdapter(client, cfg, log)
        c = a.build_context(cfg.portal_url, page)
        c.account = cfg.username
        if not c.suffix:
            c.suffix = cfg.drcom.account_suffix or "@cmcc"
        a.logout(c)
        time.sleep(3)

    client2 = daemon.build_client(cfg)
    adapter = DrcomAdapter(client2, cfg, log)
    try:
        page2 = PortalPage(client2.get(cfg.portal_url, follow_redirects=True).text, cfg.portal_url)
    except Exception:  # noqa: BLE001
        page2 = None
    ctx = adapter.build_context(cfg.portal_url, page2)
    ctx.suffix = ctx.suffix or cfg.drcom.account_suffix or "@cmcc"
    print(f"\nIP={ctx.wlan_user_ip} rcn={ctx.rcn} login_method={ctx.login_method} prefix={'开' if ctx.account_prefix else '关'}")

    formats = [
        ",0," + cfg.username + "@cmcc",
        ",0," + cfg.username,
        cfg.username + "@cmcc",
        cfg.username,
        ",1," + cfg.username + "@cmcc",
        "@cmcc" + cfg.username,
    ]
    print("\n--- 账号格式对照（真密码）---")
    for acct in formats:
        ctx.account = acct if "@" in acct else cfg.username
        # 直接构造账号（绕过 final_account 的自动补后缀逻辑）
        ctx.account = cfg.username
        plan = adapter.strategies(ctx, pwd)[0]
        if plan.get("pre"):
            try:
                plan["pre"]()
            except Exception:  # noqa: BLE001
                pass
        # 手工替换 user_account 参数
        import urllib.parse
        from campusnet import drcom as dm
        data = adapter._plain_login_data(ctx, pwd)
        data["user_account"] = acct
        url = ctx.api_login + "?" + dm._qs(data)
        try:
            r = client2.get(url, follow_redirects=False, headers=plan.get("headers"))
        except Exception as exc:  # noqa: BLE001
            print(f"[{acct}] 请求异常：{exc}")
            continue
        d = parse_jsonp(r.text)
        print(f"[{acct:26s}] result={d.get('result')!r} ret_code={d.get('ret_code')!r} msg={str(d.get('msg',''))!r}")

    from campusnet.checker import check as ck
    fin = ck(client2, cfg)
    print(f"\n最终联网：{'[OK] 成功' if fin.online else '❌ 仍不通'}（{fin.reason}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
