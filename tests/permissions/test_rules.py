from linch.permissions.rules import (
    BashRule,
    PathRule,
    match_bash_rule,
    match_path_rule,
)


def test_bash_allow_does_not_match_substring_extension() -> None:
    rule = BashRule("rm -rf /tmp", "allow")
    assert match_bash_rule(rule, "rm -rf /tmp")
    assert not match_bash_rule(rule, "rm -rf /tmp/../etc")


def test_bash_allow_does_not_match_command_prefix_substring() -> None:
    rule = BashRule("ls", "allow")
    assert match_bash_rule(rule, "ls")
    assert not match_bash_rule(rule, "lsof -i :8080")


def test_bash_allow_matches_true_token_prefix() -> None:
    rule = BashRule("npm install", "allow")
    assert match_bash_rule(rule, "npm install lodash")


def test_bash_allow_matches_glob() -> None:
    rule = BashRule("git *", "allow")
    assert match_bash_rule(rule, "git status")


def test_bash_deny_still_works() -> None:
    rule = BashRule("rm -rf /", "deny")
    assert match_bash_rule(rule, "rm -rf /")


def test_path_single_star_does_not_cross_slash() -> None:
    rule = PathRule(["config/*.json"], "allow", ["Write"])
    project_root = "/project"
    cwd = "/project"
    assert match_path_rule(
        rule,
        "Write",
        {"file_path": "config/app.json"},
        project_root,
        cwd,
    )
    assert not match_path_rule(
        rule,
        "Write",
        {"file_path": "config/prod/secrets.json"},
        project_root,
        cwd,
    )


def test_path_double_star_crosses_slash() -> None:
    rule = PathRule(["config/**/*.json"], "allow", ["Write"])
    project_root = "/project"
    cwd = "/project"
    assert match_path_rule(
        rule,
        "Write",
        {"file_path": "config/prod/secrets.json"},
        project_root,
        cwd,
    )
    # ``**/`` matches zero directories too — ``config/**/*.json`` must match a
    # file directly under ``config/`` (gitignore/glob semantics).
    assert match_path_rule(
        rule,
        "Write",
        {"file_path": "config/app.json"},
        project_root,
        cwd,
    )
