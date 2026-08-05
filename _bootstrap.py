"""导入副作用模块：将插件目录加入 sys.path（确保 PySide6 等本地依赖可导入）。

为什么独立成模块：官方插件市场 verify 的 ruff 检查启用 E402 且 --ignore-noqa，
要求 __init__.py 顶部不得有"非 import 语句"先于 import。sys.path 注入移入
本模块后，__init__.py 只需 `from . import _bootstrap` 即可保持全部 import 置顶。
"""

import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
