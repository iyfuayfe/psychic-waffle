"""V3.0 兼容薄壳：真正的实现已搬到 ``campusnet.ui``。

保留这个文件是为了让旧命令（以及旧文档里的写法）继续可用，
不再维护第二份实现。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from campusnet.ui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["--page", "overview"]))