"""注入到认证页里的填写脚本（在浏览器页面上下文中执行）。

思路参考了开源项目 CampusConnect（MIT，github.com/Ye9137/CampusConnect）的
`Windows/source/PortalTemplate.js` 与 `PortalAgreements.js`：同样是"在页面里找输入框、
填值、勾协议、选运营商、点登录，然后把状态字符串回给宿主程序"。这里按我们的配置结构
重写并精简（去掉了它针对某一所学校 init 流程的加速逻辑和不支持的自定义下拉菜单），
保留通用启发式 + Dr.COM / 锐捷 ePortal 的已知选择器。

返回的状态字符串（宿主按它决定下一步）：

    wrong-origin            页面 origin 和保存的认证地址不一致，拒绝填写
    authenticated           页面自己表示已登录成功
    authentication-rejected 页面明确表示登录失败
    waiting-ready           页面还没加载完 / 认证参数还没就绪
    missing-fields          没找到可用的账号或密码输入框
    manual-required         页面要验证码等必须人工填的内容
    agreement-required      必选协议勾不上
    service-required        页面要求先选运营商/服务，但配置里没填
    missing-service         配置的运营商名称在页面里找不到
    filled                  已填好（配置里关掉了自动提交）
    waiting-submit          等页面自己的事件处理完，下一轮再点
    submitted               已触发表单提交
    submission-unconfirmed  点了但页面没接住
"""

from __future__ import annotations

import json


