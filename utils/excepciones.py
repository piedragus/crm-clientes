class AliasValidationError(ValueError):
    """Alias vacío o inválido. HTTP 400."""
    pass


class EmpresaNotFoundError(KeyError):
    """Empresa inexistente. HTTP 404."""
    pass


class AliasConflictError(Exception):
    """Alias ya asignado a otra empresa. HTTP 409."""
    pass
