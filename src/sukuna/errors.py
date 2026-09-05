"""Domain-specific errors with stable CLI error codes."""


class CrossBufferError(RuntimeError):
    """Base error raised for an expected manager failure."""

    code = "CROSS_BUFFER_ERROR"


class ValidationError(CrossBufferError):
    code = "VALIDATION_ERROR"


class NotFoundError(CrossBufferError):
    code = "WORKER_NOT_FOUND"


class StateError(CrossBufferError):
    code = "INVALID_STATE"


class BackendError(CrossBufferError):
    code = "BACKEND_ERROR"


class SchemaVersionError(CrossBufferError):
    code = "SCHEMA_VERSION_ERROR"


class RegistryDocumentError(CrossBufferError):
    code = "REGISTRY_DOCUMENT_ERROR"


class RegistryIOError(CrossBufferError):
    code = "REGISTRY_IO_ERROR"


class ConflictError(CrossBufferError):
    code = "CONFLICT"


class ModelCatalogError(CrossBufferError):
    code = "MODEL_CATALOG_ERROR"
