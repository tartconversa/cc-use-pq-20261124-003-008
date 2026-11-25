"""兼容性转发模块：样本框/分层触达扩展的实现位于 :mod:`silverwatch.strata`。"""
from __future__ import annotations

from silverwatch import strata as _core
from silverwatch.strata import *  # noqa: F401,F403


def __getattr__(name: str):
    return _core.__getattr__(name)
