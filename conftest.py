"""pytest 配置：把本目录加进 sys.path，让各服务测试能 import 共享的 testutil
（httpx 桩替身 + 服务模块加载器）。"""
import sys
from pathlib import Path

_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
