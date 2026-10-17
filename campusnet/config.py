"""配置读写。零第三方依赖，容错解析（坏字段自动回落默认值）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
LOG_DIR = PROJECT_ROOT / "logs"
STATE_PATH = PROJECT_ROOT / "state.json"

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
    host: str = ""  # 留空则从 portal_url 推导，例如 111.17.200.130
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
    # 认证页地址；留空 = 每次自动探测
    portal_url: str = ""
    username: str = ""
    password_enc: str = ""  # DPAPI 密文（base64）

    check_urls: list = field(default_factory=lambda: [dict(x) for x in DEFAULT_CHECK_URLS])

    interval_online: int = 60  # 已在线时的巡检间隔（秒）
    interval_offline: int = 8  # 掉线后的重试间隔（秒）
    interval_max: int = 120  # 退避上限
    request_timeout: int = 8

    insecure_tls: bool = True  # 校园网portal常自签证书
    use_system_proxy: bool = False  # 关掉系统代理，避免 Clash 之类挡住探测
    include_submit_button: bool = False

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

    # ---------- 读写 ----------

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        cfg = cls()
        if not p.exists():
            return cfg
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"配置文件无法解析：{p}（{exc}）") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"配置文件根节点必须是对象：{p}")
        cfg._merge(raw)
        return cfg

    def _merge(self, raw: dict) -> None:
        known = {f.name: f for f in fields(Config)}
        for key, value in raw.items():
            if key not in known:
                continue
            if key == "custom" and isinstance(value, dict):
                sub = CustomLogin()
                for k, v in value.items():
                    if hasattr(sub, k):
                        setattr(sub, k, v)
                self.custom = sub
                continue
            if key == "drcom" and isinstance(value, dict):
                sub = DrcomConfig()
                for k, v in value.items():
                    if hasattr(sub, k):
                        setattr(sub, k, v)
                self.drcom = sub
                continue
            if key == "check_urls" and isinstance(value, list):
                parsed = []
                for item in value:
                    if isinstance(item, str):
                        parsed.append({"url": item})
                    elif isinstance(item, dict) and item.get("url"):
                        parsed.append(
                            {
                                "url": str(item["url"]),
                                "expect_text": str(item.get("expect_text", "")),
                                "expect_status": int(item.get("expect_status", 0) or 0),
                            }
                        )
                if parsed:
                    self.check_urls = parsed
                continue
            setattr(self, key, value)

    def save(self, path: str | Path | None = None) -> Path:
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def load_state() -> dict:
    """读取运行状态（记忆上次成功的认证策略等）。"""
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass
