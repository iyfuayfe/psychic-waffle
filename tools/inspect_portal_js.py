"""排障用：抓取门户自己的 JS，反查登录请求到底怎么拼的。

不猜参数，直接读门户源码。用法：
    python tools/inspect_portal_js.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campusnet.http import HttpClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "logs" / "portal_assets"


def save(name: str, text: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(text, encoding="utf-8", errors="replace")
    return path


def main() -> int:
    client = HttpClient(timeout=15, insecure_tls=True, use_system_proxy=False)

    home = client.get("http://111.17.200.130/", follow_redirects=True)
    print(f"首页 HTTP {home.status} -> {home.url} ({len(home.body)}B)")
    save("home.html", home.text)

    assets: list[str] = []
    for m in re.finditer(r"""<script[^>]+src\s*=\s*["']([^"']+)["']""", home.text, re.I):
        assets.append(m.group(1))
    for m in re.finditer(r"""<link[^>]+href\s*=\s*["']([^"']+\.css[^"']*)["']""", home.text, re.I):
        assets.append(m.group(1))
    print("发现资源：", assets)

    import urllib.parse

    for i, asset in enumerate(assets):
        url = urllib.parse.urljoin(home.url, asset)
        try:
            r = client.get(url, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  {url} -> 失败 {exc}")
            continue
        name = re.sub(r"[^\w.\-]", "_", asset.split("?")[0].split("/")[-1]) or f"asset{i}.js"
        path = save(name, r.text)
        print(f"  {url} -> HTTP {r.status} {len(r.body)}B 已存 {path.name}")

    # 在所有 JS 里找登录相关的片段
    print("\n" + "=" * 70)
    print("在 JS 里搜索登录请求构造：")
    keys = ("eportal/portal/login", "user_account", "wlan_user_ip", "ACSetting", "a=Login",
            "authloginpath", "0MKKey", "+'&'", "login_method", "wlanuserip", "nasip")
    for path in sorted(OUT.glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in keys:
            for m in re.finditer(re.escape(key), text):
                s = max(0, m.start() - 400)
                e = min(len(text), m.end() + 400)
                snippet = text[s:e].replace("\n", " ")
                print(f"\n--- {path.name} :: {key} @{m.start()} ---")
                print("   " + snippet)
                break  # 每个 key 只打第一处，够读了
    return 0


if __name__ == "__main__":
    sys.exit(main())
