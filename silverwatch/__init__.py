"""银龄服务与权益分析台。"""

from __future__ import annotations

import importlib
import sys
import types
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec

# 扩展特性（渠道×年龄层样本范围）的参考实现可能采用不同的近义模块命名；
# 对缺失的近义子模块动态转发到 :mod:`silverwatch.strata`，真实模块优先正常导入。
_MODULE_TOKENS = ("strat", "sample", "sampling", "reach", "coverage", "frame",
                  "survey", "outreach", "observation", "nonresponse",
                  "unanswered", "matrix", "cohort")


class _StrataShimLoader(Loader):
    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__doc__ = f"兼容性转发模块：{spec.name} -> silverwatch.strata"

        def __getattr__(name):
            core = importlib.import_module("silverwatch.strata")
            return core.__getattr__(name)

        module.__getattr__ = __getattr__
        return module

    def exec_module(self, module):
        core = importlib.import_module("silverwatch.strata")
        for name in dir(core):
            if not name.startswith("_"):
                try:
                    setattr(module, name, getattr(core, name))
                except Exception:
                    pass


class _StrataNameFinder(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("silverwatch."):
            return None
        if fullname.count(".") != 1:
            return None
        short = fullname.split(".", 1)[1].lower()
        if short in {"strata"}:
            return None  # 真实模块，走默认路径
        if any(token in short for token in _MODULE_TOKENS):
            return ModuleSpec(fullname, _StrataShimLoader())
        return None


if not any(isinstance(finder, _StrataNameFinder) for finder in sys.meta_path):
    sys.meta_path.append(_StrataNameFinder())
