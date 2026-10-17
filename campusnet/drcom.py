"""Dr.COM（城市热点 / 哆点）ePortal 认证适配器 —— v2：按门户自己的 JS 复刻。

## 为什么要有 v2

v1 是"猜参数"：把几套网上流传的写法依次试一遍。实测全部失败，服务端回
`认证出现异常！` / `当前页面已超时,请重新刷新页面`。

v2 改为**以门户自己的 JS 为准**（已抓取 `a41.js` / `a40.js` / `a59.js` 到
`logs/portal_assets/` 并逐行核对），关键结论：

1. `page.login_method != 0` 时，`util._jsonp` 会把**整个登录参数 JSON 用 AES 加密**
   后塞进单个 `params` 字段（a41.js:782-786）：

       params.data = { 'params': util.aes_en(JSON.stringify(params.data)) }

       aes_en: CryptoJS.AES.encrypt(str, CryptoJS.enc.Utf8.parse('5c1d5ad4dea0e8dd'),
                                    {mode: ECB, padding: Pkcs7}).toString()

   —— 所以裸发明文参数（尤其 `DDDDD`/`upass`）必然被判非法。这就是 v1 失败的根因。

2. 请求是 **GET + JSONP**，参数顺序 callback 在最前，之后是 params / jsVersion，
   末尾追加 `lang`（a41.js:789-815、826-843）：

       GET <portal_api>login?callback=dr<N>&params=<AES密文>&jsVersion=4.2.1&lang=zh

   注意 `callback` 和 `jsVersion` 是在**加密之后**才加上的，不进密文。

3. 账号前缀由服务端配置决定（a41.js:185 `account_prefix`、a40.js:119-121）：
   `account_prefix==1` 时 PC 端用 `,0,` 前缀、移动端用 `,1,`；为 0 则不加前缀。

4. 登录字段（a40.js:226 `login.login_portal`）：
   `login_method / user_account / user_password / wlan_user_ip / wlan_user_ipv6 /
    wlan_user_mac / wlan_ac_ip / wlan_ac_name / jsVersion`

5. 服务端返回语义（a40.js:157 等处）：
   `result==1|'ok'|ret_code==2` → 成功；`ret_code==1` → 账号密码错误。

实测对照（假账号，见 tools/probe_protocol.py）：
    AES(params)          -> 当前页面已超时（参数被正确解密解析，不是"认证出现异常"）
    明文 user_account    -> 当前页面已超时（服务端也兼容）
    明文 DDDDD/upass     -> 认证出现异常！（格式非法 —— v1 就是这么失败的）

所以 v2 以 **AES 浏览器同款** 为首选策略，明文 `user_account` 作兜底，
`DDDDD` 只在 `login_method == 0` 的老式部署上才用。
"""

from __future__ import annotations

import base64
import json
import random
import re
import socket
import urllib.parse
from dataclasses import dataclass, field

from . import aes
from .config import Config
from .http import HttpClient
from .portal import PortalPage

JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.S)

# 服务端明确告诉你"参数不对/页面会话不对"，换下一套
PARAM_ERROR_HINTS = ("认证出现异常", "参数错误", "非法请求", "非法", "请重新刷新页面", "已超时", "超时")
# 服务端明确告诉你"账号密码不对"，别白试了（避免账号被撞锁）
CREDENTIAL_HINTS = (
    "密码错误", "账号或密码", "用户名或密码", "用户不存在", "账号不存在", "账号密码错误",
    "停机", "余额不足", "已欠费", "未开通", "invalid", "无此用户", "已被锁定", "认证失败",
)
# 服务端告诉你"这个 IP 已经在线了" —— 这其实是成功（可能只是探测误判）
ALREADY_ONLINE_HINTS = ("已经在线", "已在线", "当前在线", "无需重复登录", "已经登录")

DEFAULT_JS_VERSION = "4.2.1"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _qs(fields: dict) -> str:
    """按 encodeURIComponent 的语义编码查询串（base64 里的 / + = 都要转义）。"""
    return "&".join(
        f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in fields.items()
    )


