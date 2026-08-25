"""Download and unpack official game clients into verified snapshots."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("game-downloader")
except PackageNotFoundError:  # pragma: no cover - only when imported without installation
    __version__ = "0.1.0"

__all__ = ["__version__"]