_SCRIPT = r"""
(() => {
  const cfg = __CAMPUS_CONFIG__;
  // 刚开窗口时可能还是 about:blank，这时候既没页面也没 origin，等它导航完再说
  if (location.protocol === 'about:' || !location.href || location.href === 'about:blank') {
    return 'waiting-ready';
  }
  if (cfg.origin && location.origin !== cfg.origin) return 'wrong-origin';

  // ---- 页面自己给出的结果（成功页 / 学校回调打过的标记）----
  if (window.__cnResult === 'success') return 'authenticated';
  if (window.__cnResult === 'fail') return 'authentication-rejected';
  const params = new URLSearchParams(location.search || '');
  if (/success|success\.jsp|succeed/i.test(location.pathname || '') && params.get('userIndex')) {
    return 'authenticated';
  }
  if (cfg.statusOnly) return window.__cnSubmitted ? 'awaiting-authentication' : 'not-submitted';
  if (document.readyState !== 'complete') return 'waiting-ready';

  const visible = e => !!e && !e.disabled && e.getClientRects().length > 0;
  const editable = e => e instanceof HTMLInputElement && !e.disabled && !e.readOnly;
  const usable = e => editable(e) && visible(e);
  const setValue = (input, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  };

  let user = null, pass = null, submit = null, portalButton = null;

  try {
    // 已知门户（Dr.COM / 锐捷 ePortal）的选择器，命中就不必猜
    const knownUser = document.querySelector('#username');
    const knownPass = document.querySelector('#pwd');
    portalButton = document.querySelector('#loginLink');
    if (editable(knownUser) && editable(knownPass) && portalButton) {
      user = knownUser; pass = knownPass;
    }
  } catch (_) { return 'invalid-selector'; }

  // ---- 密码框：一切启发式的锚点 ----
  try {
    if (cfg.passwordSelector) {
      pass = document.querySelector(cfg.passwordSelector);
    } else if (!pass) {
      pass = [...document.querySelectorAll('input[type="password"]')].find(usable) || null;
    }
  } catch (_) { return 'invalid-selector'; }
  if (!pass) return 'missing-fields';

  const root = pass.form || document;

  // ---- 账号框 ----
  try {
    if (cfg.userSelector) {
      user = document.querySelector(cfg.userSelector);
    } else if (!user) {
      const texts = [...root.querySelectorAll(
        'input[type="text"],input[type="tel"],input[type="email"],input:not([type]),input[type="number"]'
      )].filter(usable);
      user = texts.find(e => /user|account|student|phone|mobile|login|学号|账号|帐号|手机/i.test(
        [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), e.autocomplete].join(' ')
      )) || texts[0] || null;
    }
  } catch (_) { return 'invalid-selector'; }
  if (!user) return 'missing-fields';

  // ---- 提交按钮 ----
  try {
    if (cfg.submitSelector) {
      submit = document.querySelector(cfg.submitSelector);
    } else if (portalButton) {
      submit = portalButton;
    } else {
      submit = [...root.querySelectorAll('button,input[type="submit"],input[type="button"],a,[role="button"]')]
        .filter(visible)
        .find(e => e.type === 'submit' ||
          /^(登录|登陆|认证|连接|确定|login|log ?in|sign ?in|connect)$/i.test(
            ((e.innerText || e.value || '').trim())
          )) || null;
    }
  } catch (_) { return 'invalid-selector'; }

  // ---- 需要人工处理的情况：验证码 ----
  const captcha = [...document.querySelectorAll(
    '#validCode,input[name*="code" i],input[id*="code" i],input[placeholder*="验证码"]'
  )].find(visible);
  if (captcha && !String(captcha.value || '').trim()) return 'manual-required';

  if (!usable(user) || !usable(pass)) return 'missing-fields';

  // ---- 填值 ----
  if (user.value !== cfg.username) setValue(user, cfg.username);
  if (pass.value !== cfg.password) setValue(pass, cfg.password);
  if (user.value !== cfg.username || pass.value !== cfg.password) return 'missing-fields';

  // ---- 必选协议勾选框（跳过"记住密码 / 订阅"这类）----
  const boxes = [...root.querySelectorAll('input[type="checkbox"],[role="checkbox"]')];
  for (const box of boxes) {
    if (box.form && box.form !== pass.form) continue;
    const labels = [];
    if (box.labels) labels.push(...box.labels);
    const wrapper = box.closest('label,.agreement,.protocol,.terms');
    if (wrapper) labels.push(wrapper);
    const text = [box.getAttribute('aria-label'), ...labels.map(l => l.textContent),
      box.id, box.name].join(' ');
    if (/记住|自动登录|保持登录|订阅|推广|广告|促销|remember|newsletter|marketing/i.test(text)) continue;
    const isAgreement = /同意|已阅读|阅读并|服务(使用)?协议|用户协议|隐私政策|\bagree\b|\baccept\b|\bterms\b|\bprivacy\b/i.test(text);
    if (!box.required && box.getAttribute('aria-required') !== 'true' && !isAgreement) continue;
    if (!visible(box) && !labels.some(visible)) continue;
    const checked = () => box instanceof HTMLInputElement ? box.checked : box.getAttribute('aria-checked') === 'true';
    if (checked()) continue;
    if (box.disabled || box.getAttribute('aria-disabled') === 'true') return 'agreement-required';
    box.click();
    if (!checked()) return 'waiting-agreement';
  }

  // ---- 运营商 / 服务选项 ----
  const pickService = () => {
    const name = (cfg.service || '').trim();
    const selects = [...document.querySelectorAll('select')]
      .filter(s => visible(s) && !s.multiple && (!s.form || s.form === pass.form));
    const placeholder = t => /^(请)?选择((运营商|服务)(选项)?|选项)[：:….。]*$/.test((t || '').replace(/\s/g, ''));
    if (!name) {
      if (selects.some(s => placeholder((s.selectedOptions[0] || {}).textContent))) return 'service-required';
      return '';
    }
    const match = selects
      .flatMap(s => [...s.options].map(o => ({select: s, option: o})))
      .filter(x => !x.option.disabled && x.option.textContent.trim() === name);
    if (match.length === 1) {
      const {select, option} = match[0];
      if (select.selectedIndex !== option.index) {
        select.selectedIndex = option.index;
        select.dispatchEvent(new Event('input', {bubbles: true}));
        select.dispatchEvent(new Event('change', {bubbles: true}));
      }
      return select.selectedOptions[0] === option ? '' : 'missing-service';
    }
    // 横向单选（常见：中国移动 / 中国电信 / 中国联通）
    const radios = [...document.querySelectorAll('input[type="radio"],[role="radio"]')].filter(control => {
      const form = control.form || control.closest('form');
      if (form && form !== pass.form) return false;
      const labels = [];
      if (control.labels) labels.push(...control.labels);
      const wrapper = control.closest('label,[role="radio"]');
      if (wrapper) labels.push(wrapper);
      if (!visible(control) && !labels.some(visible)) return false;
      const names = [control.getAttribute('aria-label'), ...labels.map(l => l.textContent)];
      if (control.getAttribute('role') === 'radio') names.push(control.textContent);
      return names.some(n => (n || '').trim() === name);
    });
    if (radios.length === 1) {
      const radio = radios[0];
      const checked = () => radio instanceof HTMLInputElement ? radio.checked : radio.getAttribute('aria-checked') === 'true';
      if (!checked()) radio.click();
      if (!radio.isConnected || !checked()) return 'missing-service';
      return '';
    }
    return 'missing-service';
  };
  const serviceState = pickService();
  if (serviceState) return serviceState;

  if (!cfg.submit) return 'filled';
  if (!submit || !visible(submit)) return 'missing-submit';

  // ---- 提交 ----
  if (window.__cnSubmitted) return 'submitted';
  const attempt = window.__cnAttempt || (window.__cnAttempt = {clicks: 0});
  if (attempt.clicks === 0) {
    // 先让账号框失焦，再点：很多门户在 blur 里做校验/加密
    attempt.clicks = 1;
    try { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); } catch (_) {}
    try { submit.focus(); } catch (_) {}
    return 'waiting-submit';
  }
  attempt.clicks += 1;
  try {
    if (attempt.clicks === 2) submit.click();
    else submit.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  } catch (_) {
    return 'submission-unconfirmed';
  }
  window.__cnSubmitted = true;
  return 'submitted';
})()
"""


