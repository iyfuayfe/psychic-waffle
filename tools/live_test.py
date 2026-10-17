"""真实端到端验证：注销当前会话 -> 让程序自动重新登录。

这是唯一能 100% 证明"真的能用"的测试。执行链路与计划任务完全相同：
    check() 探测跳转 -> 抓门户页 -> build_context() -> AES 加密登录 -> 回连验证

失败也不怕：浏览器打开校园网门户手动登录即可恢复。

用法：
    python tools\\live_test.py            # 完整测试（会断网 10~30 秒）
    python tools\\live_test.py --dry-run  # 只看当前状态，不注销
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import crypto, daemon  # noqa: E402
from campusnet.checker import check  # noqa: E402
from campusnet.config import Config  # noqa: E402
from campusnet.drcom import DrcomAdapter, looks_like_drcom  # noqa: E402
from campusnet.http import HttpClient  # noqa: E402
from campusnet.logs import setup_logging  # noqa: E402
from campusnet.portal import PortalPage  # noqa: E402

MANUAL_HINT = """
============================================================
如果自动登录失败：请用浏览器打开 http://111.17.200.130/
手动输入账号密码登录即可恢复上网（你的密码你自己知道）。
============================================================
"""


def banner(text: str) -> None:
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = Config.load()
    log = setup_logging(cfg, console=True)

    banner("0) 前置检查")
    print(f"账号      : {cfg.username}")
    print(f"门户兜底  : {cfg.portal_url}")
    print(f"运营商后缀: {cfg.drcom.account_suffix}")

    client = daemon.build_client(cfg)

    state = check(client, cfg)
    print(f"当前联网  : {'在线' if state.online else '离线'}"
          f"（原因={state.reason}，详情={state.detail}）")
    if not state.online:
        print("\n当前本来就不在线，先让程序登录一次再说。")
        code = daemon.run_once(cfg, client, log)
        print(f"run_once 返回 {code}（0 = 成功）")
        return 0 if code == 0 else 1
    if dry:
        print("\n--dry-run：到此为止，不注销。")
        return 0

    # ---------------------------------------------------------- 取门户页 + 上下文
    banner("1) 抓门户页，准备注销上下文")
    portal = cfg.portal_url
    try:
        resp = client.get(portal, follow_redirects=True)
        page = PortalPage(resp.text, resp.url)
        print(f"门户页: HTTP {resp.status} 标题={page.title!r} "
              f"标记={page.drcom_page_state() or '(非Dr.COM/未知)'}")
    except Exception as exc:  # noqa: BLE001
        print(f"门户页抓取失败：{exc}")
        page = None

    adapter = DrcomAdapter(client, cfg, log)
    ctx = adapter.build_context(portal, page)
    ctx.account = cfg.username
    if not ctx.suffix:
        ctx.suffix = cfg.drcom.account_suffix or ""
    print(f"注销上下文: host={ctx.host}:{ctx.port} IP={ctx.wlan_user_ip or '(未取到)'} "
          f"AC={ctx.wlan_ac_ip or '-'}/{ctx.wlan_ac_name or '-'} login_method={ctx.login_method}")

    # ---------------------------------------------------------- 注销
    banner("2) 注销当前会话（这一步会断网）")
    ok, msg = adapter.logout(ctx)
    print(f"注销结果: {'成功' if ok else '未确认'} —— {msg}")

    time.sleep(3)
    client2 = daemon.build_client(cfg)
    after = check(client2, cfg)
    print(f"注销后联网: {'仍在线（注销可能没生效）' if after.online else '已离线 ✅'} "
          f"（原因={after.reason}，详情={after.detail}）")
    if after.portal_url:
        print(f"探测到认证跳转: {after.portal_url[:120]}")
        if page is None or not looks_like_drcom(after.portal_url, page):
            pass
    if after.online:
        print("\n注销没生效，取消后续测试（不动你的网络了）。")
        return 1

    # ---------------------------------------------------------- 自动登录（生产路径）
    banner("3) 跑生产代码路径：campus_login.py --once（与计划任务完全相同）")
    t0 = time.time()
    code = daemon.run_once(cfg, client2, log)
    elapsed = time.time() - t0
    print(f"\nrun_once 返回 {code}（0 = 成功），耗时 {elapsed:.1f}s")

    # ---------------------------------------------------------- 结果
    banner("4) 最终确认")
    client3 = daemon.build_client(cfg)
    final = check(client3, cfg)
    print(f"最终联网: {'✅ 已恢复，自动登录成功' if final.online else '❌ 仍不通'}"
          f"（原因={final.reason}，详情={final.detail}）")

    print("\n--- 日志末尾 ---")
    try:
        lines = (ROOT / "logs" / "campus_login.log").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        for line in lines[-18:]:
            print("  " + line)
    except Exception as exc:  # noqa: BLE001
        print(f"  读日志失败：{exc}")

    if final.online:
        print("\n🎉 端到端验证通过：注销 → 自动探测 → 加密登录 → 回连验证，全链路可用。")
        return 0
    print(MANUAL_HINT)
    return 1


if __name__ == "__main__":
    sys.exit(main())