def ip_to_int(ip: str) -> str:
    try:
        parts = [int(x) for x in str(ip).split(".")]
        if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
            return ""
        value = 0
        for p in parts:
            value = value * 256 + p
        return str(value)
    except Exception:  # noqa: BLE001
        return ""


def local_ip_for(host: str) -> str:
    """本机到门户的出口 IP。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect((host, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return ""


def parse_jsonp(text: str) -> dict:
    if not text:
        return {}
    m = JSONP_RE.match(text.strip())
    body = m.group(1) if m else text.strip()
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return {"_raw": text.strip()[:300]}
    if isinstance(obj, dict):
        return obj
    return {"_raw": str(obj)[:300]}


@dataclass
class DrcomContext:
    host: str = ""
    port: int = 801
    account: str = ""
    wlan_user_ip: str = ""
    wlan_ac_ip: str = ""
    wlan_ac_name: str = ""
    wlan_user_mac: str = "000000000000"
    suffix: str = ""
    query: dict = field(default_factory=dict)
    # ---- 由门户配置接口（page/loadConfig）得到 ----
    login_method: int = 1
    account_prefix: int = 1  # 1 = 加 ,0, 前缀（PC）
    server_suffix: str = ""
    js_version: str = DEFAULT_JS_VERSION
    terminal_type: int = 1  # 1 = PC
    config_ok: bool = False
    force_login_method: bool = False  # 配置文件里写死了 login_method
    rcn: str = ""  # loadConfig 返回的会话 nonce，登录必须原样带上

    @property
    def api(self) -> str:
        """http://<host>:<port>/eportal/portal/"""
        return f"http://{self.host}:{self.port}/eportal/portal/"

    @property
    def api_login(self) -> str:
        return self.api + "login"

    @property
    def api_ac(self) -> str:
        return f"http://{self.host}:{self.port}/eportal/"

    def final_account(self) -> str:
        """账号 + 运营商后缀（不加 ,0, 前缀）。"""
        acct = self.account.strip()
        suffix = (self.suffix or self.server_suffix or "").strip()
        if suffix and "@" not in acct:
            acct = acct + (suffix if suffix.startswith("@") else "@" + suffix)
        return acct

    def prefixed_account(self) -> str:
        """浏览器实际提交的 user_account（含 ,0, 前缀）。"""
        acct = self.final_account()
        if not self.account_prefix:
            return acct
        return (",1," if self.terminal_type == 2 else ",0,") + acct


DRCOM_MARKERS = ("dr.comwebloginid", "eportal", "哆点", "dddd", "authuserfield", "0mkkey")


def looks_like_drcom(portal_url: str, page: PortalPage | None = None) -> bool:
    if "eportal" in (portal_url or "").lower() or "/drcom/" in (portal_url or "").lower():
        return True
    if page is not None:
        blob = (page.html or "").lower()
        return any(m in blob for m in DRCOM_MARKERS)
    return False


