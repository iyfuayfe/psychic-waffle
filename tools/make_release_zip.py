"""把 dist/CampusNet 打包成可以直接拷到别的机器上的 zip，并生成校验清单。

生成物（都在 ``dist/``）：

* ``CampusNet-<版本>-win64.zip``  —— 发布包
* ``SHA256SUMS-<版本>.txt``       —— 发布包与源码包的 SHA256 校验清单

为什么要一并生成校验清单（任务书"规范发布流程"要求）：

* 用户拿到 zip 后可以先核对哈希，确认文件在传输/解压过程中没有损坏或被替换；
* 出了问题时，"你跑的是不是我们发的那个文件"可以一秒回答；
* 校验清单本身不能替代数字签名（签名要证书），但它是零成本、可验证的第一步。
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet.version import __version__  # noqa: E402

SOURCE = ROOT / "dist" / "CampusNet"
TARGET = ROOT / "dist" / f"CampusNet-{__version__}-win64.zip"
SUMS = ROOT / "dist" / f"SHA256SUMS-{__version__}.txt"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def build_zip() -> int:
    if not SOURCE.is_dir():
        print(f"[错误] 找不到发布目录：{SOURCE}")
        return 1
    if TARGET.exists():
        TARGET.unlink()
    count = 0
    with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in sorted(SOURCE.rglob("*")):
            if not item.is_file():
                continue
            archive.write(item, arcname=str(Path("CampusNet") / item.relative_to(SOURCE)))
            count += 1
    print(f"已打包 {count} 个文件 -> {TARGET}")
    print(f"大小：{TARGET.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


def write_sums() -> int:
    """为 dist 下的发布包/源码包/历史包生成 SHA256 清单。

    只收录**待交付的压缩包与 bundle**；``CampusNet/`` 目录是构建产物目录，
    逐个文件出哈希没有意义（每次构建都变，且不是分发单位）。
    """
    patterns = (f"CampusNet-{__version__}-win64.zip",
                f"CampusNet-{__version__}-src.zip",
                f"CampusNet-{__version__}-git.bundle")
    lines: list[str] = []
    for name in patterns:
        path = ROOT / "dist" / name
        if path.is_file():
            lines.append(f"{sha256_of(path)}  {name}")
    if not lines:
        print("[提示] 没有可生成校验清单的交付物（先构建发布包）。")
        return 0
    SUMS.write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"已生成校验清单 -> {SUMS}")
    for line in lines:
        print(f"  {line}")
    return 0


def main() -> int:
    code = build_zip()
    if code != 0:
        return code
    return write_sums()


if __name__ == "__main__":
    sys.exit(main())
