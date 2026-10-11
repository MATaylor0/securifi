"""Operation handlers. Importing a module registers its operations."""

_LOADED = False


def load_all() -> None:
    global _LOADED
    if _LOADED:
        return
    from . import (  # noqa: F401
        admin,
        accounts,
        catalog,
        compliance,
        data,
        facility,
        periods,
        register,
        reporting,
    )

    _LOADED = True
