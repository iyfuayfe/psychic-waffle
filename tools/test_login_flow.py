"""Dr.COM 认证链路"干跑"验证 —— 用**假账号**把每条策略都真发一遍，看服务端原文。

这是在不影响真实在线会话的前提下，验证"报文格式对不对"的手段：
假账号注定认证不成功，所以只要服务端回的不是「认证出现异常！」，
就说明参数的**格式/加密**已经被正确解析了。

判读：
    认证出现异常！ / 参数错误        -> 格式不对（v1 的 DDDDD 明文就是这样挂的）
    账号或密码错误 / 用户不存在       -> 格式完全正确（连凭据校验都走到了）
    当前页面已超时 / 请重新刷新页面   -> 格式正确，只是当前没有待认证会话（已在线时常见）

用法：
    python tools\\test_login_flow.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import drcom as drcom_mod  # noqa: E402
from campusnet.config import Config  # noqa: E402
from campusnet.drcom import DrcomAdapter, parse_jsonp  # noqa: E402
from campusnet.http import HttpClient  # noqa: E402
from campusnet.logs import setup_logging  # noqa: E402

FAKE_ACCOUNT = "00000000000"
FAKE_PASSWORD = "zzzz1111"


def classify(msg: str, result, ret_code) -> str:
    if result in (1, "1", "ok") or str(ret_code) == "2":
        return "★ 成功语义"
    if any(k in msg for k in drcom_mod.CREDENTIAL_HINTS):
        return "[OK] 格式正确（服务端已走到凭据校验）"
    if any(k in msg for k in drcom_mod.PARAM_ERROR_HINTS):
        return "[注意] 参数/会话问题（要区分是格式错还是无待认证会话）"
    return "? 未分类"


def main() -> int:
    cfg = Config.load()
    log = setup_logging(cfg, console=True)

    host = cfg.drcom.host or "192.0.2.10"
    portal_url = cfg.portal_url or f"http://{host}/a79.htm"

    client = HttpClient(
        timeout=cfg.request_timeout,
        insecure_tls=bool(cfg.insecure_tls),
        use_system_proxy=bool(cfg.use_system_proxy),
        user_agent=cfg.user_agent,
    )
    adapter = DrcomAdapter(client, cfg, log)

    print("=" * 78)
    print("Dr.COM 认证链路干跑（假账号，不会影响真实会话）")
    print("=" * 78)

    ctx = adapter.build_context(portal_url, None)
    ctx.account = FAKE_ACCOUNT
    if not ctx.suffix:
        ctx.suffix = cfg.drcom.account_suffix or "@cmcc"

    print(f"\n上下文：host={ctx.host}:{ctx.port}")
    print(f"        wlan_user_ip = {ctx.wlan_user_ip or '(未取到)'}")
    print(f"        wlan_ac_ip   = {ctx.wlan_ac_ip or '-'}")
    print(f"        wlan_ac_name = {ctx.wlan_ac_name or '-'}")
    print(f"        wlan_user_mac= {ctx.wlan_user_mac}")
    print(f"        login_method = {ctx.login_method}   account_prefix = {ctx.account_prefix}")
    print(f"        js_version   = {ctx.js_version}   服务端后缀 = {ctx.server_suffix or '(空)'}")
    print(f"        提交账号     = {ctx.prefixed_account()}  (前缀 {'已加' if ctx.account_prefix else '未加'})")

    plans = adapter.strategies(ctx, FAKE_PASSWORD)
    print(f"\n共 {len(plans)} 套策略：\n")

    worst = 0
    for plan in plans:
        label = plan["name"]
        url = plan["url"]
        shown = url if len(url) < 150 else url[:150] + " …"
        print("-" * 78)
        print(f"[{label}]")
        print(f"  {shown}")
        if plan.get("pre"):
            print("  前置步骤：duodian/queryPageSet ...")
            try:
                plan["pre"]()
            except Exception as exc:  # noqa: BLE001
                print(f"    前置步骤异常：{exc}")
        try:
            resp = client.get(url, follow_redirects=False, headers=plan.get("headers"))
        except Exception as exc:  # noqa: BLE001
            print(f"  请求异常：{exc}")
            worst += 1
            continue
        data = parse_jsonp(resp.text)
        msg = str(data.get("msg", "") or "")
        print(f"  HTTP {resp.status}  result={data.get('result')!r}  ret_code={data.get('ret_code')!r}")
        print(f"  msg = {msg!r}")
        if "_raw" in data:
            print(f"  raw = {data['_raw'][:200]!r}")
        print(f"  => {classify(msg, data.get('result'), data.get('ret_code'))}")

    print("\n" + "=" * 78)
    print("结论看 drcom:eportal-aes 那条：不是「认证出现异常！」就说明加密报文被正确解析。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
