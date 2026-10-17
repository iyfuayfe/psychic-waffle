"""生成端到端自测用的配置（含 DPAPI 加密的密码）。

配合 tools/mock_portal.py 使用：
    python tools/mock_portal.py --port 8899
    python test/make_config.py --port 8899
    python campus_login.py --config test/config.test.json --once --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import crypto  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--username", default="testuser")
    ap.add_argument("--password", default="testpass123")
    ap.add_argument("--suffix", default="@cmcc")
    ap.add_argument("--out", default=str(ROOT / "test" / "config.test.json"))
    args = ap.parse_args(argv)

    base = f"http://{args.host}:{args.port}"
    cfg = {
        "portal_url": base + "/",
        "username": args.username,
        "password_enc": crypto.protect(args.password),
        "check_urls": [
            {"url": base + "/connecttest.txt", "expect_text": "Microsoft Connect Test"}
        ],
        "interval_online": 5,
        "interval_offline": 2,
        "interval_max": 10,
        "request_timeout": 5,
        "insecure_tls": True,
        "use_system_proxy": False,
        "log_level": "DEBUG",
        "drcom": {
            "enabled": True,
            "host": args.host,
            "port": args.port,
            "account_suffix": args.suffix,
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成测试配置：{out}")
    print(f"  账号：{args.username}{args.suffix}  密码已 DPAPI 加密")
    return 0


if __name__ == "__main__":
    sys.exit(main())
