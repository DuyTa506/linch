"""RecipeFile.path must reject every OS's notion of an absolute or escaping path."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from linch_studio.support.models import RecipeFile


def _recipe_file(path: str) -> RecipeFile:
    return RecipeFile(path=path, content="print('hi')\n")


@pytest.mark.parametrize(
    "path",
    [
        "src/my_agent/tools/greet.py",
        "tests/test_greet.py",
        "a/b/c.py",
    ],
)
def test_safe_relative_paths_are_accepted(path: str) -> None:
    assert _recipe_file(path).path == path


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secret.py",
        "src/../../secret.py",
        "src//tools/greet.py",
        "..\\secret.py",
        "C:\\Windows\\System32\\evil.py",
        "C:/Windows/System32/evil.py",
        "D:/payload.py",
        "c:relative_to_current_drive.py",
        "\\\\evil-server\\share\\payload.py",
    ],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValidationError, match="safe relative paths"):
        _recipe_file(path)
