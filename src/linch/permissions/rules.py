from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

RuleDecision = Literal["allow", "deny", "ask", "passthrough"]
BashPattern: TypeAlias = str | dict[str, str]
BashRulePattern: TypeAlias = BashPattern | list[BashPattern]


class ToolRule:
    """Match a tool call by name and optionally by a Skill argument.

    Args:
        tool: Tool name to match, or ``"*"`` for every tool.
        decision: Policy outcome when the rule matches.
        arg: Optional Skill name to match for the ``Skill`` tool.
        role: Optional role required for the rule to apply.
        unless_role: Optional role whose presence disables the rule.

    Attributes:
        kind: The rule kind, always ``"tool"``.
        tool: The tool name or wildcard being matched.
        arg: The optional Skill argument constraint.
        decision: The validated policy outcome.
        role: The optional required role.
        unless_role: The optional excluded role.
    """

    __slots__ = ("kind", "tool", "arg", "decision", "role", "unless_role")

    decision: RuleDecision

    def __init__(
        self,
        tool: str,
        decision: RuleDecision,
        arg: str | None = None,
        *,
        role: str | None = None,
        unless_role: str | None = None,
    ) -> None:
        """Create a tool-name permission rule.

        Args:
            tool: Tool name to match, or ``"*"`` for every tool.
            decision: Policy outcome when the rule matches.
            arg: Optional Skill name to match for the ``Skill`` tool.
            role: Optional role required for the rule to apply.
            unless_role: Optional role whose presence disables the rule.

        Raises:
            ValueError: If ``decision`` is unsupported or a role is empty.
            TypeError: If a supplied role is not a string.
        """
        self.kind = "tool"
        self.tool = tool
        self.arg = arg
        self.decision = _validate_decision(decision)
        self.role = _validate_role(role, "role")
        self.unless_role = _validate_role(unless_role, "unless_role")


class PathRule:
    """Match file-oriented tool calls against project-relative path globs.

    Args:
        paths: Glob patterns evaluated against the resolved target path.
        decision: Policy outcome when a path match is found.
        tools: Tool names to which the path patterns apply; defaults to workspace and
            virtual filesystem write tools.
        role: Optional role required for the rule to apply.
        unless_role: Optional role whose presence disables the rule.

    Attributes:
        kind: The rule kind, always ``"path"``.
        tools: Tool names covered by the path rule.
        paths: Path glob patterns to evaluate.
        decision: The validated policy outcome.
        role: The optional required role.
        unless_role: The optional excluded role.
    """

    __slots__ = ("kind", "tools", "paths", "decision", "role", "unless_role")

    decision: RuleDecision

    def __init__(
        self,
        paths: list[str],
        decision: RuleDecision,
        tools: list[str] | None = None,
        *,
        role: str | None = None,
        unless_role: str | None = None,
    ) -> None:
        """Create a path-glob permission rule.

        Args:
            paths: Glob patterns evaluated against the resolved target path.
            decision: Policy outcome when a path match is found.
            tools: Tool names to which the path patterns apply; defaults to workspace and
                virtual filesystem write tools.
            role: Optional role required for the rule to apply.
            unless_role: Optional role whose presence disables the rule.

        Raises:
            ValueError: If ``decision`` is unsupported or a role is empty.
            TypeError: If a supplied role is not a string.
        """
        self.kind = "path"
        self.tools = tools
        self.paths = paths
        self.decision = _validate_decision(decision)
        self.role = _validate_role(role, "role")
        self.unless_role = _validate_role(unless_role, "unless_role")


class BashRule:
    """Match Bash command segments with shell-aware glob or regex patterns.

    Args:
        pattern: One shell glob, token prefix, or regex mapping to match.
        decision: Policy outcome when a command segment matches.
        patterns: Alternative sequence of patterns; mutually exclusive with ``pattern``.
        role: Optional role required for the rule to apply.
        unless_role: Optional role whose presence disables the rule.

    Attributes:
        kind: The rule kind, always ``"bash"``.
        pattern: The single pattern or normalized list of patterns.
        decision: The validated policy outcome.
        role: The optional required role.
        unless_role: The optional excluded role.
    """

    __slots__ = ("kind", "pattern", "decision", "role", "unless_role")

    pattern: BashRulePattern
    decision: RuleDecision

    def __init__(
        self,
        pattern: BashPattern | None = None,
        decision: RuleDecision = "ask",
        *,
        patterns: list[BashPattern] | tuple[BashPattern, ...] | None = None,
        role: str | None = None,
        unless_role: str | None = None,
    ) -> None:
        """Create a Bash command permission rule.

        Args:
            pattern: One shell glob, token prefix, or regex mapping to match.
            decision: Policy outcome when a command segment matches.
            patterns: Alternative sequence of patterns; mutually exclusive with ``pattern``.
            role: Optional role required for the rule to apply.
            unless_role: Optional role whose presence disables the rule.

        Raises:
            ValueError: If neither or both pattern forms are supplied, the sequence is empty,
                the decision is unsupported, or a role is empty.
            TypeError: If a supplied role is not a string.
        """
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
        self.role = _validate_role(role, "role")
        self.unless_role = _validate_role(unless_role, "unless_role")


PermissionRule = ToolRule | PathRule | BashRule


