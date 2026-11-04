"""保证裸命令 python -m pytest tests/unit -q 在仓库根可直接跑。

把仓库根加进 sys.path，使测试能 import 插件模块（status.py / main.py）。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
