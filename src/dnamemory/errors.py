# -*- coding: utf-8 -*-
"""异常体系。"""


class MemoryError(Exception):
    code = "E000"


class ValidationError(MemoryError):
    code = "E001"


class NotFoundError(ValidationError):
    code = "E006"


class ConflictError(MemoryError):
    code = "E008"


class StorageError(MemoryError):
    code = "E009"


class EmbeddingError(MemoryError):
    code = "E011"


class MemoryStateConflictError(MemoryError):
    code = "E012"


class EvidenceNotFoundError(MemoryError):
    code = "E013"


class TemporalChainInvalidError(MemoryError):
    code = "E014"


class ContextBuildFailedError(MemoryError):
    code = "E015"


class UnsupportedBeliefError(MemoryError):
    code = "E016"


class CoherenceConflictError(MemoryError):
    code = "E017"