def match_tool_rule(rule: ToolRule, call_name: str, call_input: dict[str, Any]) -> bool:
    """Match a tool rule against a call, including normalized Skill names.

    Args:
        rule: Tool rule to evaluate.
        call_name: Name of the tool being called.
        call_input: Validated tool arguments.

    Returns:
        Whether the rule applies to the call.
    """
    if rule.tool != "*" and rule.tool != call_name:
        return False
    if rule.arg is None:
        return True
    if call_name != "Skill":
        return False
    if rule.arg == "*":
        return True
    skill_name = call_input.get("skill")
    if not isinstance(skill_name, str):
        return False
    if skill_name.startswith("/"):
        skill_name = skill_name[1:]
    return skill_name == rule.arg


def match_path_rule(
    rule: PathRule,
    call_name: str,
    call_input: dict[str, Any],
    project_root: str,
    cwd: str | None = None,
) -> bool:
    """Match a path rule against a canonicalized tool target.

    Args:
        rule: Path rule to evaluate.
        call_name: Name of the file-oriented tool being called.
        call_input: Validated tool arguments containing the target path.
        project_root: Root used for relative rule patterns.
        cwd: Effective working directory for the call.

    Returns:
        Whether the resolved target matches one of the rule patterns.
    """
    tools = rule.tools or ["Write", "Edit", "write_file", "edit_file"]
    if call_name not in tools:
        return False
    work_dir = cwd or project_root
    raw_path = _path_input_for_tool(call_name, call_input, work_dir)
    if raw_path is None:
        return False
    target = _normalize_for_glob(str(Path(work_dir, raw_path).resolve()))
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
    """Evaluate Bash rules while asking for unsupported shell constructs.

    Args:
        rules: Applicable Bash rules in first-match order.
        command: Shell command to classify and evaluate.

    Returns:
        The matched decision, an ask floor for unparsed commands, or ``None``
        when the rules abstain.
    """
    if _contains_unparsed_shell_construct(command):
        stripped = command.strip()
        if any(
            rule.decision == "deny" and _match_unparsed_bash_rule(rule, stripped) for rule in rules
        ):
            return "deny"
        if any(rule.decision in {"allow", "ask", "deny"} for rule in rules):
            return "ask"
        return None

    segments = [s for s in split_composite_command(command) if s.strip() != ""]
    if not segments:
        return None
    all_allowed = True
    saw_ask = False
    for segment in segments:
        decision = _first_matching_bash_decision(rules, segment)
        if decision == "deny":
            return "deny"
        if decision == "ask":
            saw_ask = True
        if decision != "allow":
            all_allowed = False
    if saw_ask:
        return "ask"
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
    if tool in ("read_file", "write_file", "edit_file"):
        val = input.get("path")
        if isinstance(val, str) and val.strip() != "":
            return val.lstrip("/")
        return None
    if tool == "ls":
        val = input.get("prefix")
        if isinstance(val, str) and val.strip() != "":
            return val.lstrip("/")
        return cwd
    return None


def _matches_path_pattern(pattern: str, target: str, project_root: str) -> bool:
    inverted = pattern.startswith("!")
    body = pattern[1:] if inverted else pattern
    if body == "":
        return False
    absolute_pattern = Path(body) if Path(body).is_absolute() else Path(project_root) / body
    glob_pattern = _normalize_for_glob(str(absolute_pattern.resolve()))
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


def _match_unparsed_bash_rule(rule: BashRule, command: str) -> bool:
    if isinstance(rule.pattern, list):
        return any(_match_bash_pattern(pattern, command) for pattern in rule.pattern)
    return _match_bash_pattern(rule.pattern, command)


def _contains_unparsed_shell_construct(command: str) -> bool:
    """Return whether a command contains shell syntax this parser cannot model."""
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(command):
        char = command[i]
        if escaped:
            if char in "\r\n":
                return True
            escaped = False
            i += 1
            continue
        if quote != "'" and char == "\\":
            escaped = True
            i += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                i += 1
                continue
            if char == "`" or command[i : i + 2] == "$(":
                return True
            i += 1
            continue
        if char == "'" or char == '"':
            quote = char
            i += 1
            continue
        if char in "\r\n" or char == "`":
            return True
        if command[i : i + 2] == "$(":
            return True
        if command[i : i + 2] in {"<(", ">("}:
            return True
        if char in "()":
            return True
        if char == "&":
            next_char = command[i + 1 : i + 2]
            previous_char = command[i - 1 : i]
            if next_char == ">" or previous_char in {"<", ">"}:
                i += 1
                continue
            if next_char == "&":
                i += 2
                continue
            return True
        i += 1
    return escaped or quote is not None


def _tokens_start_with(tokens: list[str], prefix: list[str]) -> bool:
    if not prefix or len(prefix) > len(tokens):
        return False
    return all(tokens[i] == prefix[i] for i in range(len(prefix)))


def _first_matching_bash_decision(
    rules: list[BashRule], command_segment: str
) -> RuleDecision | None:
    for rule in rules:
        if match_bash_rule(rule, command_segment):
            if rule.decision == "passthrough":
                continue
            return rule.decision
    return None


def _validate_decision(decision: str) -> RuleDecision:
    if decision in {"allow", "deny", "ask", "passthrough"}:
        return cast(RuleDecision, decision)
    raise ValueError(f"invalid rule decision: {decision!r}")


def _validate_role(role: str | None, name: str) -> str | None:
    if role is None:
        return None
    if not isinstance(role, str):
        raise TypeError(f"{name} must be a string")
    if not role.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return role


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
