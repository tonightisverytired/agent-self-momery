# -*- coding: utf-8 -*-
"""异常体系（docs/09 C11）。"""


class MemoryError(Exception):
    code = "E000"


class ValidationError(MemoryError):
    code = "E001"


class NotFoundError(MemoryError):
    code = "E006"


class ConflictError(MemoryError):
    code = "E008"


class StorageError(MemoryError):
    code = "E009"


class EmbeddingError(MemoryError):
    code = "E011"
