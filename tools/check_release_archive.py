"""检查发布压缩包里没有工作区垃圾/个人痕迹。

为什么要单独做这个检查器：

* 3.1.1 的源码包（``dist/CampusNet-3.1.1-src.zip``）里混进了 ``.dsh-vision-router``
  的截图、``__pycache__`` 目录、空的 ``test/_e.txt``；截图文件名还带着
  ``<盘符>--<Windows 用户名>-Documents-…`` 这种个人路径痕迹。这和 ``docs/REVIEW_3.1.0.md``
  里"完整工程源码（不含任何个人数据）"、``KNOWN_ISSUES`` K-07 的"不会进发布包"
  直接冲突。
* ``tests/test_release_hygiene.py`` 只扫 ``.py``，正则本身也失效了，所以没拦住。
* 于是把"包内容体检"独立成一个 CI 与本地发版都能调用的工具：只要包里出现已知垃圾
  目录/后缀/个人路径，就用非 0 退出，让人在把包发出去**之前**先看到问题。

用法：python tools/check_release_archive.py dist/CampusNet-3.1.2-src.zip
退出码：0 干净，1 命中问题，2 参数/文件错误。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

# 每一条都是"已知绝不该出现在发布包里"的东西，命中的条目会被打印出来。
FORBIDDEN = (
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"(^|/)\.dsh-vision-router/"),
    re.compile(r"(^|/)\.git/"),
    re.compile(r"(^|/)logs?/"),
    re.compile(r"(^|/)config\.json$"),
    re.compile(r"(^|/)state\.json$"),
    re.compile(r"(^|/)heartbeat\.json$"),
    re.compile(r"\.(pyc|pyo|log|tmp|bak)$", re.I),
    re.compile(r"(^|/)test/_e\.txt$"),
)
PERSONAL = re.compile(r"Users[/\\][A-Za-z0-9_.-]+", re.I)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python tools/check_release_archive.py <zip>")
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"[错误] 找不到压缩包: {path}")
        return 2

    bad: list[tuple[str, str]] = []
    scanned = 0
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"[错误] 打不开压缩包: {path}（{exc}）")
        return 2
    with archive:
        for info in archive.infolist():
            # 压缩包内可能是 ``\`` 或 ``/``，先归一化再匹配，否则同一条垃圾会漏检。
            name = info.filename.replace("\\", "/")
            if info.is_dir() or name.endswith("/"):
                continue
            scanned += 1
            for pattern in FORBIDDEN:
                if pattern.search(name):
                    bad.append((name, f"命中 {pattern.pattern}"))
                    break
            if PERSONAL.search(name):
                bad.append((name, "路径含 Windows 用户名"))

    if bad:
        print(f"[失败] {path.name} 含 {len(bad)} 个不该发布的条目：")
        for name, why in bad[:50]:
            print(f"  - {name}  ({why})")
        return 1
    print(f"[通过] {path.name} 条目干净（已检查 {scanned} 个文件条目）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))