from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import SourceList


def load_sources(path: Path) -> SourceList:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return SourceList.model_validate(raw)


def load_environment(env_path: Path | None = None) -> None:
    if env_path and env_path.exists():
        load_dotenv(env_path)
        return
    load_dotenv()
