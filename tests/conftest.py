"""让 pytest 能把项目根目录加入 sys.path，从而 `import harness`。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
