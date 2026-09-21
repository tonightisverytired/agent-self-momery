# -*- coding: utf-8 -*-
from .memory import MemorySystem
from .models import (MemoryConfig, MemoryHit, RecallFilters, RecallQuery,
                     RelationFilter)

__version__ = "0.8.3"

__all__ = ["MemorySystem", "MemoryConfig", "RecallQuery", "RecallFilters",
           "RecallQuery", "RelationFilter", "MemoryHit", "__version__"]

# 0.8.1 二轮：HyDE 查询改写协议与参考实现（惰性：不 import 不触网）
from .hyde import DeepSeekHyDERewriter, QueryRewriter  # noqa: E402,F401
