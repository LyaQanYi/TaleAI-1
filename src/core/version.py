"""版本号：唯一来源是 pyproject.toml（面板“关于”、启动日志都读这里）。"""

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def version() -> str:
    try:
        with _PYPROJECT.open("rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"
