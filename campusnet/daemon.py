"""后台守护主循环（V3.0：显式状态机 + 心跳 + 休眠恢复）。

"不弹窗"的落地方式：
- 进程用 pythonw.exe / windowed EXE 启动 => 没有控制台窗口；
- 所有异常就地捕获写日志，绝不让 Python 的未捕获异常弹出崩溃对话框；
- 日志只写文件（且在用户数据目录）；
- 子进程调用一律带 CREATE_NO_WINDOW。

状态机（每个状态都会写进心跳文件，供 GUI 显示"现在在干什么"）::

    starting ──► checking ──► online ──(sleep interval_online)──┐
                    │                                            │
                    ├─ wifi ──► auth ──► online                  │
                    ├─ wifi-failed ──► cooldown                  │
                    ├─ need-user（验证码等）──► paused-user        │
                    └─ network-down ──► cooldown ────────────────┘

重试策略：失败后 ``interval_offline`` 起步，每次 ×1.6，封顶 ``interval_max``；
一次成功立刻回到 ``interval_online``。账号密码错误时不再高频重试（避免账号被锁），
直接退到最大间隔，并只在第一次推一条通知。

休眠/待机恢复：用 ``time.monotonic()`` 与 ``time.time()`` 的差值检测"睡了一大觉"
（monotonic 不走、wall clock 走了），恢复后立刻重新探测一次而不是等满一个周期。
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto, paths, wifi
from .adapters import (
    STATUS_ALREADY_ONLINE,
    STATUS_BAD_CREDENTIALS,
    STATUS_CAPTCHA_REQUIRED,
    STATUS_MANUAL_REQUIRED,
    STATUS_SUCCESS,
    AuthOutcome,
)
from .authenticator import PortalAuthenticator
from .checker import CheckResult, check, describe_kind
from .config import Config, ConfigError, LoadReport, load_heartbeat, save_heartbeat
from .http import HttpClient
from .logs import setup_logging
from .notify import notify
from .version import banner, mutex_name

FAIL_LOG_INTERVAL = 300  # 持续失败时，最多每 5 分钟记一条，避免日志爆炸
WIFI_RETRY_INTERVAL = 25.0  # 连不上 WiFi 时，最多每 25 秒试一次，别把网卡刷爆
BROWSER_FALLBACK_INTERVAL = 900.0  # 浏览器兜底最多每 15 分钟试一次，避免反复弹窗
SLEEP_DETECT_THRESHOLD = 150.0  # wall clock 比 monotonic 多走这么多秒 = 电脑睡过

# 「优雅停止」标记文件：GUI / 卸载脚本写它，守护进程看到就退出。
# 比 taskkill 温和：不会打断正在进行的一次认证请求。
STOP_FLAG_NAME = "stop.flag"

# 状态名（写进心跳，GUI 直接用）
STATE_STARTING = "starting"
STATE_CHECKING = "checking"
STATE_ONLINE = "online"
STATE_WIFI = "wifi"
STATE_AUTH = "auth"
STATE_COOLDOWN = "cooldown"
STATE_NEED_USER = "need-user"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"

# 状态名 -> 面向用户的中文（GUI / CLI 共用，避免两处文案各自漂移）
STATE_TEXT = {
    STATE_STARTING: "正在启动",
    STATE_CHECKING: "正在检测网络",
    STATE_ONLINE: "网络正常",
    STATE_WIFI: "正在连接 Wi-Fi",
    STATE_AUTH: "正在认证",
    STATE_COOLDOWN: "等待重试",
    STATE_NEED_USER: "需要你处理",
    STATE_PAUSED: "已暂停自动连接",
    STATE_STOPPED: "已停止",
}

# 心跳多久没更新就认为"进程还在但不响应"。
# 后台最多每 60 秒（在线巡检间隔）写一次心跳，留 3 倍余量以容忍一次慢请求。
HEARTBEAT_TOLERANCE = 180.0


def daemon_health(tolerance: float = HEARTBEAT_TOLERANCE) -> "DaemonHealth":
    """检查后台守护**是否真的在工作**，而不只是"进程还在"。

    任务书要求：不能仅凭进程存在就判断服务正常，也要能区分
    "未运行 / 运行中且正常 / 进程在但无响应"。判定依据有三条：

    1. 命名互斥体是否被持有（进程存在）；
    2. 心跳文件是否新鲜（守护每一轮都会重写它）；
    3. 心跳里的状态是否是"已停止"。

    只有 1+2+3 都通过才算 ``healthy``。``reason`` 给出可读依据，UI/CLI 直接显示。
    """
    from . import autostart, paths
    from .config import load_heartbeat

    health = DaemonHealth()
    try:
        health.running = bool(autostart.daemon_running())
    except Exception as exc:  # noqa: BLE001  探测失败按"未运行"处理，但要说明原因
        health.reason = f"无法探测后台进程：{exc}"
        return health

    beat = load_heartbeat()
    if beat:
        health.state = str(beat.get("state") or "")
        health.detail = str(beat.get("detail") or "")
        health.pid = int(beat.get("pid") or 0)
        try:
            updated = float(beat.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        if updated:
            health.age = max(0.0, time.time() - updated)

    if not health.running:
        health.reason = "后台进程未运行（没有持有运行锁）。"
        if health.state == STATE_STOPPED and health.age >= 0:
            health.reason = "后台进程已停止（上次心跳状态为已停止）。"
        return health

    if not beat:
        health.reason = "后台进程在运行，但还没有写出心跳（可能刚启动或无法写数据目录）。"
        return health
    if health.state == STATE_STOPPED:
        health.reason = "后台进程仍持有运行锁，但心跳显示已停止（可能是上次残留）。"
        return health
    if health.age < 0:
        health.reason = "后台进程在运行，但心跳时间戳无效。"
        return health
    if health.age > tolerance:
        health.reason = (
            f"后台进程在运行，但心跳已 {health.age:.0f} 秒没有更新"
            f"（超过 {tolerance:.0f} 秒判定为无响应）。"
        )
        return health

    health.healthy = True
    label = STATE_TEXT.get(health.state, health.state or "未知状态")
    health.reason = f"后台守护运行中（{label}），{health.age:.0f} 秒前刚更新过心跳。"
    return health


@dataclass
class DaemonHealth:
    """后台守护健康状态（结论 + 依据）。"""

    running: bool = False
    healthy: bool = False
    state: str = ""
    detail: str = ""
    age: float = -1.0
    pid: int = 0
    reason: str = ""

    @property
    def state_text(self) -> str:
        return STATE_TEXT.get(self.state, self.state or "")

    @property
    def label(self) -> str:
        """一句短语，供状态栏/CLI 直接显示。"""
        if self.healthy:
            text = f"运行中 · {self.state_text}" if self.state_text else "运行中"
            if self.detail:
                text += f"（{self.detail}）"
            return text
        if self.running:
            return "运行中但无响应"
        return "未运行"

    @property
    def needs_attention(self) -> bool:
        """是否需要提醒用户（无响应、需要处理、未运行但用户以为在跑）。"""
        return self.running and not self.healthy or self.state == STATE_NEED_USER

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "healthy": self.healthy,
            "state": self.state,
            "state_text": self.state_text,
            "detail": self.detail,
            "age_seconds": round(self.age, 1),
            "pid": self.pid,
            "label": self.label,
            "reason": self.reason,
            "needs_attention": self.needs_attention,
        }



def build_client(cfg: Config) -> HttpClient:
    """按配置构造 HTTP 客户端（TLS 逐主机策略在这里落地）。"""
    return HttpClient(
        timeout=cfg.request_timeout,
        tls_verify=cfg.tls_verify_for,
        use_system_proxy=bool(cfg.use_system_proxy),
        user_agent=cfg.user_agent,
    )


def make_verifier(client: HttpClient, cfg: Config, log, tries: int = 3):
    """"认证到底成没成"只看真实联网结果，不信页面上的成功文字。"""

    def verify() -> bool:
        for i in range(tries):
            try:
                if check(client, cfg).online:
                    return True
            except Exception as exc:  # noqa: BLE001
                log.debug("验证时探测异常：%s", exc)
            if i < tries - 1:
                time.sleep(1.2)
        return False

    return verify


def load_credential(cfg: Config, log):
    """解密密码；任何失败都只写日志并返回 None（绝不让凭据进日志）。"""
    if not cfg.is_configured():
        log.error("配置不完整：缺少 username 或 password_enc。请打开 CampusNet 填写账号密码。")
        return None
    try:
        return crypto.unprotect(cfg.password_enc)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "密码解密失败：%s（多半是换了 Windows 用户、换了电脑，或重装过系统；"
            "请在 CampusNet 里重新输入一次密码）", exc,
        )
        return None


# ---------------------------------------------------------------- 单次动作

def run_once(cfg: Config, client: HttpClient, log) -> int:
    """只检查/认证一次就退出（排障、GUI「立即认证」都走这里）。"""
    password = load_credential(cfg, log)
    if password is None:
        return 3
    state = check(client, cfg)
    if state.online:
        log.info("网络已在线，无需认证（%s）", state.detail)
        return 0
    log.info("当前无法上网：状态=%s（%s）", state.kind, describe_kind(state.kind))
    if state.is_network_layer:
        log.warning("这是网络层问题，不是认证问题：%s", state.explain())
        # 网络层没通时仍尝试一次认证：校园网常见"未认证 -> DNS 被拦"
    portal = state.portal_url or cfg.portal_url
    if not portal:
        log.error("没有可用的认证页地址。请在 CampusNet 的「认证」页点「探测认证地址」后保存")
        return 2
    auth = PortalAuthenticator(client, cfg, log)
    result = auth.login(portal, cfg.username, password, make_verifier(client, cfg, log, tries=1))
    if result.ok:
        log.info("认证成功：%s", result.reason)
        return 0
    log.error("认证失败（%s）：%s", result.status, result.reason)
    return 1


# ---------------------------------------------------------------- 守护循环

@dataclass
class LoopSnapshot:
    """一次循环的可观察状态（写心跳、给 GUI 读）。"""

    state: str = STATE_STARTING
    kind: str = ""
    detail: str = ""
    portal_url: str = ""
    next_check_at: float = 0.0
    consecutive_failures: int = 0
    last_auth_status: str = ""
    last_auth_reason: str = ""
    last_success_ts: float = 0.0
    pid: int = 0
    version: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = {
            "state": self.state,
            "network_kind": self.kind,
            "detail": self.detail,
            "portal_url": self.portal_url,
            "next_check_at": int(self.next_check_at or 0),
            "consecutive_failures": self.consecutive_failures,
            "last_auth_status": self.last_auth_status,
            "last_auth_reason": self.last_auth_reason,
            "last_success_ts": int(self.last_success_ts or 0),
            "pid": self.pid,
            "version": self.version,
        }
        data.update(self.extra or {})
        return data


class Daemon:
    """后台守护。构造后调用 :meth:`run_forever`。"""

    def __init__(self, config_path: Path | None = None, *, stop_event: threading.Event | None = None,
                 log=None):
        self.config_path = Path(config_path or paths.config_path())
        self.stop_event = stop_event or threading.Event()
        self.log = log
        self.snapshot = LoopSnapshot(pid=0)
        self._cfg_mtime = 0.0
        self._interval = 0.0
        self._last_fail_log = 0.0
        self._had_failure = False
        self._notified_bad = False
        self._notified_need_user = False
        self._last_wifi_try = 0.0
        self._last_browser_try = 0.0
        self._last_online: bool | None = None
        self._wall_start = 0.0
        self._mono_start = 0.0

    # ---------- 对外 ----------

    def run_forever(self, cfg: Config | None = None) -> int:
        log = self.log
        if log is None:
            raise RuntimeError("Daemon 需要 logger")
        try:
            cfg = cfg or self._load_config()
        except ConfigError as exc:
            log.error("配置读取失败：%s", exc)
            self._heartbeat(state=STATE_STOPPED, detail=str(exc))
            return 4

        password = load_credential(cfg, log)
        if password is None:
            self._heartbeat(state=STATE_NEED_USER, detail="账号密码不可用，需要重新设置")
            return 3

        client = build_client(cfg)
        auth = PortalAuthenticator(client, cfg, log)
        verify = make_verifier(client, cfg, log)
        self._interval = float(cfg.interval_offline)
        self._cfg_mtime = _mtime(self.config_path)
        self._wall_start = time.time()
        self._mono_start = time.monotonic()
        self.snapshot.version = banner()
        self.snapshot.pid = _pid()

        log.info(
            "%s 后台守护启动：目标无线=%s，portal=%s，在线巡检 %ss，掉线重试 %ss，TLS 校验=%s",
            banner(),
            cfg.wifi_ssid or "(不主动连 WiFi)",
            cfg.portal_url or "(自动探测)",
            cfg.interval_online,
            cfg.interval_offline,
            "关闭（全局）" if cfg.insecure_tls else "启用",
        )

        while not self.stop_event.is_set():
            if self._stop_requested():
                log.info("收到停止请求（stop.flag），退出后台守护")
                self._clear_stop_flag()
                self._heartbeat(state=STATE_STOPPED, detail="收到停止请求")
                return 0
            try:
                cfg, client, auth, verify, password = self._maybe_reload(
                    cfg, client, auth, verify, password
                )
                password = self._tick(cfg, client, auth, verify, password)
            except KeyboardInterrupt:  # pragma: no cover
                log.info("收到中断信号，退出")
                self._heartbeat(state=STATE_STOPPED, detail="收到中断信号")
                return 0
            except Exception as exc:  # noqa: BLE001  任何异常都不允许冒泡
                log.exception("主循环异常，5 秒后继续：%s", exc)
                self._heartbeat(state=STATE_COOLDOWN, detail=f"内部异常：{exc}")
                self._sleep(5.0)
                continue
            self._sleep(self._interval * (1 + random.uniform(0, 0.15)))

        self._heartbeat(state=STATE_STOPPED, detail="已停止")
        return 0

    # ---------- 单轮 ----------

    def _tick(self, cfg: Config, client: HttpClient, auth: PortalAuthenticator,
              verify, password: str):
        """跑一轮：Wi-Fi → 探测 → 认证。返回（可能被热更新替换的）密码。"""
        log = self.log
        self._detect_resume()

        # ---- 第 0 步：把无线网连到目标 SSID（不弹任何窗口） ----
        if cfg.wifi_ssid and cfg.auto_enabled:
            self._ensure_wifi(cfg)

        self._heartbeat(state=STATE_CHECKING)
        state: CheckResult = check(client, cfg)
        self.snapshot.kind = state.kind
        self.snapshot.detail = state.detail
        self.snapshot.portal_url = state.portal_url or cfg.portal_url

        if state.online:
            if self._last_online is False:
                log.info("网络已恢复，恢复常规巡检")
                if self._had_failure and cfg.notify_on_success:
                    notify(cfg, "网络已恢复。", log)
            self._last_online = True
            self._had_failure = False
            self._notified_bad = False
            self._notified_need_user = False
            self.snapshot.consecutive_failures = 0
            self._interval = float(cfg.interval_online)
            self._heartbeat(state=STATE_ONLINE)
            return password

        if self._last_online is not False:
            log.warning("无法上网：状态=%s（%s）", state.kind, state.explain())
        self._last_online = False

        if not cfg.auto_enabled:
            # 界面上的总开关关着：只巡检状态，什么都不做
            self._interval = float(cfg.interval_online)
            self._heartbeat(state=STATE_PAUSED, detail="「自动连接与认证」已关闭，只巡检状态")
            return password

        portal = state.portal_url or cfg.portal_url
        if not portal:
            self._note_failure(state, None)
            self._heartbeat(state=STATE_COOLDOWN)
            return password

        self._heartbeat(state=STATE_AUTH)
        result: AuthOutcome = auth.login(portal, cfg.username, password, verify)
        self.snapshot.last_auth_status = result.status
        self.snapshot.last_auth_reason = result.reason

        if result.ok:
            log.info("自动认证成功（%s，尝试 %d 次）",
                     result.reason or result.status, result.attempts)
            if result.status == STATUS_ALREADY_ONLINE and not result.reason:
                log.info("服务端判断本机已在线")
            self._had_failure = False
            self._notified_bad = False
            self._notified_need_user = False
            self._last_fail_log = 0.0
            self.snapshot.consecutive_failures = 0
            self.snapshot.last_success_ts = time.time()
            self._interval = float(cfg.interval_online)
            self._last_online = True
            self._heartbeat(state=STATE_ONLINE)
            return password

        self._note_failure(state, result)

        if result.status == STATUS_BAD_CREDENTIALS:
            # 账号密码不对：重试没有意义，别把账号撞锁
            self._interval = float(cfg.interval_max)
            if not self._notified_bad:
                self._notified_bad = True
                notify(cfg, f"自动登录失败：账号或密码有问题。\n{result.reason}\n请在 CampusNet 里重新设置。", log)
            self._heartbeat(state=STATE_NEED_USER, detail="账号或密码不正确，已降低重试频率")
            return password

        if result.status in (STATUS_CAPTCHA_REQUIRED, STATUS_MANUAL_REQUIRED) or result.needs_user:
            self._interval = float(cfg.interval_max)
            if not self._notified_need_user:
                self._notified_need_user = True
                log.warning("认证需要人工处理：%s（已降低重试频率）", result.reason)
                notify(cfg, f"校园网认证需要人工处理：\n{result.reason}", log)
            self._heartbeat(state=STATE_NEED_USER, detail=result.reason)
            return password

        if not result.retryable:
            self._interval = float(cfg.interval_max)
            self._heartbeat(state=STATE_NEED_USER, detail=result.reason)
            return password

        self._interval = min(max(self._interval, float(cfg.interval_offline)) * 1.6,
                             float(cfg.interval_max))

        # 可选兜底：HTTP 打不通就开浏览器再试一次（用户显式开启才走）
        if cfg.browser_fallback:
            now = time.monotonic()
            if now - self._last_browser_try >= BROWSER_FALLBACK_INTERVAL:
                self._last_browser_try = now
                log.info("HTTP 认证未成功，尝试浏览器兜底")
                if self._browser_fallback(cfg, password):
                    log.info("浏览器兜底认证成功")
                    self._had_failure = False
                    self._last_fail_log = 0.0
                    self._interval = float(cfg.interval_online)
                    self._last_online = True
                    self._heartbeat(state=STATE_ONLINE)
                    return password

        self._heartbeat(state=STATE_COOLDOWN)
        return password

    # ---------- 辅助 ----------

    def _load_config(self) -> Config:
        report = LoadReport()
        try:
            cfg = Config.load(self.config_path, report=report)
        except ConfigError:
            raise
        if self.log is not None:
            for message in report.messages():
                self.log.warning(message)
        return cfg

    def _maybe_reload(self, cfg, client, auth, verify, password):
        """配置热更新：用户重新配置后，不必重启进程。"""
        new_mtime = _mtime(self.config_path)
        if not new_mtime or new_mtime == self._cfg_mtime:
            return cfg, client, auth, verify, password
        self._cfg_mtime = new_mtime
        try:
            new_cfg = self._load_config()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("配置热加载失败，继续用旧配置：%s", exc)
            return cfg, client, auth, verify, password

        if new_cfg.password_enc and new_cfg.password_enc != cfg.password_enc:
            try:
                new_password = crypto.unprotect(new_cfg.password_enc)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("新配置里的密码解不开，继续用旧密码：%s", exc)
                new_password = password
            password = new_password
        self.log.info("检测到配置更新，已热加载")
        client = build_client(new_cfg)
        auth = PortalAuthenticator(client, new_cfg, self.log)
        verify = make_verifier(client, new_cfg, self.log)
        self._interval = float(new_cfg.interval_offline)
        self._notified_bad = False
        self._notified_need_user = False
        return new_cfg, client, auth, verify, password

    def _note_failure(self, state: CheckResult, result: AuthOutcome | None) -> None:
        """失败日志限流：持续失败时最多每 5 分钟一条。"""
        was_failing = self._had_failure
        self._had_failure = True
        self.snapshot.consecutive_failures += 1
        now = time.monotonic()
        severe = bool(result and (result.bad_credentials or not result.retryable))
        if now - self._last_fail_log >= FAIL_LOG_INTERVAL or severe:
            self._last_fail_log = now
            if result is not None:
                self.log.error(
                    "自动认证未成功（%s）：%s（网络状态：%s，详情：%s）",
                    result.status, result.reason, state.kind, state.detail,
                )
            else:
                self.log.warning(
                    "未探测到认证页（网络状态：%s）。%s",
                    state.kind, state.explain(),
                )
        elif not was_failing:
            self.log.warning("自动认证未成功，将按退避策略重试。")

    def _ensure_wifi(self, cfg: Config) -> None:
        """把无线网连到目标 SSID（限流，失败只写日志）。"""
        now = time.monotonic()
        if self._last_wifi_try and now - self._last_wifi_try < WIFI_RETRY_INTERVAL:
            return
        self._heartbeat(state=STATE_WIFI)
        try:
            status = wifi.interface_status()
            if status.connected and status.ssid == cfg.wifi_ssid:
                return
            self._last_wifi_try = now
            ok, msg = wifi.ensure_connected(cfg.wifi_ssid, cfg.wifi_connect_mode, log=self.log)
            if ok:
                self.log.info("已自动连接到无线网络 %s", cfg.wifi_ssid)
            else:
                self.log.warning("自动连接无线网络 %s 未成功：%s", cfg.wifi_ssid, msg)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("无线网络处理异常：%s", exc)

    def _browser_fallback(self, cfg: Config, password: str) -> bool:
        """HTTP 认证失败后的兜底：开浏览器把页面填了（用户显式开启才会调到）。"""
        try:
            from . import webauth

            result = webauth.run(
                cfg, password,
                progress=lambda text: self.log.info("浏览器兜底：%s", text),
                timeout=90.0,
            )
            if result.get("ok"):
                return True
            self.log.warning("浏览器兜底未成功：%s（状态：%s）", result.get("reason"), result.get("state"))
            return False
        except Exception as exc:  # noqa: BLE001
            self.log.warning("浏览器兜底异常：%s", exc)
            return False

    def _detect_resume(self) -> float:
        """检测"电脑刚睡醒"。返回睡过的秒数（0 表示没睡）。"""
        wall = time.time() - self._wall_start
        mono = time.monotonic() - self._mono_start
        self._wall_start = time.time()
        self._mono_start = time.monotonic()
        drift = wall - mono
        if drift >= SLEEP_DETECT_THRESHOLD:
            self.log.info("检测到系统休眠/挂起约 %.0f 秒，立即重新探测网络", drift)
            self._interval = float(self._last_interval_floor())
            return drift
        return 0.0

    def _last_interval_floor(self) -> float:
        return max(3.0, float(getattr(self, "_interval", 8.0)))

    def _sleep(self, seconds: float) -> None:
        """可中断的睡眠。

        必须同时盯两件事：

        * ``stop_event``：GUI / 测试里主动停止；
        * ``stop.flag``：卸载与 ``CampusNetCLI stop`` 走的是这条路。

        曾经只等 ``stop_event``，结果守护在 ``interval_online``（默认 60s，
        最长 120s）的睡眠里根本看不到停止标记 —— 卸载要等两分钟、清理用户数据
        时文件还被占用。这里改成每秒检查一次，1 秒内响应。
        """
        deadline = time.monotonic() + max(1.0, float(seconds))
        while not self.stop_event.is_set():
            if self._stop_requested():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(1.0, remaining))

    # ---------- 停止标记 ----------

    @staticmethod
    def _stop_flag() -> Path:
        return paths.data_dir() / STOP_FLAG_NAME

    def _stop_requested(self) -> bool:
        try:
            return self._stop_flag().exists()
        except Exception:  # noqa: BLE001
            return False

    def _clear_stop_flag(self) -> None:
        try:
            self._stop_flag().unlink()
        except Exception:  # noqa: BLE001
            pass

    def _heartbeat(self, *, state: str = "", detail: str = "") -> None:
        snap = self.snapshot
        if state:
            snap.state = state
        if detail:
            snap.detail = detail
        snap.next_check_at = time.time() + max(0.0, self._interval)
        try:
            save_heartbeat(snap.as_dict())
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 入口

def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:  # noqa: BLE001
        return 0.0


def _pid() -> int:
    try:
        import os

        return os.getpid()
    except Exception:  # noqa: BLE001
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="CampusNetService",
        description="校园网后台自动登录（静默、无弹窗）",
    )
    parser.add_argument("--config", default="", help="配置文件路径（默认用户数据目录）")
    parser.add_argument("--once", action="store_true", help="只检查/认证一次就退出（排障用）")
    parser.add_argument("--check", action="store_true", help="只检查联网状态，不做认证")
    parser.add_argument("--verbose", action="store_true", help="同时把日志打到控制台（仅排障用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config) if args.config else paths.config_path()

    migration = paths.migrate_legacy_data()
    try:
        paths.ensure_dirs()
    except Exception:  # noqa: BLE001
        pass

    report = LoadReport()
    try:
        cfg = Config.load(config_path, report=report)
    except ConfigError as exc:
        print(f"[致命] {exc}", file=sys.stderr)
        return 4

    log = setup_logging(cfg, console=bool(args.verbose))
    if migration.ran:
        if migration.ok:
            log.info("已从旧版目录迁移用户数据：%s", migration.summary())
        else:
            log.warning("旧版数据迁移有问题（不影响本次运行）：%s", migration.summary())
    for message in report.messages():
        log.warning(message)

    if args.check:
        client = build_client(cfg)
        state = check(client, cfg)
        log.info("状态：%s（%s）%s", "在线" if state.online else "离线",
                 state.kind, state.detail)
        if args.verbose:
            print(f"online={state.online} kind={state.kind} "
                  f"portal={state.portal_url or '-'} detail={state.detail}")
        return 0 if state.online else 1

    client = build_client(cfg)
    if args.once:
        return run_once(cfg, client, log)

    handle = _acquire_mutex()
    if handle is None:
        log.info("已有后台实例在运行，本进程退出")
        return 0
    daemon = Daemon(config_path, log=log)
    try:
        return daemon.run_forever(cfg)
    except Exception as exc:  # noqa: BLE001 最后一道防线
        log.exception("守护进程异常退出：%s", exc)
        return 1


def _acquire_mutex():
    """单实例互斥：新名字 + 旧名字都要试，避免与旧版本同时跑。"""
    handle = crypto.acquire_single_instance(mutex_name(paths.data_dir_key()))
    if handle is None:
        return None
    from .version import LEGACY_MUTEX_NAMES

    for name in LEGACY_MUTEX_NAMES:
        legacy = crypto.acquire_single_instance(name)
        if legacy is None:
            # 旧版进程还在：安静退出，避免两个守护同时登录
            return None
    return handle


def cli() -> None:  # pragma: no cover
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        code = 1
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    cli()
