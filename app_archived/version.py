from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("votrix-managed-agents")
except PackageNotFoundError:
    __version__ = "0.0.0"


__all__ = ["__version__"]
