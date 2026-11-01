from __future__ import annotations

import pytest

from linch.permissions.rules import BashRule, PathRule, match_bash_rule, match_path_rule


@pytest.mark.parametrize(
    ("pattern", "action", "command", "matches"),
    [
        ("rm -rf /tmp", "allow", "rm -rf /tmp", True),
        ("rm -rf /tmp", "allow", "rm -rf /tmp/../etc", False),
        ("ls", "allow", "ls", True),
        ("ls", "allow", "lsof -i :8080", False),
        ("npm install", "allow", "npm install lodash", True),
        ("git *", "allow", "git status", True),
        ("rm -rf /", "deny", "rm -rf /", True),
    ],
)
def test_bash_rule_matching(pattern: str, action: str, command: str, matches: bool) -> None:
    assert match_bash_rule(BashRule(pattern, action), command) is matches


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
