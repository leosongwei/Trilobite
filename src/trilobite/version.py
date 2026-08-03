"""Trilobite 版本号获取，web 服务与 CLI 共用。"""

import importlib.metadata


def get_version() -> str:
    try:
        return importlib.metadata.version("trilobite-code")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
