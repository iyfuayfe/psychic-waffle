"""可选的登录结果推送。

为什么需要它：按你的要求程序**不弹任何窗口**，所以登录成功/失败在屏幕上是看不见的。
代价就是"出问题了也不知道"。如果你愿意，可以填一个机器人 webhook，
让它在「认证失败」或「掉线后自动恢复」时推一条消息到你手机。

默认关闭（notify_webhook 为空即不启用）。

支持的 webhook 格式（企业微信机器人和钉钉机器人是同一套 JSON）：
    {"msgtype":"text","text":{"content":"消息内容"}}
把群机器人的 webhook 地址填进 config.json 的 notify_webhook 即可。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import Config

_TIMEOUT = 8


def _post_json(url: str, payload: dict, timeout: int = _TIMEOUT) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def notify(cfg: Config, text: str, logger=None) -> bool:
    """推送一条文本。没配置就静默跳过，绝不抛异常、绝不弹窗。"""
    url = (getattr(cfg, "notify_webhook", "") or "").strip()
    if not url:
        return False
    ok, info = _post_json(url, {"msgtype": "text", "text": {"content": f"校园网自动登录\n{text}"}})
    if logger:
        if ok:
            logger.info("已推送通知：%s", text)
        else:
            logger.warning("通知推送失败（%s）：%s", info, text)
    return ok
