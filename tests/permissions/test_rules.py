from __future__ import annotations

import pytest

from linch.permissions.rules import BashRule, PathRule, match_bash_rule, match_path_rule


@pytest.mark.parametrize(
    ("pattern", "command", "matches"),
    [
        ("rm -rf /tmp", "rm -rf /tmp", True),
        ("rm -rf /tmp", "rm -rf /tmp/../etc", False),
        ("ls", "ls", True),
        ("ls", "lsof -i :8080", False),
        ("npm install", "npm install lodash", True),
        ("git *", "git status", True),
        ("rm -rf /", "rm -rf /", True),
    ],
)
def test_bash_rule_matching(pattern: str, command: str, matches: bool) -> None:
    assert match_bash_rule(BashRule(pattern, "allow"), command) is matches


@pytest.mark.parametrize(
    ("pattern", "matches", "nonmatches"),
    [
        ("config/*.json", ["config/app.json"], ["config/prod/secrets.json"]),
        ("config/**/*.json", ["config/prod/secrets.json", "config/app.json"], []),
    ],
)
def test_path_rule_matching(pattern: str, matches: list[str], nonmatches: list[str]) -> None:
    rule = PathRule([pattern], "allow", ["Write"])
    for path in matches:
        assert match_path_rule(rule, "Write", {"file_path": path}, "/project", "/project")
    for path in nonmatches:
        assert not match_path_rule(rule, "Write", {"file_path": path}, "/project", "/project")
