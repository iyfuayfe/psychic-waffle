"""认证页解析：从真实 HTML 里抽出登录表单，并生成提交参数。

校园网 Portal 五花八门，但 90% 是"一个带密码框的 HTML form"。
这里做通用解析，外加两层逃生舱：
- pick_login_form() 打分挑出最像登录框的那个表单；
- 从 <script> 里找 JS 拼接的跳转/接口地址（Srun/深澜 之类）。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_SCRIPT_URL_RE = re.compile(
    r"""(?:url|action|href|api|loginUrl)\s*[:=]\s*['"]([^'"\s]{4,300})['"]""", re.I
)
_IP_URL_RE = re.compile(r"""['"]((?:https?:)?//[\w.\-]+(?::\d+)?/[^'"\s<>]{0,200})['"]""", re.I)

_PASSWORD_NAME_RE = re.compile(r"pass|pwd|psw|mima|secret|密码", re.I)
_USERNAME_NAME_RE = re.compile(
    r"user|account|login|uname|uid|userid|name|no\b|number|id\b|学号|账号|帐号|用户名|工号", re.I
)
_SKIP_TYPES = {"submit", "reset", "button", "file", "image"}
_SKIP_NAME_RE = re.compile(r"csrf|token|rand|checkcode|captcha|verify|vcode|yzm|验证码", re.I)


@dataclass
class FormField:
    name: str
    value: str = ""
    type: str = "text"
    tag: str = "input"

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "type": self.type, "tag": self.tag}


@dataclass
class HtmlForm:
    action: str = ""
    method: str = "get"
    fields: list = field(default_factory=list)
    form_id: str = ""
    form_name: str = ""
    index: int = 0

    def field_names(self) -> list:
        return [f.name for f in self.fields if f.name]

    def get(self, name: str) -> FormField | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "action": self.action,
            "method": self.method,
            "id": self.form_id,
            "name": self.form_name,
            "fields": [f.to_dict() for f in self.fields],
        }


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[HtmlForm] = []
        self.loose_inputs: list[FormField] = []
        self.scripts: list[str] = []
        self.title = ""
        self._cur: HtmlForm | None = None
        self._in_title = False
        self._in_script = False
        self._in_textarea = False
        self._select: dict | None = None
        self._textarea: dict | None = None

    # -- 标签 --
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            self._cur = HtmlForm(
                action=(a.get("action") or "").strip(),
                method=(a.get("method") or "get").strip().lower() or "get",
                form_id=a.get("id", ""),
                form_name=a.get("name", ""),
                index=len(self.forms),
            )
            self.forms.append(self._cur)
        elif tag == "input":
            if "disabled" in a:
                return
            fld = FormField(
                name=(a.get("name") or "").strip(),
                value=a.get("value", ""),
                type=(a.get("type") or "text").strip().lower() or "text",
            )
            if not fld.name:
                return
            if self._cur is not None:
                self._cur.fields.append(fld)
            else:
                self.loose_inputs.append(fld)
        elif tag == "select":
            self._select = {
                "name": (a.get("name") or "").strip(),
                "value": "",
                "type": "select",
                "tag": "select",
            }
        elif tag == "option" and self._select is not None:
            value = a.get("value", "") or ""
            if not self._select["value"] or "selected" in a:
                self._select["value"] = value
        elif tag == "textarea":
            self._in_textarea = True
            self._textarea = {
                "name": (a.get("name") or "").strip(),
                "value": "",
                "type": "textarea",
                "tag": "textarea",
            }
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            self._in_script = True

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None
        elif tag == "select" and self._select is not None:
            fld = FormField(
                name=self._select["name"],
                value=self._select["value"],
                type="select",
                tag="select",
            )
            if fld.name:
                if self._cur is not None:
                    self._cur.fields.append(fld)
                else:
                    self.loose_inputs.append(fld)
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            fld = FormField(
                name=self._textarea["name"],
                value=self._textarea["value"],
                type="textarea",
                tag="textarea",
            )
            if fld.name:
                if self._cur is not None:
                    self._cur.fields.append(fld)
                else:
                    self.loose_inputs.append(fld)
            self._textarea = None
            self._in_textarea = False
        elif tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_script:
            self.scripts.append(data)
        elif self._in_textarea and self._textarea is not None:
            self._textarea["value"] += data


class PortalPage:
    def __init__(self, html: str, url: str):
        self.html = html or ""
        self.url = url
        ex = _Extractor()
        try:
            ex.feed(self.html)
        except Exception:  # noqa: BLE001
            pass
        self.forms = ex.forms
        self.loose_inputs = ex.loose_inputs
        self.scripts = ex.scripts
        m = _TITLE_RE.search(self.html)
        self.title = (ex.title or (m.group(1) if m else "")).strip()[:200]

    # ---------- 选择 ----------

    def score_form(self, form: HtmlForm) -> int:
        score = 0
        for f in form.fields:
            if f.type == "password":
                score += 10
            elif f.type in ("text", "email", "tel", ""):
                score += 3
            if _PASSWORD_NAME_RE.search(f.name):
                score += 6
            if _USERNAME_NAME_RE.search(f.name):
                score += 4
        if form.action:
            score += 2
        if form.method == "post":
            score += 2
        if any(_SKIP_NAME_RE.search(f.name) for f in form.fields):
            score -= 1  # 有验证码，多半不是主登录框
        return score

    def pick_login_form(self) -> HtmlForm | None:
        candidates = [f for f in self.forms if f.fields]
        if not candidates:
            if self.loose_inputs:
                synthetic = HtmlForm(action="", method="post", fields=list(self.loose_inputs), index=-1)
                candidates = [synthetic]
            else:
                return None
        return max(candidates, key=self.score_form)

    def classify(self, form: HtmlForm) -> tuple[FormField | None, FormField | None]:
        """返回 (用户名框, 密码框)。"""
        password = None
        for f in form.fields:
            if f.type == "password":
                password = f
                break
        if password is None:
            for f in form.fields:
                if _PASSWORD_NAME_RE.search(f.name):
                    password = f
                    break

        username = None
        for f in form.fields:
            if f is password or f.type in _SKIP_TYPES or f.type == "hidden":
                continue
            if _USERNAME_NAME_RE.search(f.name):
                username = f
                break
        if username is None:
            for f in form.fields:
                if f is password or f.type in _SKIP_TYPES or f.type == "hidden":
                    continue
                username = f
                break
        return username, password

    def action_url(self, form: HtmlForm) -> str:
        raw = (form.action or "").strip()
        if not raw or raw.lower().startswith("javascript:"):
            return self.url
        return urllib.parse.urljoin(self.url, raw)

    def visible_text(self) -> str:
        """页面里**用户能看见**的文字（去掉 <script>/<style>/注释/标签）。"""
        html = _COMMENT_RE.sub(" ", self.html or "")
        html = _SCRIPT_BLOCK_RE.sub(" ", html)
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()

    def drcom_page_state(self) -> str:
        """Dr.COM / 哆点门户的页面状态。

        服务端会在页面里留标记：
            登录页 -> <!--Dr.COMWebLoginID_0.htm-->  标题「上网登录页」
            注销页 -> <!--Dr.COMWebLoginID_1.htm-->  标题「注销页」
        返回 'online' / 'login' / ''（非 Dr.COM 或无法判断）。
        """
        html = (self.html or "").lower()
        title = self.title or ""
        if "dr.comwebloginid_1.htm" in html or "注销页" in title:
            return "online"
        if "dr.comwebloginid_0.htm" in html or "上网登录页" in title or "登录页" in title:
            return "login"
        return ""

    def looks_logged_in(self, keywords: list) -> bool:
        """是否"已登录"。

        只匹配**标题 + 可见文字**，不能拿整页 HTML 匹配：
        Dr.COM 的「上网登录页」在 <script> 里写着
        `authlogoutpath='/eportal/?c=ACSetting&a=Logout&ver=1.0'`，
        用整页文本匹配会把登录页误判成"已登录"（v1 就是这么误报的）。
        """
        haystack = f"{self.title or ''} {self.visible_text()}".lower()
        for k in keywords:
            if k and str(k).lower() in haystack:
                return True
        return self.drcom_page_state() == "online"

    def bad_credential_hint(self, keywords: list) -> bool:
        text = f"{self.title or ''} {self.visible_text()}".lower()
        return any(str(k).lower() in text for k in keywords if k)

    def has_captcha(self, form: HtmlForm | None = None) -> bool:
        fields = form.fields if form else [f for fm in self.forms for f in fm.fields]
        names = " ".join(f.name for f in fields).lower()
        if re.search(r"captcha|checkcode|verify|vcode|yzm|验证码", names):
            return True
        return bool(re.search(r"验证码|captcha|checkcode", self.html, re.I))

    def script_candidates(self) -> list:
        """从 JS 里猜认证接口地址（对付 JS 拼 URL 的门户）。"""
        found: list[str] = []
        blob = "\n".join(self.scripts)
        for rx in (_SCRIPT_URL_RE, _IP_URL_RE):
            for m in rx.finditer(blob):
                cand = m.group(1).strip()
                if not cand or cand.lower().startswith(("javascript:", "data:", "#")):
                    continue
                if "://" not in cand and not cand.startswith("//"):
                    continue
                full = urllib.parse.urljoin(self.url, cand)
                if full not in found:
                    found.append(full)
        return found[:20]


def build_payload(
    form: HtmlForm | None,
    *,
    username: str,
    password: str,
    username_field: str = "",
    password_field: str = "",
    extra: dict | None = None,
    include_submit_button: bool = False,
) -> dict:
    """按表单原始字段顺序拼提交数据，保留 hidden 域。"""
    payload: dict[str, str] = {}
    for f in (form.fields if form else []):
        if f.type in _SKIP_TYPES and not (include_submit_button and f.type == "submit"):
            continue
        if f.type in ("reset", "file", "image"):
            continue
        payload[f.name] = f.value

    if username_field:
        payload[username_field] = username
    if password_field:
        payload[password_field] = password
    if extra:
        for k, v in extra.items():
            if k:
                payload[str(k)] = "" if v is None else str(v)
    return payload


def summarize(page: PortalPage) -> dict:
    form = page.pick_login_form()
    user_f, pass_f = page.classify(form) if form else (None, None)
    return {
        "url": page.url,
        "title": page.title,
        "form_count": len(page.forms),
        "has_captcha": page.has_captcha(form),
        "chosen_form": form.to_dict() if form else None,
        "username_field": user_f.name if user_f else "",
        "password_field": pass_f.name if pass_f else "",
        "submit_url": page.action_url(form) if form else "",
        "submit_method": (form.method if form else "").upper(),
        "script_candidates": page.script_candidates(),
    }


# ------------------------------------------------------------------ 门户证据
#
# 为什么需要这一节：``checker.check()`` 把**任何** 200 + HTML 的响应都当作
# "内容劫持"（见 checker.py 的 _looks_like_html），于是普通网站、运营商提示页、
# 代理错误页也会产出一个"门户地址"，还会被写进配置（page_auth._persist_portal）。
# 状态码和 content-type 只能说明"这不是我们要的内容"，**不能证明它是校园网门户**。
# 这里要求正向证据（登录表单 / 门户特征词 / Dr.COM 标记 / 网关参数），
# 证据不足时上层必须报 UNKNOWN，而不是"已发现认证门户"。

# 标题里出现这些词，页面"像"认证页
_PORTAL_TITLE_HINTS = (
    "登录", "登陆", "认证", "上网", "校园网", "注销", "eportal", "portal",
    "drcom", "哆点", "城市热点", "sign in", "login", "hotspot",
)
# 正文（可见文字）里出现这些词才更可信：全是门户/网关专用词，普通网站不会同时出现
_PORTAL_TEXT_HINTS = (
    "wlanuserip", "wlanacname", "nasip", "wlanac", "上网时长", "校园网",
    "注销", "eportal", "drcom", "哆点", "认证",
)
# Dr.COM / ePortal 跳转必备的网关参数（地址里或页面里出现就很有力）
_PORTAL_PARAM_RE = re.compile(
    r"(wlanuserip|wlanacname|wlanacip|nasip|wlan_user_mac|wlanac|wlan_ac_ip)\s*[=:]", re.I
)
# 证据分阈值：40 = "有密码框的登录表单"或"Dr.COM 标记"单独就够
_PORTAL_SCORE_THRESHOLD = 40


@dataclass
class PortalEvidence:
    """"这个页面像不像认证门户"的证据汇总。**是证据，不是猜测。**"""

    is_portal: bool = False
    score: int = 0
    reasons: list = field(default_factory=list)
    has_login_form: bool = False
    has_password_field: bool = False
    title: str = ""

    def summary(self) -> str:
        return "；".join(self.reasons) if self.reasons else "没有发现门户特征"


def portal_page_evidence(page: PortalPage | None, *, is_drcom: bool = False) -> PortalEvidence:
    """从页面本身找"这是认证门户"的正向证据。

    ``is_drcom`` 由调用方传入（``drcom.looks_like_drcom()`` 的结果）。**刻意不在
    这里 import drcom**：``drcom`` 依赖 ``portal``，反向 import 会形成循环。
    """
    ev = PortalEvidence()
    if page is None:
        return ev
    ev.title = (page.title or "").strip()
    html = page.html or ""

    # 1) 厂商标记（最强）：既有的 Dr.COM / 哆点 ePortal 识别结果，或页面自带标记
    try:
        drcom_state = page.drcom_page_state()
    except Exception:  # noqa: BLE001
        drcom_state = ""
    if is_drcom or drcom_state:
        ev.score += 50
        ev.reasons.append(
            f"带 Dr.COM / 哆点门户标记（{drcom_state}）" if drcom_state else "被识别为 Dr.COM / 哆点门户"
        )

    # 2) 登录表单（有密码框最强）
    form = None
    try:
        form = page.pick_login_form()
    except Exception:  # noqa: BLE001
        form = None
    if form is not None:
        ev.has_login_form = True
        password = None
        try:
            _user, password = page.classify(form)
        except Exception:  # noqa: BLE001
            password = None
        if password is not None:
            ev.has_password_field = True
            ev.score += 40
            ev.reasons.append("有带密码框的登录表单")
        else:
            ev.score += 15
            ev.reasons.append("有表单，但没找到密码框")

    # 3) 标题 / 可见文字特征词
    if ev.title and any(hint in ev.title.lower() for hint in _PORTAL_TITLE_HINTS):
        ev.score += 20
        ev.reasons.append(f"标题像认证页（{ev.title[:40]}）")
    try:
        visible = page.visible_text().lower()
    except Exception:  # noqa: BLE001
        visible = ""
    if visible and any(hint in visible for hint in _PORTAL_TEXT_HINTS):
        ev.score += 10
        ev.reasons.append("页面文字里出现认证 / 门户专用词")

    # 4) 网关参数：Dr.COM / ePortal 跳转必备
    if _PORTAL_PARAM_RE.search(html) or _PORTAL_PARAM_RE.search(page.url or ""):
        ev.score += 25
        ev.reasons.append("带校园网网关参数（wlanuserip / wlanacname / nasip）")

    ev.is_portal = ev.score >= _PORTAL_SCORE_THRESHOLD
    if not ev.is_portal:
        ev.reasons.append(f"门户特征分 {ev.score} 低于阈值 {_PORTAL_SCORE_THRESHOLD}")
    return ev
