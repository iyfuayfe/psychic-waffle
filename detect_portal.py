"""认证页探测工具（排障用，控制台运行）。

作用：把校园网 Portal 的真实结构扒出来，并给出可以直接粘贴进 config.json
的字段映射。不知道认证页地址、或者自动登录不生效时，先跑这个。

用法：
    python detect_portal.py                          # 自动探测
    python detect_portal.py --url http://192.0.2.10/
    python detect_portal.py --json                   # 输出机器可读结果
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from campusnet.checker import check  # noqa: E402
from campusnet.config import DEFAULT_CONFIG_PATH, LOG_DIR, Config  # noqa: E402
from campusnet.http import HttpClient, guess_encoding  # noqa: E402
from campusnet.portal import PortalPage, summarize  # noqa: E402
from campusnet.security import redact_portal_html  # noqa: E402


def _p(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8", "replace"))


def _hr(title: str = "") -> None:
    _p("-" * 62)
    if title:
        _p(title)
        _p("-" * 62)


def build_client(cfg: Config) -> HttpClient:
    return HttpClient(
        timeout=cfg.request_timeout,
        insecure_tls=bool(cfg.insecure_tls),
        use_system_proxy=bool(cfg.use_system_proxy),
        user_agent=cfg.user_agent,
    )


def probe_connectivity(cfg: Config, client: HttpClient) -> dict:
    _hr("1. 联网探测")
    result = check(client, cfg)
    for item in result.tried:
        if "error" in item:
            _p(f"  [失败] {item['url']}\n         {item['error']}")
        else:
            _p(f"  [响应] {item['url']} -> HTTP {item['status']}")
    _p()
    _p(f"  结论：{'已在线' if result.online else '未在线'}")
    _p(f"  原因：{result.reason}    详情：{result.detail}")
    if result.portal_url:
        _p(f"  探测到的认证页：{result.portal_url}")
    return result


def analyze_portal(client: HttpClient, url: str) -> dict:
    _hr(f"2. 认证页分析：{url}")
    try:
        resp = client.get(url, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        _p(f"  [失败] 打不开：{exc}")
        return {"ok": False, "error": str(exc)}

    enc = guess_encoding(resp.content_type, resp.body)
    _p(f"  HTTP {resp.status}   最终地址：{resp.url}")
    _p(f"  Content-Type: {resp.content_type or '(无)'}")
    _p(f"  嗅探编码: {enc}   原始字节数: {len(resp.body)}")

    page = PortalPage(resp.text, resp.url)
    info = summarize(page)
    _p(f"  页面标题: {info['title'] or '(无)'}")
    _p(f"  表单数量: {info['form_count']}   验证码: {'有' if info['has_captcha'] else '无'}")

    _p()
    for form in page.forms:
        _p(f"  [表单 #{form.index}] action={form.action or '(空，提交到当前地址)'}  method={form.method}")
        _p(f"      id={form.form_id or '-'}  name={form.form_name or '-'}")
        for f in form.fields:
            _p(f"        - {f.type:<10} {f.name:<28} value={f.value[:40]!r}")
    if page.loose_inputs:
        _p("  [游离 input（不在 form 内）]")
        for f in page.loose_inputs:
            _p(f"        - {f.type:<10} {f.name:<28} value={f.value[:40]!r}")

    if info["script_candidates"]:
        _p()
        _p("  [JS 里疑似接口地址]")
        for c in info["script_candidates"]:
            _p(f"        {c}")

    _p()
    _p("  [自动识别结果]")
    _p(f"        账号字段: {info['username_field'] or '(没认出来)'}")
    _p(f"        密码字段: {info['password_field'] or '(没认出来)'}")
    _p(f"        提交地址: {info['submit_url'] or '-'}")
    _p(f"        提交方式: {info['submit_method'] or '-'}")

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        dump = LOG_DIR / "portal_dump.html"
        # 诊断转储也要脱敏：账号、密码、令牌、内网地址、MAC 全部打码
        dump.write_text(redact_portal_html(page.html, resp.url), encoding="utf-8", errors="replace")
        _p(f"\n  已脱敏的页面副本已保存：{dump}")
    except Exception as exc:  # noqa: BLE001
        _p(f"\n  （页面保存失败：{exc}）")

    return {
        "ok": True,
        "encoding": enc,
        "page": info,
        "status": resp.status,
        "final_url": resp.url,
        "_page_obj": page,
    }


def analyze_drcom(cfg: Config, client: HttpClient, portal_url: str, page_info: dict, page) -> dict:
    """识别 Dr.COM / 哆点 ePortal 并还原出认证上下文。"""
    from campusnet.drcom import DrcomAdapter, ip_to_int, local_ip_for, looks_like_drcom

    _hr("3. Dr.COM / 哆点 ePortal 识别")
    is_drcom = looks_like_drcom(portal_url, page)
    _p(f"  是否 Dr.COM: {'是' if is_drcom else '否（将走通用表单引擎）'}")
    if not is_drcom:
        return {"is_drcom": False}

    adapter = DrcomAdapter(client, cfg, cfg_logger())
    ctx = adapter.build_context(portal_url, page)
    ctx.account = cfg.username or "你的账号"
    ctx.suffix = ctx.suffix or cfg.drcom.account_suffix

    _p(f"  认证服务: http://{ctx.host}:{ctx.port}/eportal/portal/login")
    _p(f"  客户端 IP (wlan_user_ip): {ctx.wlan_user_ip or '(未取到)'}"
       + (f"   十进制={ip_to_int(ctx.wlan_user_ip)}" if ctx.wlan_user_ip else ""))
    _p(f"  AC 名称: {ctx.wlan_ac_name or '(未取到，可能不影响)'}")
    _p(f"  AC IP  : {ctx.wlan_ac_ip or '(未取到，可能不影响)'}")
    _p(f"  账号后缀: {ctx.suffix or '(无)'}   ->  最终账号: {ctx.final_account()}")
    _p(f"  本机到门户的出口 IP: {local_ip_for(ctx.host) or '(取不到)'}")

    plans = adapter.strategies(ctx, "密码")
    _p()
    _p(f"  将按顺序尝试 {len(plans)} 套策略（每套都用真实联网结果验证）：")
    for i, plan in enumerate(plans, 1):
        _p(f"      {i}. {plan['name']}   [{plan['method']} {plan['url']}]")

    return {
        "is_drcom": True,
        "host": ctx.host,
        "port": ctx.port,
        "wlan_user_ip": ctx.wlan_user_ip,
        "wlan_ac_name": ctx.wlan_ac_name,
        "wlan_ac_ip": ctx.wlan_ac_ip,
        "account_suffix": ctx.suffix,
        "strategies": [p["name"] for p in plans],
    }


def cfg_logger():
    import logging

    return logging.getLogger("campusnet.probe")


def suggest_config(cfg: Config, portal_url: str, info: dict, drcom: dict | None = None) -> dict:
    suggestion = {
        "portal_url": portal_url,
        "username": cfg.username or "你的账号",
        "interval_online": cfg.interval_online,
        "interval_offline": cfg.interval_offline,
        "insecure_tls": True,
        "use_system_proxy": False,
    }
    form = info.get("chosen_form") or {}
    if info.get("username_field") or info.get("password_field"):
        suggestion["custom"] = {
            "enabled": False,
            "url": info.get("submit_url", ""),
            "method": info.get("submit_method", "POST"),
            "username_field": info.get("username_field", ""),
            "password_field": info.get("password_field", ""),
            "extra_fields": {
                f["name"]: f["value"]
                for f in (form.get("fields") or [])
                if f.get("type") == "hidden"
            },
        }
    if drcom and drcom.get("is_drcom"):
        suggestion["drcom"] = {
            "enabled": True,
            "host": drcom.get("host", ""),
            "port": drcom.get("port", 801),
            "account_suffix": drcom.get("account_suffix", ""),
            "wlan_user_ip": "",
            "wlan_ac_ip": drcom.get("wlan_ac_ip", ""),
            "wlan_ac_name": drcom.get("wlan_ac_name", ""),
            "wlan_user_mac": "000000000000",
        }
    return suggestion


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校园网认证页探测工具")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--url", default="", help="直接指定认证页地址")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于粘贴反馈）")
    args = parser.parse_args(argv)

    try:
        cfg = Config.load(args.config)
    except Exception as exc:  # noqa: BLE001
        _p(f"[致命] 配置读取失败：{exc}")
        return 4

    client = build_client(cfg)
    report: dict = {"config_path": str(args.config)}

    conn = probe_connectivity(cfg, client)
    report["connectivity"] = {
        "online": conn.online,
        "reason": conn.reason,
        "detail": conn.detail,
        "portal_url": conn.portal_url,
        "tried": conn.tried,
    }

    portal_url = args.url or cfg.portal_url or conn.portal_url
    if not portal_url and args.url == "":
        _p()
        _p("  没能自动发现认证页地址。请用 --url 手工指定，例如：")
        _p("      python detect_portal.py --url http://192.0.2.10/")
    if portal_url:
        report["portal"] = analyze_portal(client, portal_url)
        page_obj = (report["portal"] or {}).pop("_page_obj", None)
        info = (report["portal"] or {}).get("page") or {}

        drcom_report = {"is_drcom": False}
        if page_obj is not None:
            try:
                drcom_report = analyze_drcom(cfg, client, portal_url, info, page_obj)
            except Exception as exc:  # noqa: BLE001
                _p(f"  （Dr.COM 识别出错：{exc}）")
        report["drcom"] = drcom_report

        if info:
            suggestion = suggest_config(cfg, portal_url, info, drcom_report)
            report["suggested_config"] = suggestion
            _hr("4. 建议写入 config.json 的片段")
            _p(json.dumps(suggestion, ensure_ascii=False, indent=2))
            _p()
            _p("提示：直接运行  python tools/setup_wizard.py  会自动探测并写好配置。")

    if args.json:
        _p()
        _hr("JSON 结果")
        _p(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
