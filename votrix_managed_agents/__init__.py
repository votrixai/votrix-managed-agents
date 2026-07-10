from importlib.metadata import PackageNotFoundError, version

from app.auth import AuthProvider, DatabaseApiKeyAuthProvider, EnvApiKeyAuthProvider, RequestCredentials
from app.factory import create_app
from app.workspace import CurrentWorkspace, default_workspace

try:
    __version__ = version("votrix-managed-agents")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "AuthProvider",
    "CurrentWorkspace",
    "DatabaseApiKeyAuthProvider",
    "EnvApiKeyAuthProvider",
    "RequestCredentials",
    "__version__",
    "create_app",
    "default_workspace",
]
