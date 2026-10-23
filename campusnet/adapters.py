"""认证适配器的统一结果结构与适配列表。

这个模块**不实现**具体协议（Dr.COM 在 :mod:`campusnet.drcom`，通用表单在
:mod:`campusnet.authenticator`），它只定义两件所有适配器都必须遵守的东西：

1. :class:`AuthOutcome` —— 结构化结果。调用方（守护进程、GUI）只认这一种结构，
   不再各自猜"返回的两个布尔是什么含义"。
2. :class:`AdapterInfo` —— "这套认证方式适不适用、为什么不用"，让界面和日志能
   明确告诉用户"我用了哪种方式"，而不是"认证失败"四个字。

为什么不做成完整的策略类层次：现有 Dr.COM 与通用表单的实现已经稳定且互相
耦合（都要复用同一个 HTTP 会话和"真实联网"验证器），强行拆成独立类会放大
回归风险。按任务书要求"先理解、再改、不许为了整洁重写"，这里只统一**对外接口**。

``status`` 取值见模块常量，覆盖任务书要求区分的七类情况。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------- 状态常量

STATUS_SUCCESS = "success"                     # 认证成功且联网验证通过
STATUS_ALREADY_ONLINE = "already-online"       # 认证页/服务端说已在线，且校验通过
STATUS_BAD_CREDENTIALS = "bad-credentials"     # 账号或密码不对（重试无意义）
STATUS_CAPTCHA_REQUIRED = "captcha-required"   # 需要验证码（必须人工）
STATUS_MANUAL_REQUIRED = "manual-required"     # 其它必须人工的步骤
STATUS_NETWORK_ERROR = "network-error"         # 网络层问题（可重试）
STATUS_SERVER_ERROR = "server-error"           # 认证服务端异常（可重试）
STATUS_UNSUPPORTED = "unsupported"             # 找不到可用的认证方式
STATUS_REFUSED_TARGET = "refused-target"       # 目标地址未确认，拒绝提交凭据
STATUS_VERIFY_FAILED = "verify-failed"         # 服务端说成功，但外网仍不通

SUCCESS_STATUSES = frozenset({STATUS_SUCCESS, STATUS_ALREADY_ONLINE})

# 这些状态表示"继续重试没有意义，等人处理"
FATAL_STATUSES = frozenset({STATUS_BAD_CREDENTIALS, STATUS_CAPTCHA_REQUIRED, STATUS_REFUSED_TARGET})

_STATUS_TEXT = {
    STATUS_SUCCESS: "认证成功",
    STATUS_ALREADY_ONLINE: "已在线，无需重复认证",
    STATUS_BAD_CREDENTIALS: "账号或密码不正确",
    STATUS_CAPTCHA_REQUIRED: "认证页要求验证码，需要人工完成",
    STATUS_MANUAL_REQUIRED: "认证需要人工确认",
    STATUS_NETWORK_ERROR: "网络层出错",
    STATUS_SERVER_ERROR: "认证服务端返回异常",
    STATUS_UNSUPPORTED: "未找到可用的认证方式",
    STATUS_REFUSED_TARGET: "认证接口地址未确认，已停止提交",
    STATUS_VERIFY_FAILED: "服务端报告成功，但网络仍不可用",
}


def status_text(status: str) -> str:
    return _STATUS_TEXT.get(status, status or "未知状态")


@dataclass
class AuthOutcome:
    """一次认证尝试的结构化结果。

    ``ok`` 是真正的判据（同时要求认证语义成功 + 真实联网验证通过）；
    ``status`` 说明"为什么"，``needs_user`` 告诉 GUI 是否要人介入。
    """

    ok: bool = False
    status: str = STATUS_SERVER_ERROR
    reason: str = ""            # 给人看的一句话
    strategy: str = ""          # 具体用过的策略名（drcom:plain-yau / form:标准提交 …）
    adapter: str = ""           # 哪一类适配器（custom / drcom / form / none）
    attempts: int = 0
    bad_credentials: bool = False
    needs_user: bool = False
    retryable: bool = True
    portal_url: str = ""
    dump_path: str = ""
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status in SUCCESS_STATUSES:
            self.ok = True
        if self.bad_credentials:
            self.status = STATUS_BAD_CREDENTIALS
        if self.status in FATAL_STATUSES:
            self.retryable = False
        if self.status == STATUS_CAPTCHA_REQUIRED:
            self.needs_user = True
        if self.status == STATUS_MANUAL_REQUIRED:
            self.needs_user = True
        if self.status == STATUS_REFUSED_TARGET:
            self.needs_user = True
        if not self.reason:
            self.reason = status_text(self.status)

    def __bool__(self) -> bool:
        return self.ok

    def describe(self) -> str:
        """``认证成功（drcom:plain-yau）`` 这种可直接写进日志/界面的短句。"""
        text = self.reason or status_text(self.status)
        if self.strategy:
            return f"{text}（{self.strategy}）"
        return text

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "strategy": self.strategy,
            "adapter": self.adapter,
            "attempts": self.attempts,
            "bad_credentials": self.bad_credentials,
            "needs_user": self.needs_user,
            "retryable": self.retryable,
            "portal_url": self.portal_url,
        }


# ---------------------------------------------------------------- 适配器清单

Verifier = Callable[[], bool]


@dataclass
class AdapterInfo:
    """一套候选认证方式的描述（能不能用 + 为什么）。"""

    name: str
    label: str
    applicable: bool = False
    reason: str = ""

    def __str__(self) -> str:  # pragma: no cover - 日志用
        state = "可用" if self.applicable else f"不适用（{self.reason}）"
        return f"{self.label}[{self.name}]: {state}"


def adapter_plan(cfg, *, portal_url: str = "", page=None, looks_like_drcom=None) -> list:
    """按优先级列出候选认证方式及其适用性（供日志与界面展示）。

    顺序与 :meth:`campusnet.authenticator.PortalAuthenticator.login` 的执行顺序一致：
    自定义接口 → Dr.COM / 哆点 → 通用 HTML 表单。
    """
    plan: list = []

    custom = getattr(cfg, "custom", None)
    custom_ok = bool(custom and custom.enabled and custom.url)
    plan.append(
        AdapterInfo(
            name="custom",
            label="自定义认证接口",
            applicable=custom_ok,
            reason="" if custom_ok else "未启用或未填写地址",
        )
    )

    drcom_cfg = getattr(cfg, "drcom", None)
    drcom_enabled = bool(drcom_cfg and drcom_cfg.enabled)
    detected = False
    if drcom_enabled:
        if looks_like_drcom is not None:
            try:
                detected = bool(looks_like_drcom(portal_url, page))
            except Exception:  # noqa: BLE001
                detected = False
        else:
            detected = "eportal" in (portal_url or "").lower()
    plan.append(
        AdapterInfo(
            name="drcom",
            label="Dr.COM / 哆点 ePortal",
            applicable=drcom_enabled and detected,
            reason=(
                "配置里已关闭"
                if not drcom_enabled
                else ("门户未识别为 Dr.COM" if not detected else "")
            ),
        )
    )

    form_ok = page is not None and bool(getattr(page, "forms", None) or getattr(page, "loose_inputs", None))
    plan.append(
        AdapterInfo(
            name="form",
            label="通用 HTML 表单",
            applicable=form_ok,
            reason="" if form_ok else "页面里没有可识别的表单",
        )
    )
    return plan
