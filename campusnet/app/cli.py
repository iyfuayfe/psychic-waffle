"""``CampusNetCLI.exe``：命令行诊断入口（给开发者与高级用户）。

子命令：

    status               当前网络状态（在线 / 认证跳转 / DNS 失败 …）
    check                只做一次联网探测（等同 status）
    once                 做一次真实认证（排障用，会提交账号密码）
    autostart status     查看开机自启动与后台守护状态
    autostart on|off     开启 / 关闭开机自启动
    start / stop         启动 / 停止后台守护
    uninstall [--remove-data]
                         卸载：停后台、关自启动、清旧版痕迹
                         （默认**保留**配置与日志，加 --remove-data 才删）
    cleanup-legacy       清理旧版（V2）计划任务与启动脚本
    doctor               导出一份脱敏诊断包，返回文件路径
    conflicts            检查有没有第三方叠加软件注入本程序（界面闪退排查）
    paths                显示安装目录 / 数据目录 / 日志位置
    version              显示版本

所有子命令都不需要管理员权限，也不会修改杀毒软件设置。
"""

from __future__ import annotations

import json
import sys
import time

from .. import autostart, diagnostics, paths
from ..version import banner
from .bootstrap import bootstrap

USAGE = """\
CampusNet 命令行工具

子命令：
  status                 当前网络状态
  once                   做一次真实认证（排障用）
  autostart [status|on|off]
  start | stop           启动 / 停止后台守护
  cleanup-legacy         清理旧版自启动项
  doctor                 导出脱敏诊断包
  paths                  显示路径
  version                显示版本
"""


def _print(text: str = "") -> None:
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def _cmd_status() -> int:
    from ..checker import check
    from ..config import Config
    from ..daemon import build_client

    cfg = Config.load(paths.config_path())
    client = build_client(cfg)
    state = check(client, cfg)
    _print(json.dumps({
        "online": state.online,
        "kind": state.kind,
        "detail": state.detail,
        "portal_url": state.portal_url,
        "hint": state.explain(),
    }, ensure_ascii=False, indent=2))
    return 0 if state.online else 1


def _cmd_daemon_status() -> int:
    """后台守护的真实健康状态（进程 + 心跳新鲜度 + 当前状态）。"""
    from ..daemon import daemon_health

    health = daemon_health()
    _print("== 后台守护 ==")
    _print(f"  结论      : {health.label}")
    _print(f"  进程存在  : {'是' if health.running else '否'}")
    _print(f"  心跳新鲜  : {'是' if (health.age >= 0 and health.age <= 180) else '否'}"
           + (f"（{health.age:.0f} 秒前）" if health.age >= 0 else "（没有心跳）"))
    if health.state:
        _print(f"  当前状态  : {health.state}（{health.state_text}）")
    if health.detail:
        _print(f"  状态说明  : {health.detail}")
    if health.pid:
        _print(f"  进程号    : {health.pid}")
    _print(f"  判断依据  : {health.reason}")
    _print("")
    try:
        from .. import autostart

        info = autostart.status()
        _print(f"  开机自启动: {'已开启' if info.enabled else '未开启'}（机制：{info.mechanism}）")
        if info.stale:
            _print(f"  注意      : 自启动项指向的路径与当前程序不一致：{info.command}")
    except Exception as exc:  # noqa: BLE001
        _print(f"  开机自启动: 读取失败（{exc}）")
    return 0 if health.healthy else 1


def _cmd_once() -> int:
    from ..config import Config
    from ..daemon import build_client, run_once
    from ..logs import get_logger

    cfg = Config.load(paths.config_path())
    return run_once(cfg, build_client(cfg), get_logger())


def _cmd_autostart(action: str) -> int:
    action = (action or "status").lower()
    if action == "on":
        ok, message = autostart.enable()
        _print(message)
        if ok:
            started, info = autostart.start_daemon()
            _print(info)
        return 0 if ok else 1
    if action == "off":
        ok, message = autostart.disable()
        _print(message)
        return 0 if ok else 1
    _print(json.dumps(autostart.status_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_cleanup_legacy() -> int:
    done = autostart.cleanup_legacy()
    if not done:
        _print("没有发现旧版自启动项。")
        return 0
    for line in done:
        _print(line)
    return 0


def _cmd_uninstall(argv: list) -> int:
    from ..uninstall import uninstall

    remove_data = any(arg in ("--remove-data", "--purge", "-d") for arg in argv)
    report = uninstall(remove_data=remove_data)
    for line in report.actions:
        _print(line)
    if report.errors:
        _print("")
        _print("以下步骤没有成功：")
        for line in report.errors:
            _print("  · " + line)
    _print("")
    _print("卸载完成。" if report.ok else "卸载完成，但有步骤失败（见上）。")
    return 0 if report.ok else 1


def _cmd_doctor() -> int:
    # 先把最要命的问题摆在屏幕上：注入模块会害得界面直接闪退
    try:
        from .. import conflicts, session

        hits = conflicts.detect()
        if hits:
            _print("⚠ " + conflicts.describe(hits))
            _print("")
        previous = session.previous()
        text = session.describe_previous(previous)
        if text:
            _print("⚠ " + text)
            _print("")
    except Exception as exc:  # noqa: BLE001
        _print(f"（自检失败，不影响诊断包生成：{exc}）")

    path = diagnostics.export_bundle()
    _print(f"诊断包已导出：{path}")
    _print("（账号、密码、内网地址、MAC 均已打码）")
    return 0


def _cmd_wifi() -> int:
    """无线网卡状态 + 附近网络扫描（排障"看不到 Wi-Fi 列表"用）。"""
    from .. import wifi

    _print("== 无线网卡状态 ==")
    try:
        status = wifi.interface_status()
    except Exception as exc:  # noqa: BLE001
        _print(f"  读取失败：{exc}")
        return 1
    _print(f"  可用: {'是' if status.available else '否'}")
    _print(f"  接口: {status.name or '-'}  网卡: {status.description or '-'}")
    _print(f"  状态: {status.state or '-'}")
    _print(f"  已连接: {'是' if status.connected else '否'}  SSID: {status.ssid or '-'}  信号: {status.signal}%")
    if status.error:
        _print(f"  错误: {status.error}")
    _print("")

    _print("== 附近网络（netsh wlan show networks）==")
    started = time.time()
    networks = []
    try:
        networks = wifi.scan()
    except Exception as exc:  # noqa: BLE001
        _print(f"  扫描失败：{exc}")

    elapsed = (time.time() - started) * 1000
    if networks:
        _print(f"  共 {len(networks)} 个（耗时 {elapsed:.0f} ms）：")
        for item in networks:
            _print(f"    {item.ssid:<28} 信号 {item.signal:>3}%  {item.kind_text}")
        return 0

    _print(f"  扫描结果为空（耗时 {elapsed:.0f} ms）。")
    _print("")
    _print("== netsh 原始返回（诊断用）==")
    for args in (["wlan", "show", "interfaces"], ["wlan", "show", "networks", "mode=bssid"]):
        try:
            code, text = wifi._netsh(args, timeout=20)
        except Exception as exc:  # noqa: BLE001
            _print(f"  netsh {' '.join(args)} -> 调用异常：{exc}")
            continue
        lines = [line for line in text.splitlines() if line.strip()]
        _print(f"  netsh {' '.join(args)}")
        _print(f"    退出码 = {code}，输出 {len(text)} 字符，非空行 {len(lines)}")
        for line in lines[:6]:
            _print(f"    | {line[:100]}")
    _print("")
    _print("  可能原因：没有无线网卡 / 网卡被禁用 / WLAN AutoConfig(WlanSvc) 未运行 /")
    _print("            netsh 被安全软件拦截。把上面的退出码与原文发来即可定位。")
    return 1


def _cmd_conflicts() -> int:
    """查"有没有第三方叠加软件会害得界面闪退"，便于远程排障。"""
    from .. import conflicts, hardening, session

    hits = conflicts.detect()
    _print("== 本进程是否已被注入 ==")
    if not hits:
        _print("  没有检测到已知的叠加类注入模块。")
    else:
        _print("  " + conflicts.describe(hits))
        _print("")
        for hit in hits:
            _print(f"  模块: {hit.module}")
            _print(f"  来源: {hit.vendor}")
            _print(f"  路径: {hit.path}")
            _print(f"  已确认导致闪退: {'是' if hit.verified else '未确认'}")
            _print(f"  建议: {hit.advice}")
            _print("")
    _print("")

    _print("== 是否有叠加软件在运行（这类软件会注入界面进程）==")
    overlays = hardening.status().get("overlay_processes") or []
    if not overlays:
        _print("  没有检测到。")
    else:
        for item in overlays:
            _print(f"  {item['vendor']}（进程 {item['process']}）")
        _print("  提示：它们在运行时会尝试向本程序注入 DLL。若界面几秒后消失，")
        _print("        请退出该软件，或在它的设置里把 CampusNet.exe 加入排除/忽略。")
    _print("")

    _print("== 进程加固现状 ==")
    status = hardening.status()
    if status.get("disabled_by_env"):
        _print("  已通过环境变量 CAMPUSNET_NO_HARDENING 关闭加固。")
    else:
        _print("  " + hardening.describe())
    _print("  说明：加固只在「检测到叠加软件正在运行」时才启用签名锁；")
    _print("        可用 CAMPUSNET_HARDEN=1 强制开启、CAMPUSNET_NO_HARDENING=1 关闭。")

    text = session.describe_previous(session.previous())
    if text:
        _print("")
        _print(text)
    return 0


def _cmd_paths() -> int:
    for key, value in paths.describe().items():
        _print(f"{key:14} = {value}")
    return 0


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print(USAGE)
        return 0

    command = argv[0].lower()
    rest = argv[1:]
    bootstrap(console=True)

    try:
        if command in ("status", "check"):
            return _cmd_status()
        if command in ("daemon", "service"):
            return _cmd_daemon_status()
        if command in ("once", "login"):
            return _cmd_once()
        if command == "autostart":
            return _cmd_autostart(rest[0] if rest else "status")
        if command == "start":
            ok, message = autostart.start_daemon()
            _print(message)
            return 0 if ok else 1
        if command == "stop":
            ok, message = autostart.stop_daemon()
            _print(message)
            return 0 if ok else 1
        if command in ("cleanup-legacy", "cleanup_legacy"):
            return _cmd_cleanup_legacy()
        if command in ("uninstall", "remove"):
            return _cmd_uninstall(rest)
        if command == "doctor":
            return _cmd_doctor()
        if command in ("conflicts", "overlay", "injections"):
            return _cmd_conflicts()
        if command in ("wifi", "wlan", "ssid"):
            return _cmd_wifi()
        if command == "paths":
            return _cmd_paths()
        if command in ("version", "--version", "-v"):
            _print(banner())
            return 0
    except Exception as exc:  # noqa: BLE001
        _print(f"[错误] {exc}")
        return 1

    _print(f"未知子命令：{command}")
    _print(USAGE)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