class DrcomAdapter:
    def __init__(self, client: HttpClient, cfg: Config, logger):
        self.client = client
        self.cfg = cfg
        self.log = logger
        self.attempts_used = 0
        self.last_ctx: DrcomContext | None = None
        self._forced_login_method: int = 1

    # ------------------------------------------------------------ 上下文

    def _load_portal_config(self, ctx: DrcomContext) -> None:
        """调 page/loadConfig 拿 login_method / account_prefix / account_suffix。

        这一步是浏览器加载门户页后的第一个请求，未加密（此时 page.login_method 还是默认 0）。
        """
        fields = {
            "callback": "dr1003",
            "program_index": "",
            "wlan_vlan_id": "1",
            "wlan_user_ip": _b64(ctx.wlan_user_ip or ""),
            "wlan_user_ipv6": _b64(""),
            "wlan_user_ssid": "",
            "wlan_user_areaid": "",
            "wlan_ac_ip": _b64(ctx.wlan_ac_ip or ""),
            "wlan_ap_mac": "000000000000",
            "gw_id": "",
            "jsVersion": ctx.js_version,
            "lang": "zh",
        }
        url = ctx.api + "page/loadConfig?" + _qs(fields)
        try:
            resp = self.client.get(url, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("loadConfig 请求失败（用默认值继续）：%s", exc)
            return

        data = parse_jsonp(resp.text)
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        if not inner:
            self.log.debug("loadConfig 未返回配置数据：%s", str(data)[:160])
            return

        def as_int(key: str, default: int) -> int:
            try:
                return int(str(inner.get(key, default)).strip() or default)
            except Exception:  # noqa: BLE001
                return default

        ctx.login_method = as_int("login_method", ctx.login_method)
        if ctx.force_login_method:
            ctx.login_method = self._forced_login_method
            self.log.info("门户配置：login_method 被配置强制为 %s（服务端值 %s）",
                          ctx.login_method, inner.get("login_method"))
        ctx.account_prefix = as_int("account_prefix", ctx.account_prefix)
        ctx.server_suffix = str(inner.get("account_suffix") or "").strip()
        ctx.rcn = str(inner.get("rcn") or "").strip()
        ctx.config_ok = True
        self.log.info(
            "门户配置：login_method=%s account_prefix=%s 服务端后缀=%r 方案=%s",
            ctx.login_method,
            ctx.account_prefix,
            ctx.server_suffix or "(空)",
            inner.get("program_index") or "-",
        )

    def build_context(self, portal_url: str, page: PortalPage | None = None) -> DrcomContext:
        dc = self.cfg.drcom
        parsed = urllib.parse.urlsplit(portal_url or "")
        host = dc.host or parsed.hostname or ""
        port = dc.port or 801
        if parsed.port:
            port = dc.port if dc.host and dc.host != parsed.hostname else parsed.port

        forced = int(getattr(dc, "login_method", -1) if getattr(dc, "login_method", -1) is not None else -1)
        ctx = DrcomContext(
            host=host,
            port=int(port),
            account="",
            wlan_user_ip=dc.wlan_user_ip or "",
            wlan_ac_ip=dc.wlan_ac_ip or "",
            wlan_ac_name=dc.wlan_ac_name or "",
            wlan_user_mac=dc.wlan_user_mac or "000000000000",
            suffix=dc.account_suffix or "",
            js_version=getattr(dc, "js_version", "") or DEFAULT_JS_VERSION,
            terminal_type=int(getattr(dc, "terminal_type", 1) or 1),
            login_method=forced if forced >= 0 else 1,
            force_login_method=forced >= 0,
        )
        self._forced_login_method = ctx.login_method

        # 1) 先看门户页内嵌 JS —— 这是服务端**为本机现场生成**的，最权威。
        #    实测 a79.htm 里有 v46ip='<本机IP>'（v1 漏了这个，只认 v4ip，
        #    结果用了跳转 URL 里的过期 IP，服务端自然回"当前页面已超时"）。
        page_ip = ""
        page_mac = ""
        if page is not None:
            html = page.html or ""
            for pattern in (
                r"v46ip\s*=\s*['\"]([\d.]+)['\"]",
                r"v4ip\s*=\s*['\"]([\d.]+)['\"]",
                r"\bss5\s*=\s*[\"']([\d.]+)[\"']",
            ):
                m = re.search(pattern, html)
                if m and m.group(1) not in ("0.0.0.0", "000.000.000.000.", ""):
                    page_ip = m.group(1)
                    break
            m = re.search(r"\bss4\s*=\s*[\"']([0-9a-fA-F]{12})[\"']", html)
            if m and m.group(1) != "000000000000":
                page_mac = m.group(1)
            m = re.search(r"authloginport\s*=\s*(\d+)", html)
            if m and not self.cfg.drcom.port:
                try:
                    ctx.port = int(m.group(1))
                except Exception:  # noqa: BLE001
                    pass
            if not ctx.suffix:
                m = re.search(r"account_suffix\s*[:=]\s*['\"](@?\w+)['\"]", html)
                if m:
                    ctx.suffix = m.group(1)

        # 2) 跳转 URL 的查询串（未认证时 Dr.COM 会把 AC 参数带出来）
        q = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v}
        ctx.query = q
        query_ip = ""
        for key in ("wlanuserip", "wlan_user_ip", "userip", "user-ip", "client_ip", "ip", "uip", "station_ip"):
            if q.get(key):
                query_ip = q[key]
                break
        if page_ip and query_ip and page_ip != query_ip:
            self.log.warning(
                "跳转 URL 里的 wlanuserip=%s 与门户页里的真实 IP=%s 不一致，以门户页为准"
                "（URL 多半是上次缓存/配置残留）",
                query_ip, page_ip,
            )

        for key in ("wlanacip", "acip", "switchip", "nasip", "nas-ip"):
            if q.get(key) and not ctx.wlan_ac_ip:
                ctx.wlan_ac_ip = q[key]
                break
        for key in ("wlanacname", "acname", "sysname", "nasname", "nas-name"):
            if q.get(key) and not ctx.wlan_ac_name:
                ctx.wlan_ac_name = q[key]
                break
        for key in ("mac", "wlanusermac", "wlan_user_mac"):
            if q.get(key) and ctx.wlan_user_mac == "000000000000":
                ctx.wlan_user_mac = q[key]
                break

        # 3) IP 取值优先级：门户页现场值 > 跳转URL > 配置 > 本机出口IP
        #    （配置里手填的 wlan_user_ip 放在最后，避免"写死的旧 IP"污染现场数据）
        ctx.wlan_user_ip = page_ip or query_ip or (dc.wlan_user_ip or "")

        # MAC 统一成 12 位大写十六进制（浏览器就是这么提交的）
        ctx.wlan_user_mac = re.sub(r"[^0-9a-fA-F]", "", ctx.wlan_user_mac or "").upper() or "000000000000"
        if ctx.wlan_user_mac == "000000000000" and page_mac:
            ctx.wlan_user_mac = page_mac.upper()

        # 4) 兜底：本机出口 IP
        if not ctx.wlan_user_ip and ctx.host:
            ctx.wlan_user_ip = local_ip_for(ctx.host)

        # 4) 向门户要权威配置（login_method / account_prefix）
        if ctx.host:
            self._load_portal_config(ctx)
            # 5) 内核状态接口：激活"页面会话" + 拿服务端登记的权威 IP/MAC
            self.chkstatus(ctx)

        self.last_ctx = ctx
        return ctx

    # ------------------------------------------------------------ 策略集

    def _login_t(self, ctx: DrcomContext) -> str:
        """门户里的 custom.login_t：PC 端为 '0'（装了安全控件才是 '1'），手机端 '4'。"""
        return "4" if ctx.terminal_type == 2 else "0"

    def _portal_login_data(self, ctx: DrcomContext, password: str) -> dict:
        """浏览器 login.login_portal + login.portal_login 提交的完整字段（a40.js:226 与 :136）。

        注意后半段那 7 个字段是 portal_login 统一补上的，缺了它们服务端也可能不认。
        """
        data = {
            "login_method": ctx.login_method,
            "user_account": ctx.prefixed_account(),
            "user_password": password,
            "wlan_user_ip": ctx.wlan_user_ip,
            "wlan_user_ipv6": "",
            "wlan_user_mac": (ctx.wlan_user_mac or "000000000000").upper(),
            "wlan_ac_ip": ctx.wlan_ac_ip,
            "wlan_ac_name": ctx.wlan_ac_name,
            "jsVersion": ctx.js_version,
            # ↓ login.portal_login 补的
            "login_t": self._login_t(ctx),
            "js_status": "0",
            "is_page": "1",
            "is_page_new": str(random.randint(500, 10499)),
            "terminal_type": str(ctx.terminal_type),
            "lang": "zh",
            "rcn": ctx.rcn,
        }
        return data

    def query_page_set(self, ctx: DrcomContext) -> tuple[bool, str]:
        """复刻门户的 `duodian/queryPageSet` —— **登录前的必要一步**。

        门户 a40.js 的 login.portal_login 里，只要 custom.login_t != '1'，就必定先调它，
        并且真正的登录请求是写在它的 complete 回调里发的。少了这一步，服务端没有
        "当前页面"会话，登录接口只会回「当前页面已超时,请重新刷新页面」。

        请求形态（注意是**双层加密**）：
            外层 params = AES(JSON)                     <- util._jsonp 干的
            JSON 里每个值  = AES(字段原文)               <- _util.aes_en 干的
        """
        inner = {
            "account": aes.encrypt_b64(ctx.final_account()),
            "wlan_user_ip": aes.encrypt_b64(ctx.wlan_user_ip or ""),
            "wlan_user_mac": aes.encrypt_b64((ctx.wlan_user_mac or "000000000000").upper()),
            "user_agent": aes.encrypt_b64((self.cfg.user_agent or "").lower()),
            "login_t": aes.encrypt_b64(self._login_t(ctx)),
        }
        token = aes.encrypt_b64(json.dumps(inner, separators=(",", ":"), ensure_ascii=False))
        url = ctx.api + "duodian/queryPageSet?" + _qs({
            "callback": f"dr{random.randint(100, 9999)}",
            "params": token,
            "jsVersion": ctx.js_version,
            "lang": "zh",
        })
        try:
            resp = self.client.get(url, follow_redirects=False,
                                   headers={"Referer": f"http://{ctx.host}/"})
        except Exception as exc:  # noqa: BLE001
            self.log.debug("queryPageSet 请求失败：%s", exc)
            return False, f"请求异常 {exc}"

        data = parse_jsonp(resp.text)
        msg = str(data.get("msg", "") or "")
        ok = data.get("result") in (1, "1", "ok")
        self.log.info("duodian/queryPageSet -> HTTP %s result=%r msg=%r",
                      resp.status, data.get("result"), msg)
        return ok, msg or f"HTTP {resp.status}"

    def chkstatus(self, ctx: DrcomContext) -> tuple[bool, str]:
        """调内核状态接口 `/drcom/chkstatus`（端口 80）。

        这是浏览器加载门户后必走的一步（a41.js checkStatus），也是多个可用实现
        （延安大学 YAU / 深大 net-login）登录前的第一步。它做两件事：
          1. 在服务端把本机的"页面会话"激活（少了它登录接口会回「当前页面已超时」）
          2. 返回服务端登记的权威本机信息：v4ip/v46ip/ss5 = IP，ss4 = MAC，result = 是否在线
        """
        url = f"http://{ctx.host}/drcom/chkstatus?" + _qs({
            "callback": "_ck",
            "jsVersion": "4.X",
            "v": str(random.randint(500, 10500)),
            "lang": "zh",
        })
        try:
            resp = self.client.get(url, follow_redirects=False,
                                   headers={"Referer": f"http://{ctx.host}/"})
        except Exception as exc:  # noqa: BLE001
            self.log.debug("chkstatus 请求失败：%s", exc)
            return False, f"请求异常 {exc}"

        data = parse_jsonp(resp.text)
        ip = str(data.get("v4ip") or data.get("v46ip") or data.get("ss5") or "")
        mac = str(data.get("ss4") or "").replace("-", "").replace(":", "").upper() or "000000000000"
        if ip and ip not in ("0.0.0.0", "000.000.000.000."):
            ctx.wlan_user_ip = ip
        ctx.wlan_user_mac = mac
        online = data.get("result") in (1, "1")
        self.log.info(
            "chkstatus -> 在线=%s IP=%s MAC=%s uid=%s",
            online, ctx.wlan_user_ip, mac, data.get("uid") or "-",
        )
        return online, str(data.get("msg", "") or "")

    def _plain_login_data(self, ctx: DrcomContext, password: str,
                          ac_ip: str = "", ac_name: str = "") -> dict:
        """明文登录参数 —— 与延安大学/深大/「零依赖 campusnet」三个可用实现一致。

        关键点：wlan_ac_ip / wlan_ac_name 留空；mac 用 chkstatus 给的（常为全零）；
        lang=zh-cn；带 v 缓存穿透随机数。
        """
        return {
            "callback": f"dr{random.randint(100, 9999)}",
            "login_method": "1",
            "user_account": ctx.prefixed_account(),
            "user_password": password,
            "wlan_user_ip": ctx.wlan_user_ip,
            "wlan_user_ipv6": "",
            "wlan_user_mac": (ctx.wlan_user_mac or "000000000000").upper(),
            "wlan_ac_ip": ac_ip,
            "wlan_ac_name": ac_name,
            "jsVersion": ctx.js_version,
            "terminal_type": str(ctx.terminal_type),
            "lang": "zh-cn",
            "v": str(random.randint(500, 10500)),
            "rcn": ctx.rcn,
        }

    def strategies(self, ctx: DrcomContext, password: str) -> list[dict]:
        out: list[dict] = []
        headers = {"Referer": f"http://{ctx.host}/", "Origin": f"http://{ctx.host}"}

        # ---- 策略 1（首选）：明文，与三个可用实现完全一致（AC 参数留空）----
        out.append({
            "name": "drcom:plain-yau",
            "url": ctx.api_login + "?" + _qs(self._plain_login_data(ctx, password)),
            "method": "RAWGET",
            "headers": headers,
        })

        # ---- 策略 2：明文，但带上从跳转 URL 里挖到的 AC 参数 ----
        if ctx.wlan_ac_ip or ctx.wlan_ac_name:
            out.append({
                "name": "drcom:plain-with-ac",
                "url": ctx.api_login + "?" + _qs(
                    self._plain_login_data(ctx, password, ctx.wlan_ac_ip, ctx.wlan_ac_name)
                ),
                "method": "RAWGET",
                "headers": headers,
            })

        # ---- 策略 3（兜底）：浏览器同款 AES 加密（含 queryPageSet 建会话）----
        payload = json.dumps(self._portal_login_data(ctx, password), separators=(",", ":"), ensure_ascii=False)
        aes_fields = {
            "callback": f"dr{random.randint(100, 9999)}",
            "params": aes.encrypt_b64(payload),
            "jsVersion": ctx.js_version,
            "lang": "zh",
        }
        out.append({
            "name": "drcom:eportal-aes",
            "url": ctx.api_login + "?" + _qs(aes_fields),
            "method": "RAWGET",
            "headers": headers,
            "pre": lambda: self.query_page_set(ctx),
        })

        return out

    # ------------------------------------------------------------ 执行

    def attempt(
        self, ctx: DrcomContext, password: str, verify, preferred: str = ""
    ) -> tuple[bool, str, str, bool]:
        """依次尝试所有策略。

        返回 (是否成功, 说明, 策略名, 是否账号密码问题)。
        最后一个布尔为 True 时调用方应停止重试（账号密码不对，重试只会增加被锁风险）。
        """
        plans = self.strategies(ctx, password)
        if preferred:
            plans.sort(key=lambda p: 0 if p["name"] == preferred else 1)

        errors: list[str] = []
        self.attempts_used = 0
        for plan in plans:
            self.attempts_used += 1
            # 前置步骤：浏览器在发登录前必调 duodian/queryPageSet 建立"当前页面"会话
            if plan.get("pre"):
                try:
                    plan["pre"]()
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("策略 %s 的前置步骤异常（继续尝试登录）：%s", plan["name"], exc)
            try:
                if plan["method"] == "RAWGET":
                    resp = self.client.get(plan["url"], follow_redirects=False, headers=plan.get("headers"))
                elif plan["method"] == "POST":
                    resp = self.client.post_form(
                        plan["url"], plan["params"], headers=plan.get("headers"), follow_redirects=False
                    )
                else:
                    resp = self.client.get_query(plan["url"], plan["params"], follow_redirects=False)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{plan['name']}: 请求异常 {exc}")
                continue

            data = parse_jsonp(resp.text)
            result = data.get("result")
            msg = str(data.get("msg", "") or "")
            ret_code = data.get("ret_code")

            ok_flag = result in (1, "1", "ok", True)
            already = (
                any(k in msg for k in ALREADY_ONLINE_HINTS)
                or str(ret_code) == "2"
            )

            if ok_flag or already:
                if already and not ok_flag:
                    self.log.info("Dr.COM 策略 %s：服务端说本机已经在线（%s）", plan["name"], msg)
                else:
                    self.log.info("Dr.COM 策略 %s 返回成功：%s", plan["name"], msg or result)
                # 服务端说成功不算数，必须真的能上外网
                if verify():
                    return True, msg or "result=1", plan["name"], False
                errors.append(f"{plan['name']}: 服务端报成功但外网仍不通")
                self.log.warning("Dr.COM 策略 %s 服务端报成功，但联网验证未通过", plan["name"])
                continue

            if any(k in msg for k in CREDENTIAL_HINTS):
                self.log.error("Dr.COM 策略 %s 提示账号/密码问题：%s", plan["name"], msg)
                return False, f"账号或密码有问题：{msg}", plan["name"], True

            errors.append(f"{plan['name']}: {msg or ('HTTP ' + str(resp.status))}")
            self.log.debug("Dr.COM 策略 %s 未生效：%s", plan["name"], msg or resp.status)

        return False, "；".join(errors[-3:]) or "所有 Dr.COM 策略均未生效", "", False

    # ------------------------------------------------------------ 注销

    def logout(self, ctx: DrcomContext) -> tuple[bool, str]:
        """注销当前会话。

        用途：AC 侧存在"半死残留会话"时（探测到认证跳转、但门户页却显示已登录、
        登录接口一律回「当前页面已超时」），必须先注销掉旧会话才能重新认证。
        参数按 a40.js:232 `logout.logout_portal` 复刻，URL 为 portal_api + 'logout'。
        """
        data = {
            "login_method": ctx.login_method,
            "user_account": "drcom",
            "user_password": "123",
            "ac_logout": 0,
            "register_mode": 1,
            "wlan_user_ip": ctx.wlan_user_ip,
            "wlan_user_ipv6": "",
            "wlan_vlan_id": "1",
            "wlan_user_mac": (ctx.wlan_user_mac or "000000000000").upper(),
            "wlan_ac_ip": ctx.wlan_ac_ip,
            "wlan_ac_name": ctx.wlan_ac_name,
            "jsVersion": ctx.js_version,
        }

        candidates: list[tuple[str, str]] = []
        if ctx.login_method != 0:
            token = aes.encrypt_b64(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
            candidates.append((
                ctx.api + "logout?" + _qs({
                    "callback": f"dr{random.randint(100, 9999)}",
                    "params": token,
                    "jsVersion": ctx.js_version,
                    "lang": "zh",
                }),
                "logout:aes",
            ))
        candidates.append((
            ctx.api + "logout?" + _qs({
                "callback": f"dr{random.randint(100, 9999)}",
                "lang": "zh",
                **data,
            }),
            "logout:plain",
        ))

        last = "未执行"
        for url, name in candidates:
            try:
                resp = self.client.get(url, follow_redirects=False,
                                       headers={"Referer": f"http://{ctx.host}/"})
            except Exception as exc:  # noqa: BLE001
                last = f"{name}: 请求异常 {exc}"
                continue
            parsed = parse_jsonp(resp.text)
            msg = str(parsed.get("msg", "") or "")
            result = parsed.get("result")
            last = f"{name}: {msg or ('HTTP ' + str(resp.status))}"
            if result in (1, "1", "ok") or any(k in msg for k in ("注销成功", "退出成功", "成功")):
                self.log.info("已注销旧会话（%s）", last)
                return True, last
            self.log.debug("注销尝试 %s 未成功：%s", name, last)
        return False, last