def build(config: dict) -> str:
    """把配置（账号/密码/选择器等）拼进脚本，返回可执行的一段 JS。"""
    payload = json.dumps(config, ensure_ascii=False)
    return _SCRIPT.replace("__CAMPUS_CONFIG__", payload)


_PROBE = r"""
(() => {
  const out = {
    href: String(location.href || ''),
    origin: String(location.origin || ''),
    protocol: String(location.protocol || ''),
    ready: String(document.readyState || ''),
    title: String(document.title || '').slice(0, 120),
    state: 'ready'
  };
  if (out.protocol === 'about:' || !out.href || out.href === 'about:blank') {
    out.state = 'waiting-ready';
    return out;
  }
  if (/^(chrome|edge|about|view-source)-error:$/.test(out.protocol) ||
      out.href.indexOf('chrome-error://') === 0 ||
      out.href.indexOf('edge-error://') === 0) {
    out.state = 'page-error';
    return out;
  }
  if (out.protocol !== 'http:' && out.protocol !== 'https:') {
    out.state = 'insecure-origin';
    return out;
  }
  if (__EXPECTED_ORIGIN__ && out.origin !== __EXPECTED_ORIGIN__) {
    out.state = 'wrong-origin';
    return out;
  }
  if (out.ready !== 'complete') {
    out.state = 'waiting-ready';
    return out;
  }
  return out;
})()
"""


def build_probe(expected_origin: str) -> str:
    """构造"只看当前页面是什么，不碰账号密码"的探测脚本。

    自动填写之前必须先跑它：确认协议是 http/https、没有跳到别的 origin、
    也不是浏览器的证书错误页。探针脚本里**没有任何凭据**，即使被
    其它页面执行也不泄露东西。
    """
    return _PROBE.replace("__EXPECTED_ORIGIN__", json.dumps(expected_origin or ""))


# 状态字符串 → 给人看的中文（界面直接显示这个）
STATE_TEXT = {
    "wrong-origin": "页面地址与已保存的认证页面地址不一致，已停止填写。",
    "insecure-origin": "当前页面不是 http/https 网页（可能被重定向到本地文件或应用内页面），已停止填写。",
    "page-error": "认证页面加载失败（常见原因：校园网强制 HTTPS 但门户只有自签证书，或网络中断）。",
    "authenticated": "认证页面显示登录成功。",
    "authentication-rejected": "认证页面显示登录失败。",
    "waiting-ready": "页面加载中。",
    "missing-fields": "未找到账号或密码输入框。",
    "manual-required": "认证页面要求填写验证码，请在窗口中完成。",
    "agreement-required": "必选协议不可勾选。",
    "waiting-agreement": "正在勾选必选协议。",
    "service-required": "认证页面要求选择运营商 / 服务选项，尚未配置。",
    "missing-service": "未在认证页面找到所填的运营商 / 服务选项。",
    "filled": "账号与密码已填写。",
    "waiting-submit": "等待页面就绪后提交。",
    "submitted": "已提交，等待页面返回结果。",
    "submission-unconfirmed": "提交后页面无响应。",
    "awaiting-authentication": "已提交，等待认证结果。",
    "not-submitted": "尚未提交。",
    "invalid-selector": "自定义 CSS 选择器无效，请在设置里检查。",
    "missing-submit": "已填好账号密码，但没有找到登录按钮。",
}


def describe(state: str) -> str:
    return STATE_TEXT.get(state, state or "未知状态")
