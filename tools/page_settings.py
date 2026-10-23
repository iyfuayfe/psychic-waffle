"""V3.0 兼容薄壳：实现已搬到 `campusnet.ui.page_settings`。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from campusnet.ui.page_settings import *  # noqa: F401,F403,E402