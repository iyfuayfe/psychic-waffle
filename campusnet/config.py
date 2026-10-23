"""配置读写：带版本号、校验、迁移与原子保存。

V3.0 相对旧版的变化（全部向后兼容）：

* 引入 ``config_version``。旧配置（没有这个字段）读入时自动当作 v1，
  先备份原始文件再就地升级，并把结果记进 :class:`LoadReport`。
* 新增 :meth:`Config.validate` —— 布尔、数值范围、URL 合法性统一校验，
  非法值**不会**让程序崩掉，而是回落默认值并给出可读提示。
* 未知字段会被保留下来（``_extra``），保存时原样写回，避免新旧版本互踩。
* :meth:`Config.save` 使用"先写临时文件再替换"，并保留 ``.bak``。

所有路径都来自 :mod:`campusnet.paths`：安装目录只读，用户数据在
``%LOCALAPPDATA%\\CampusNet``。旧版把配置文件放在项目根目录，升级时由
``paths.migrate_legacy_data()`` 复制过来（旧文件保留）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.parse
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path

from . import paths
from .security import is_http_url, redact_text
from .version import CONFIG_SCHEMA_VERSION

# ---- 兼容旧代码里的模块级常量（值已改为指向用户数据目录） ----
PROJECT_ROOT = paths.install_dir()
DEFAULT_CONFIG_PATH = paths.config_path()
LOG_DIR = paths.log_dir()
STATE_PATH = paths.state_path()

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 联网判定目标：返回内容/状态码符合预期 => 已经能上外网
DEFAULT_CHECK_URLS = [
    {"url": "http://www.msftconnecttest.com/connecttest.txt", "expect_text": "Microsoft Connect Test"},
    {"url": "http://connect.rom.miui.com/generate_204", "expect_status": 204},
    {"url": "http://captive.apple.com/hotspot-detect.html", "expect_text": "Success"},
]


class ConfigError(Exception):
    """配置无法解析（损坏的 JSON、根节点不是对象）。"""


@dataclass
class CheckTarget:
    url: str
    expect_text: str = ""
    expect_status: int = 0


@dataclass
class CustomLogin:
    """当认证接口不是普通 HTML 表单时的逃生舱（例如 Srun/深澜、Dr.COM 的 JSON 接口）。"""

    enabled: bool = False
    url: str = ""
    method: str = "POST"  # POST / GET
    username_field: str = ""
    password_field: str = ""
    extra_fields: dict = field(default_factory=dict)
    encoding: str = ""  # 空 = 自动嗅探
    content_type: str = "form"  # form = application/x-www-form-urlencoded


@dataclass
class DrcomConfig:
    """城市热点 Dr.COM / 哆点 ePortal 专用参数（自动识别，一般不用改）。"""

    enabled: bool = True
    host: str = ""  # 留空则从 portal_url 推导，例如 192.0.2.10
    port: int = 801  # 页面里 authloginport=801
    account_suffix: str = ""  # 如 @cmcc / @dx / @lt，留空则不改动账号
    wlan_user_ip: str = ""  # 留空则自动获取（跳转URL -> 页面JS -> 本机出口IP）
    wlan_ac_ip: str = ""
    wlan_ac_name: str = ""
    wlan_user_mac: str = "000000000000"
    # 以下三项留默认即可：程序会向门户 page/loadConfig 问真实值
    js_version: str = ""  # 门户 a59.js 里的 jsVersion，留空 = 4.2.1
    login_method: int = -1  # -1 = 自动询问门户（1 = 新版加密接口，0 = 老式明文接口）
    terminal_type: int = 1  # 1 = PC（账号前缀 ,0,）；2 = 手机（,1,）


@dataclass
class Config:
    # ---------- 元信息 ----------
    config_version: int = CONFIG_SCHEMA_VERSION

    # ---------- 无线网络（界面上的"校园网设置"） ----------
    # 目标 SSID：留空 = 不主动连 WiFi，只按系统当前的网络做认证
    wifi_ssid: str = ""
    # 连接方式：open = 开放网络（没有已保存配置时临时建一个）
    #           saved = 使用 Windows 已保存的配置（要密码/企业认证的网络必须这样）
    wifi_connect_mode: str = "open"
    # 「自动连接与认证」总开关：关掉 = 后台只巡检状态，不主动连 WiFi、不自动登录
    auto_enabled: bool = True
    # 「连接后进行网页认证」：关掉 = 只连 WiFi，不做 Portal 认证（免认证/已绑 MAC 的网络）
    portal_auth_enabled: bool = True
    # 打开认证页后是否自动填写并提交已保存的账号密码
    autofill_click: bool = True
    # 浏览器模式里要选的"运营商 / 服务"名称（页面上怎么写就怎么填，例如 中国移动）。
    # 留空 = 页面没有这个选项，或者不需要选。
    service_option: str = ""
    # 浏览器模式的 CSS 选择器覆盖（页面结构太特殊、自动识别不中时才填）
    user_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""
    # 后台守护：HTTP 直接提交失败后，是否用浏览器再试一次（会弹出窗口，默认关闭）
    browser_fallback: bool = False

    # 认证页地址；留空 = 每次自动探测
    portal_url: str = ""
    username: str = ""
    password_enc: str = ""  # DPAPI 密文（base64）

    check_urls: list = field(default_factory=lambda: [dict(x) for x in DEFAULT_CHECK_URLS])

    interval_online: int = 60  # 已在线时的巡检间隔（秒）
    interval_offline: int = 8  # 掉线后的重试间隔（秒）
    interval_max: int = 120  # 退避上限
    request_timeout: int = 8

    # ---- TLS ----
    # V3 默认**验证证书**。校园网门户大量使用自签证书，所以单列了
    # tls_insecure_hosts：只对列表里明确列出的主机降级，不影响其它站点。
    insecure_tls: bool = False  # 全局关闭校验：默认关闭（不安全），需用户显式开启
    tls_insecure_hosts: list = field(default_factory=list)  # 例如 ["192.0.2.10"]

    use_system_proxy: bool = False  # 关掉系统代理，避免 Clash 之类挡住探测
    include_submit_button: bool = False

    # ---- 凭据投递白名单 ----
    # 认证接口和认证页不在同一个域名时（部分学校如此），把接口域名显式列在这里。
    # 空 = 只允许提交给认证页所在的主机。
    trusted_auth_hosts: list = field(default_factory=list)

    # ---- 诊断 ----
    # 认证页转储：默认关闭；打开时也只写"已脱敏"的副本
    portal_dump_enabled: bool = False
    portal_dump_max_files: int = 5

    # 页面出现这些词就认为已经登录
    already_online_keywords: list = field(
        default_factory=lambda: ["注销", "退出登录", "已登录", "logout", "you are online", "上网时长"]
    )
    # 页面出现这些词就认为账号密码有问题（不必反复重试）
    bad_credential_keywords: list = field(
        default_factory=lambda: ["密码错误", "用户名或密码", "账号或密码", "认证失败", "invalid password", "user not found"]
    )

    user_agent: str = DEFAULT_USER_AGENT
    log_level: str = "INFO"
    log_max_bytes: int = 1_000_000
    log_backups: int = 3

    # 可选：登录结果推送到手机（企业微信/钉钉群机器人 webhook）。留空 = 不推送。
    notify_webhook: str = ""
    notify_on_success: bool = False  # 只在"掉线后恢复"时推，避免每次巡检都推

    custom: CustomLogin = field(default_factory=CustomLogin)
    drcom: DrcomConfig = field(default_factory=DrcomConfig)

    # ---------- 运行时附加信息（不写入 JSON 的字段放这里） ----------
    _extra: dict = field(default_factory=dict, repr=False)

    # ---------- 读写 ----------

    @classmethod
    def load(cls, path: str | Path | None = None, *, report: "LoadReport | None" = None) -> "Config":
        """读取配置。文件不存在时返回默认配置（不算错误）。"""
        p = Path(path) if path else paths.config_path()
        cfg = cls()
        if not p.exists():
            if report is not None:
                report.path = str(p)
                report.existed = False
            return cfg
        try:
            raw_text = p.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"配置文件无法读取：{p}（{exc}）") from exc
        try:
            raw = json.loads(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(
                f"配置文件不是合法的 JSON：{p}（{exc}）。"
                f"原文件已保留未改动，可以修复或删除后重新配置。"
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件根节点必须是对象：{p}")

        version = raw.get("config_version")
        if report is not None:
            report.path = str(p)
            report.existed = True
            report.from_version = int(version) if isinstance(version, int) else 1

        # ---- 迁移：旧版（无版本号）先备份，再补默认值 ----
        if not isinstance(version, int) or version < CONFIG_SCHEMA_VERSION:
            if report is not None:
                report.migrated = True
                report.backup = _backup_config(p)
            cfg._merge(raw)
            cfg.config_version = CONFIG_SCHEMA_VERSION
        else:
            cfg._merge(raw)

        problems = cfg.validate()
        if report is not None:
            report.problems = problems
        return cfg

    def _merge(self, raw: dict) -> None:
        known = {f.name: f for f in fields(Config) if not f.name.startswith("_")}
        for key, value in raw.items():
            if key == "config_version":
                continue
            if key not in known:
                self._extra[key] = value  # 未知字段原样保留
                continue
            if key == "custom":
                self.custom = _merge_sub(CustomLogin, value)
                continue
            if key == "drcom":
                self.drcom = _merge_sub(DrcomConfig, value)
                continue
            if key == "check_urls":
                parsed = _parse_check_urls(value)
                if parsed:
                    self.check_urls = parsed
                continue
            if key in ("tls_insecure_hosts", "trusted_auth_hosts"):
                setattr(self, key, _as_str_list(value))
                continue
            if key in ("already_online_keywords", "bad_credential_keywords"):
                items = _as_str_list(value)
                if items:
                    setattr(self, key, items)
                continue
            setattr(self, key, _coerce(_field_default(known[key]), value))

    def validate(self) -> list:
        """把配置规整成合法值，返回 [(字段, 问题说明), ...]（不抛异常）。"""
        problems: list = []

        def note(name: str, message: str) -> None:
            problems.append((name, message))

        # ---- 布尔 ----
        for name in ("auto_enabled", "portal_auth_enabled", "autofill_click",
                     "browser_fallback", "insecure_tls", "use_system_proxy",
                     "include_submit_button", "notify_on_success", "portal_dump_enabled"):
            value = getattr(self, name)
            if isinstance(value, bool):
                continue
            coerced = _to_bool(value, default=bool(_field_default(
                next(f for f in fields(Config) if f.name == name))))
            setattr(self, name, coerced)
            note(name, f"应为 true/false，已按 {coerced} 处理")

        self.drcom.enabled = _to_bool(self.drcom.enabled, default=True)
        self.custom.enabled = _to_bool(self.custom.enabled, default=False)

        # ---- 数值范围 ----
        self.interval_online = _int_in_range(
            self.interval_online, 10, 3600, 60, "interval_online", note)
        self.interval_offline = _int_in_range(
            self.interval_offline, 3, 600, 8, "interval_offline", note)
        self.interval_max = _int_in_range(
            self.interval_max, 10, 3600, 120, "interval_max", note)
        if self.interval_max < self.interval_offline:
            self.interval_max = self.interval_offline
            note("interval_max", "小于 interval_offline，已抬到同值")
        self.request_timeout = _int_in_range(
            self.request_timeout, 3, 120, 8, "request_timeout", note)
        self.log_max_bytes = _int_in_range(
            self.log_max_bytes, 100_000, 50_000_000, 1_000_000, "log_max_bytes", note)
        self.log_backups = _int_in_range(self.log_backups, 0, 20, 3, "log_backups", note)
        self.portal_dump_max_files = _int_in_range(
            self.portal_dump_max_files, 0, 50, 5, "portal_dump_max_files", note)
        self.drcom.port = _int_in_range(self.drcom.port, 1, 65535, 801, "drcom.port", note)
        self.drcom.terminal_type = _int_in_range(
            self.drcom.terminal_type, 1, 2, 1, "drcom.terminal_type", note)

        # ---- 字符串 ----
        self.wifi_ssid = str(self.wifi_ssid or "").strip()
        self.username = str(self.username or "").strip()
        self.portal_url = str(self.portal_url or "").strip()
        self.service_option = str(self.service_option or "").strip()
        for name in ("user_selector", "password_selector", "submit_selector"):
            setattr(self, name, str(getattr(self, name) or "").strip())
        if self.wifi_connect_mode not in ("open", "saved"):
            note("wifi_connect_mode", f"未知取值 {self.wifi_connect_mode!r}，已改为 saved")
            self.wifi_connect_mode = "saved"
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            note("log_level", f"未知取值 {self.log_level!r}，已改为 INFO")
            self.log_level = "INFO"
        else:
            self.log_level = self.log_level.upper()

        # ---- URL ----
        if self.portal_url and not is_http_url(self.portal_url):
            note("portal_url", "不是合法的 http/https 地址，已清空（下次自动探测）")
            self.portal_url = ""
        if self.custom.enabled:
            if not self.custom.url:
                note("custom.url", "已启用自定义接口但地址为空，已关闭自定义接口")
                self.custom.enabled = False
            elif not is_http_url(self.custom.url):
                note("custom.url", "不是合法的 http/https 地址，已关闭自定义接口")
                self.custom.enabled = False
            self.custom.method = str(self.custom.method or "POST").upper()
            if self.custom.method not in ("POST", "GET"):
                self.custom.method = "POST"
        if self.notify_webhook and not is_http_url(self.notify_webhook):
            note("notify_webhook", "不是合法的 http/https 地址，已清空")
            self.notify_webhook = ""

        # ---- 列表 ----
        self.tls_insecure_hosts = [h for h in _as_str_list(self.tls_insecure_hosts)]
        self.trusted_auth_hosts = [h for h in _as_str_list(self.trusted_auth_hosts)]
        if self.insecure_tls:
            note(
                "insecure_tls",
                "已全局关闭 TLS 证书校验（会降低安全性）。建议改用 tls_insecure_hosts "
                "只对校园网门户放行。",
            )

        # ---- 检测目标 ----
        targets = [t for t in self.targets() if is_http_url(t.url)]
        if not targets:
            self.check_urls = [dict(x) for x in DEFAULT_CHECK_URLS]
            note("check_urls", "没有可用的探测地址，已恢复默认三目标")

        self.drcom.host = str(self.drcom.host or "").strip()
        self.drcom.account_suffix = str(self.drcom.account_suffix or "").strip()
        return problems

    def save(self, path: str | Path | None = None) -> Path:
        """原子保存：写临时文件 -> os.replace，并保留一份 ``.bak``。"""
        p = Path(path) if path else paths.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("_extra", None)
        for key, value in (self._extra or {}).items():
            data.setdefault(key, value)
        data["config_version"] = CONFIG_SCHEMA_VERSION

        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if p.exists():
            try:
                shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
            except Exception:  # noqa: BLE001
                pass
        fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
            os.replace(tmp_name, p)
        except Exception:
            try:
                os.unlink(tmp_name)
            except Exception:  # noqa: BLE001
                pass
            raise
        return p

    # ---------- 便捷方法 ----------

    def targets(self) -> list[CheckTarget]:
        out: list[CheckTarget] = []
        for item in self.check_urls:
            if isinstance(item, str):
                out.append(CheckTarget(url=item))
                continue
            try:
                out.append(
                    CheckTarget(
                        url=str(item.get("url", "")),
                        expect_text=str(item.get("expect_text", "") or ""),
                        expect_status=int(item.get("expect_status", 0) or 0),
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        return [t for t in out if t.url]

    def is_configured(self) -> bool:
        return bool(self.username) and bool(self.password_enc)

    def has_password(self) -> bool:
        """配置里是否有一个**本机解得开**的密码（不解密就无法判断，交给调用方）。"""
        return bool(self.password_enc)

    def tls_verify_for(self, url: str) -> bool:
        """这个地址要不要验证 TLS 证书。

        优先级：全局 insecure_tls > tls_insecure_hosts 命中 > 默认验证。
        """
        if self.insecure_tls:
            return False
        host = ""
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except Exception:  # noqa: BLE001
            return True
        for item in self.tls_insecure_hosts:
            if item and (item.lower() == host or item.lower() == (host + ":" + str(_port_of(url)))):
                return False
        return True

    def redacted(self) -> dict:
        """给日志/诊断用的配置快照（密码密文也只显示前缀）。"""
        data = asdict(self)
        data.pop("_extra", None)
        enc = str(data.get("password_enc") or "")
        data["password_enc"] = (enc[:8] + "…" if enc else "")
        data["username"] = redact_text(self.username)
        for key in ("notify_webhook",):
            if data.get(key):
                data[key] = "（已设置）"
        return data

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("_extra", None)
        for key, value in (self._extra or {}).items():
            data.setdefault(key, value)
        return json.dumps(data, ensure_ascii=False, indent=2)


@dataclass
class LoadReport:
    """配置加载过程的自描述，用于日志和 GUI 提示。"""

    path: str = ""
    existed: bool = False
    from_version: int = 0
    migrated: bool = False
    backup: str = ""
    problems: list = field(default_factory=list)

    def messages(self) -> list:
        out = []
        if self.migrated:
            out.append(
                f"已把旧版配置升级到 v{CONFIG_SCHEMA_VERSION}"
                + (f"（原文件备份：{self.backup}）" if self.backup else "")
            )
        for name, message in self.problems:
            out.append(f"配置项 {name}：{message}")
        return out


# ---------------------------------------------------------------- 内部工具

def _field_default(f) -> object:
    """取 dataclass 字段的默认值（兼容 default 与 default_factory）。"""
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[misc]
        try:
            return f.default_factory()  # type: ignore[misc]
        except Exception:  # noqa: BLE001
            return None
    return None


def _coerce(default, value):
    """按默认值的类型做一次温和转换；失败则回落默认值。"""
    if value is None:
        return default if not callable(default) else default()
    try:
        if isinstance(default, bool):
            return _to_bool(value, default=default)
        if isinstance(default, int) and not isinstance(default, bool):
            return int(value)
        if isinstance(default, str):
            return str(value)
    except Exception:  # noqa: BLE001
        return default
    return value


def _merge_sub(cls, value):
    sub = cls()
    if isinstance(value, dict):
        for key, item in value.items():
            if hasattr(sub, key):
                setattr(sub, key, item)
    return sub


def _to_bool(value, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "y", "on", "是", "开"):
            return True
        if text in ("0", "false", "no", "n", "off", "否", "关", ""):
            return False
    return default


def _int_in_range(value, low: int, high: int, default: int, name: str, note) -> int:
    try:
        number = int(value)
    except Exception:  # noqa: BLE001
        note(name, f"不是整数（{value!r}），已用默认值 {default}")
        return default
    if number < low or number > high:
        clamped = max(low, min(high, number))
        note(name, f"超出允许范围 {low}~{high}，已改为 {clamped}")
        return clamped
    return number


def _as_str_list(value) -> list:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    return []


def _parse_check_urls(value) -> list:
    if not isinstance(value, list):
        return []
    parsed = []
    for item in value:
        if isinstance(item, str):
            parsed.append({"url": item})
        elif isinstance(item, dict) and item.get("url"):
            try:
                status = int(item.get("expect_status", 0) or 0)
            except Exception:  # noqa: BLE001
                status = 0
            parsed.append(
                {
                    "url": str(item["url"]),
                    "expect_text": str(item.get("expect_text", "") or ""),
                    "expect_status": status,
                }
            )
    return parsed


def _port_of(url: str) -> int:
    try:
        return urllib.parse.urlsplit(url).port or 0
    except Exception:  # noqa: BLE001
        return 0


MIGRATION_BACKUP_LIMIT = 5


def _backup_config(p: Path) -> str:
    """迁移前把原配置整份留档。

    两条纪律（都是实测踩出来的）：

    * **内容相同就复用旧备份**：旧配置没有版本号时，每次启动都会被判定为
      "需要迁移"；若每次都写一份新备份，``backup/`` 会无限膨胀（实测启动 3 次
      就留下 3 份一模一样的 ``config.v1.*.json``）。这里按内容哈希去重。
    * **只保留最近若干份**，避免长期运行把磁盘占满。
    """
    try:
        raw = p.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        target_dir = paths.backup_dir()
        target_dir.mkdir(parents=True, exist_ok=True)

        existing = sorted(
            target_dir.glob("config.v1.*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for item in existing:
            try:
                if hashlib.sha256(item.read_bytes()).hexdigest() == digest:
                    return str(item)  # 已有完全相同的备份，直接复用
            except Exception:  # noqa: BLE001
                continue

        stamp = time.strftime("%Y%m%d-%H%M%S")
        saved = target_dir / f"config.v1.{stamp}.json"
        suffix = 1
        while saved.exists():  # 同一秒内多次迁移时避免互相覆盖
            saved = target_dir / f"config.v1.{stamp}-{suffix}.json"
            suffix += 1
        saved.write_bytes(raw)

        for stale in existing[max(MIGRATION_BACKUP_LIMIT - 1, 0):]:
            try:
                stale.unlink()
            except Exception:  # noqa: BLE001
                pass
        return str(saved)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 运行状态

def load_state() -> dict:
    """读取运行状态（记忆上次成功的认证策略等）。"""
    try:
        path = paths.state_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        path = paths.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def load_heartbeat() -> dict:
    """后台守护的实时心跳（可能为空）。"""
    try:
        path = paths.heartbeat_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_heartbeat(data: dict) -> None:
    try:
        path = paths.heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data or {})
        payload["updated_at"] = int(time.time())
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
