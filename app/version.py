from importlib.metadata import PackageNotFoundError, version


try:
    # The control plane's own distribution. `votrix-managed-agents` is the
    # client SDK, which may be installed alongside it and carries its own
    # version — reading that one here would report the wrong build.
    __version__ = version("votrix-managed-agents-server")
except PackageNotFoundError:
    __version__ = "0.0.0"


__all__ = ["__version__"]
