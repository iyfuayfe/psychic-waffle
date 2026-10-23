"""决定性对照实验：真密码 / 错密码 / 假账号，抓完整响应与 Cookie。

目的：区分「账号密码不对」和「页面会话/协议问题」。
   - 若三者响应**不同**（真密码能通或报不同错误）=> 服务端在查凭据，问题在账号/密码
   - 若三者响应**完全相同**（都是"页面已超时"）=> 服务端没走到凭据校验，另有原因

会先注销一次制造离线环境；最后用真密码再登录一次并回连验证。
用法：
    python tools\\probe_diff.py
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

HOST = "192.0.2.10"


def fire(adapter, ctx, password, label, client):
    plans = adapter.strategies(ctx, password)
    plan = plans[0]  # plain-yau
    if plan.get("pre"):
        try:
            plan["pre"]()
        except Exception:  # noqa: BLE001
            pass
    try:
        resp = client.get(plan["url"], follow_redirects=False, headers=plan.get("headers"))
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] 请求异常：{exc}")
        return None
    d = parse_jsonp(resp.text)
    cookies = ", ".join(f"{c.name}={c.value[:12]}" for c in client.jar) or "(无)"
    sc = [h for h in (resp.headers or {}) if h.lower() == "set-cookie"]
    print(f"[{label}] HTTP {resp.status} result={d.get('result')!r} ret_code={d.get('ret_code')!r} "
          f"msg={str(d.get('msg',''))!r}")
    print(f"         cookie罐: {cookies}  Set-Cookie头: {sc or '(无)'}")
    if "_raw" in d:
        print(f"         raw={d['_raw'][:120]!r}")
    return d


def main() -> int:
    cfg = Config.load()
    log = setup_logging(cfg, console=True)
    try:
        real = crypto.unprotect(cfg.password_enc)
    except Exception as exc:  # noqa: BLE001
        print(f"密码解密失败：{exc}")
        return 2

    client = daemon.build_client(cfg)

    print("=" * 78)
    print(f"账号 {cfg.username}  真密码长度 {len(real)}")
    print("=" * 78)

    # ---- 若在线，先注销制造离线 ----
    from campusnet.checker import check
    state = check(client, cfg)
    if state.online:
        print("\n当前在线，先注销……")
        try:
            page = PortalPage(client.get(cfg.portal_url, follow_redirects=True).text, cfg.portal_url)
        except Exception:  # noqa: BLE001
            page = None
        adapter = DrcomAdapter(client, cfg, log)
        ctx = adapter.build_context(cfg.portal_url, page)
        ctx.account = cfg.username
        if not ctx.suffix:
            ctx.suffix = cfg.drcom.account_suffix or "@cmcc"
        ok, msg = adapter.logout(ctx)
        print(f"注销：{'成功' if ok else '未确认'} —— {msg}")
        time.sleep(3)
    else:
        print("\n已经离线，无需注销")

    # ---- 重建上下文（离线态 chkstatus）----
    client2 = daemon.build_client(cfg)
    adapter2 = DrcomAdapter(client2, cfg, log)
    try:
        page2 = PortalPage(client2.get(cfg.portal_url, follow_redirects=True).text, cfg.portal_url)
    except Exception:  # noqa: BLE001
        page2 = None
    ctx2 = adapter2.build_context(cfg.portal_url, page2)
    ctx2.account = cfg.username
    if not ctx2.suffix:
        ctx2.suffix = cfg.drcom.account_suffix or "@cmcc"
    print(f"\n离线上下文：IP={ctx2.wlan_user_ip} MAC={ctx2.wlan_user_mac} "
          f"提交账号={ctx2.prefixed_account()}")

    print("\n--- 对照实验 ---")
    fire(adapter2, ctx2, real, "A 真密码", client2)
    time.sleep(1)
    fire(adapter2, ctx2, "WrongPass123", "B 错密码", client2)
    time.sleep(1)
    save = ctx2.account
    ctx2.account = "00000000000"
    fire(adapter2, ctx2, real, "C 假账号", client2)
    ctx2.account = save
    time.sleep(1)

    # ---- 最终：真密码再来一次 + 回连验证 ----
    print("\n--- 最终真密码登录 + 回连 ---")
    from campusnet.checker import check as ck
    fire(adapter2, ctx2, real, "A2 真密码(重试)", client2)
    time.sleep(3)
    fin = ck(client2, cfg)
    print(f"最终联网：{'[OK] 成功' if fin.online else '❌ 仍不通'}（{fin.reason}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
