from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

RuleDecision = Literal["allow", "deny", "ask"]
BashPattern: TypeAlias = str | dict[str, str]
BashRulePattern: TypeAlias = BashPattern | list[BashPattern]


class ToolRule:
    __slots__ = ("kind", "tool", "arg", "decision")

    decision: RuleDecision

    def __init__(
        self,
        tool: str,
        decision: RuleDecision,
        arg: str | None = None,
    ) -> None:
        self.kind = "tool"
        self.tool = tool
        self.arg = arg
        self.decision = _validate_decision(decision)


class PathRule:
    __slots__ = ("kind", "tools", "paths", "decision")

    decision: RuleDecision

    def __init__(
        self,
        paths: list[str],
        decision: RuleDecision,
        tools: list[str] | None = None,
    ) -> None:
        self.kind = "path"
        self.tools = tools
        self.paths = paths
        self.decision = _validate_decision(decision)


class BashRule:
    __slots__ = ("kind", "pattern", "decision")

    pattern: BashRulePattern
    decision: RuleDecision

    def __init__(
        self,
        pattern: BashPattern | None = None,
        decision: RuleDecision = "ask",
        *,
        patterns: list[BashPattern] | tuple[BashPattern, ...] | None = None,
    ) -> None:
        if pattern is not None and patterns is not None:
            raise ValueError("BashRule accepts either pattern or patterns, not both")
        if patterns is not None:
            if not patterns:
                raise ValueError("BashRule patterns must not be empty")
            resolved_pattern: BashRulePattern = list(patterns)
        elif pattern is not None:
            resolved_pattern = pattern
        else:
            raise ValueError("BashRule requires pattern or patterns")
        self.kind = "bash"
        self.pattern = resolved_pattern
        self.decision = _validate_decision(decision)


PermissionRule = ToolRule | PathRule | BashRule


def match_tool_rule(rule: ToolRule, call_name: str, call_input: dict[str, Any]) -> bool:
    if rule.tool != "*" and rule.tool != call_name:
        return False
    if rule.arg is None:
        return True
    if call_name != "Skill":
        return False
    if rule.arg == "*":
        return True
    skill_name = call_input.get("skill")
    return isinstance(skill_name, str) and skill_name == rule.arg


def match_path_rule(
    rule: PathRule,
    call_name: str,
    call_input: dict[str, Any],
    project_root: str,
    cwd: str | None = None,
) -> bool:
    tools = rule.tools or ["Write", "Edit"]
    if call_name not in tools:
        return False
    work_dir = cwd or project_root
    raw_path = _path_input_for_tool(call_name, call_input, work_dir)
    if raw_path is None:
        return False
    target = _normalize_for_glob(str(Path(work_dir).resolve() / raw_path))
    pattern_root = str(Path(project_root).resolve())
    return any(_matches_path_pattern(p, target, pattern_root) for p in rule.paths)


def match_bash_rule(rule: BashRule, command_segment: str) -> bool:
    segment = command_segment.strip()
    if isinstance(rule.pattern, list):
        return any(_match_bash_pattern(pattern, segment) for pattern in rule.pattern)
    return _match_bash_pattern(rule.pattern, segment)


def _match_bash_pattern(pattern: str | dict[str, str], segment: str) -> bool:
    if isinstance(pattern, str):
        if fnmatch.fnmatch(segment, pattern):
            return True
        return _tokens_start_with(
            tokenize_shell_prefix(segment),
            tokenize_shell_prefix(pattern),
        )
    try:
        compiled = re.compile(pattern["regex"])
        return compiled.search(segment) is not None
    except re.error:
        return False


def evaluate_bash_rules(
    rules: list[BashRule],
    command: str,
) -> RuleDecision | None:
    segments = [s for s in split_composite_command(command) if s.strip() != ""]
    if not segments:
        return None
    all_allowed = True
    for segment in segments:
        decision = _first_matching_bash_decision(rules, segment)
        if decision == "deny":
            return "deny"
        if decision != "allow":
            all_allowed = False
    return "allow" if all_allowed else None


def tokenize_shell_prefix(input_str: str) -> list[str]:
    tokens: list[str] = []
    current = ""
    quote: str | None = None
    in_token = False
    i = 0
    while i < len(input_str):
        char = input_str[i]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                current += char
            i += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char == "\\" and i + 1 < len(input_str):
                i += 1
                current += input_str[i]
            else:
                current += char
            i += 1
            continue
        if char.isspace():
            if in_token:
                tokens.append(current)
                current = ""
                in_token = False
            i += 1
            continue
        in_token = True
        if char == "'" or char == '"':
            quote = char
        elif char == "\\" and i + 1 < len(input_str):
            i += 1
            current += input_str[i]
        else:
            current += char
        i += 1
    if in_token:
        tokens.append(current)
    return tokens


def split_composite_command(input_str: str) -> list[str]:
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(input_str):
        char = input_str[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if quote != "'" and char == "\\":
            escaped = True
            i += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            i += 1
            continue
        if char == "'" or char == '"':
            quote = char
            i += 1
            continue
        op_len = _composite_operator_length(input_str, i)
        if op_len > 0:
            segments.append(input_str[start:i])
            i += op_len
            start = i
        else:
            i += 1
    segments.append(input_str[start:])
    return segments


def _path_input_for_tool(tool: str, input: dict[str, Any], cwd: str) -> str | None:
    if tool in ("Read", "Write", "Edit"):
        val = input.get("file_path")
        if isinstance(val, str) and val.strip() != "":
            return val
        return None
    if tool in ("Glob", "Grep"):
        val = input.get("path")
        if isinstance(val, str) and val.strip() != "":
            return val
        return cwd
    return None


def _matches_path_pattern(pattern: str, target: str, project_root: str) -> bool:
    inverted = pattern.startswith("!")
    body = pattern[1:] if inverted else pattern
    if body == "":
        return False
    absolute_pattern = body if Path(body).is_absolute() else str(Path(project_root) / body)
    glob_pattern = _normalize_for_glob(absolute_pattern)
    matched = re.fullmatch(_glob_to_regex(glob_pattern), target) is not None
    return not matched if inverted else matched


def _glob_to_regex(pattern: str) -> str:
    """Translate a glob pattern to a regex where ``*``/``?`` do not cross ``/``
    but ``**`` does, matching standard gitignore/glob semantics."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # ``**/`` is an optional path prefix: it matches zero or more
                # directory segments, so ``a/**/b`` matches both ``a/b`` and
                # ``a/x/b``.  A standalone ``**`` stays ``.*``.
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


def _normalize_for_glob(value: str) -> str:
    return value.replace("\\", "/")


def _tokens_start_with(tokens: list[str], prefix: list[str]) -> bool:
    if not prefix or len(prefix) > len(tokens):
        return False
    return all(tokens[i] == prefix[i] for i in range(len(prefix)))


def _first_matching_bash_decision(
    rules: list[BashRule], command_segment: str
) -> RuleDecision | None:
    for rule in rules:
        if match_bash_rule(rule, command_segment):
            return rule.decision
    return None


def _validate_decision(decision: str) -> RuleDecision:
    if decision in {"allow", "deny", "ask"}:
        return cast(RuleDecision, decision)
    raise ValueError(f"invalid rule decision: {decision!r}")


def _composite_operator_length(input_str: str, index: int) -> int:
    char = input_str[index : index + 1]
    if not char:
        return 0
    next_char = input_str[index + 1 : index + 2]
    if (char == "&" and next_char == "&") or (char == "|" and next_char == "|"):
        return 2
    if char == ";" or char == "|":
        return 1
    return 0
