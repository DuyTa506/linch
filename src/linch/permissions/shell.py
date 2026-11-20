"""Shell command analysis for Bash rules.

The model is small on purpose: words, single and double quotes, backslash
escapes, the list operators ``;`` ``&&`` ``||`` ``|``, and redirections (``>``,
``2>&1``). Anything that can run a command the splitter would not see as its own
segment is reported by :func:`has_unparsed_construct`, and the rule evaluation
applies its floor to it instead of trusting the segments. Words are separated
by space and tab only, as in bash. The behaviour matches the Rust kernel's
``permissions/shell.rs``.
"""

from __future__ import annotations

import fnmatch
import re

_BLANKS = " \t"
_DIGITS = "0123456789"
_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})

# Words that precede a command without being it.
_RESERVED = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{"})
# Commands that run their arguments as a command, and may take options.
_WRAPPERS = frozenset({"time", "exec", "command", "builtin", "env"})

_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_NAME_START = re.compile(r"[A-Za-z_]")
_NAME = re.compile(r"[A-Za-z0-9_]*")


def trim_blanks(text: str) -> str:
    """Return ``text`` without leading and trailing spaces and tabs."""
    return text.strip(_BLANKS)


def tokenize(text: str) -> list[str]:
    """Split one command segment into words, removing quotes and backslash escapes.

    Args:
        text: Command segment to split; expansions stay literal text.

    Returns:
        The words as the shell would pass them on.
    """
    tokens: list[str] = []
    current: list[str] = []
    in_token = False
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        i += 1
        if quote == "'":
            if c == "'":
                quote = None
            else:
                current.append(c)
        elif quote == '"':
            if c == '"':
                quote = None
            elif c == "\\":
                # Inside double quotes only these bytes lose their backslash;
                # before any other byte the shell keeps it.
                following = text[i : i + 1]
                if following != "" and following in '$`"\\':
                    current.append(following)
                    i += 1
                elif following == "\n":
                    i += 1
                else:
                    current.append(c)
            else:
                current.append(c)
        elif c in _BLANKS:
            if in_token:
                tokens.append("".join(current))
                current = []
                in_token = False
        else:
            in_token = True
            if c in "'\"":
                quote = c
            elif c == "\\":
                current.append(text[i : i + 1] or "\\")
                i += 1
            else:
                current.append(c)
    if in_token:
        tokens.append("".join(current))
    return tokens


def split_composite(text: str) -> list[str]:
    """Split a command at unquoted, unescaped ``;``, ``&&``, ``||`` and ``|``.

    Args:
        text: Command to split.

    Returns:
        The segments, untrimmed; some may be empty.
    """
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if escaped:
            escaped = False
        elif quote != "'" and c == "\\":
            escaped = True
        elif quote is not None:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        else:
            following = text[i + 1 : i + 2]
            if (c == "&" and following == "&") or (c == "|" and following == "|"):
                op = 2
            elif c in ";|":
                op = 1
            else:
                op = 0
            if op:
                segments.append(text[start:i])
                i += op
                start = i
                continue
        i += 1
    segments.append(text[start:])
    return segments


def has_unparsed_construct(command: str) -> bool:
    """Return whether ``command`` uses shell syntax that :func:`split_composite` cannot model.

    That is a background ``&`` (``&>`` included: ``sh`` reads it as ``&`` then ``>``),
    an unquoted newline or carriage return, a line continuation, a comment (an
    unquoted ``#`` at the start of a word), a here-document (``<<``, ``<<-``), the
    clobber redirect ``>|``, command substitution (backtick, ``$(``), process
    substitution or subshell parentheses, ANSI-C quoting (``$'...'``, whose ``\\'``
    the quote tracking would misread), an unbalanced quote or trailing backslash, or a
    ``${...}`` inside double quotes whose body holds a quote, a backslash or a nested
    ``${``. ``&`` is a redirection only right after an unescaped, unquoted ``<`` or
    ``>`` and names a descriptor (``2>&1``, ``>&2``, ``<&3``, ``>&-``): what
    ``>&file`` means differs between shells. A ``$`` expansion in command position
    (``$a -rf x``) or ``env -S`` is reported too: the command is not in the text.

    Args:
        command: Shell command to inspect.

    Returns:
        Whether bash rules cannot vouch for every command in ``command``.
    """
    n = len(command)
    quote: str | None = None
    escaped = False
    # Index just past the last unescaped, unquoted `<` or `>`.
    redirect_end = 0
    # Index just past the last unescaped, unquoted blank or operator.
    break_end = 0
    i = 0
    while i < n:
        c = command[i]
        following = command[i + 1 : i + 2]
        if escaped:
            if c in "\r\n":
                return True
            escaped = False
        elif quote != "'" and c == "\\":
            escaped = True
        elif quote == "'":
            if c == "'":
                quote = None
        elif quote == '"':
            if c == '"':
                quote = None
            elif (
                c == "`"
                or (c == "$" and following == "(")
                or (c == "$" and following == "{" and _quotes_in_expansion(command, i + 2))
            ):
                return True
        else:
            if c in " \t;&|<>":
                break_end = i + 1
            if c in "'\"":
                quote = c
            elif c in "\r\n`()":
                return True
            elif c == "$" and following != "" and following in "('":
                return True
            elif c == "#" and (i == 0 or break_end == i):
                return True
            elif c == "<" and command.startswith("<<<", i):
                # A here-string: its word is an ordinary word.
                i += 3
                redirect_end = i
                break_end = i
                continue
            elif c == "<" and following == "<":
                return True
            elif c == ">" and following == "|":
                return True
            elif c in "<>":
                redirect_end = i + 1
            elif c == "&" and redirect_end == i and following != "" and following in "0123456789-":
                # `2>&1`, `>&2`, `<&3`: a redirection.
                pass
            elif c == "&" and following == "&":
                i += 2
                break_end = i
                continue
            elif c == "&":
                return True
        i += 1
    return (
        escaped
        or quote is not None
        or any(_command_is_not_in_the_text(s) for s in split_composite(command))
    )


def _quotes_in_expansion(command: str, start: int) -> bool:
    """Return whether the body of a ``${`` up to its first ``}`` holds a quote, backslash or ``${``.

    The shell reads those on their own (``"${x#'"'}"`` quotes a ``"`` inside the
    pattern), which the quote tracking cannot follow, so the operators are not modelled.
    """
    end = command.find("}", start)
    body = command[start:] if end < 0 else command[start:end]
    return any(c in "'\"\\" for c in body) or "${" in body


def _word_end(text: str) -> int:
    """Return the end of the first word of ``text``: the first blank outside quotes and escapes."""
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote is not None and c == quote:
            quote = None
        elif quote == "'":
            pass
        elif c == "\\":
            i += 1
        elif quote is not None:
            pass
        elif c in "'\"":
            quote = c
        elif c in _BLANKS:
            return i
        i += 1
    return n


def _unquote(word: str) -> str:
    """Return ``word`` with its quotes and backslashes removed."""
    return "".join(tokenize(word))


def _is_assignment(word: str) -> bool:
    """Return whether ``word`` is ``NAME=value`` or ``NAME+=value``."""
    return _ASSIGNMENT.match(word) is not None


def _is_name(text: str) -> bool:
    return _NAME.fullmatch(text) is not None


def _is_array_assignment(word: str) -> bool:
    """Return whether ``word`` is an assignment to an array element (``a[0]=1``)."""
    name, sep, _ = word.partition("=")
    return sep != "" and _NAME_START.match(name) is not None and "[" in name


def _is_fd_redirection(word: str) -> bool:
    """Return whether ``word`` is a redirection with a descriptor variable (``{fd}>log``)."""
    if not word.startswith("{"):
        return False
    name, sep, after = word[1:].partition("}")
    return sep != "" and name != "" and _is_name(name) and after[:1] in ("<", ">")


def _wrapper_option(wrapper: str, option: str) -> str:
    """Classify a wrapper option as ``plain``, ``takes`` an argument, ``split`` or ``query``."""
    if (
        (wrapper == "env" and option in ("-u", "-C", "--unset", "--chdir"))
        or (wrapper == "time" and option in ("-f", "-o", "--format", "--output"))
        or (wrapper == "exec" and option == "-a")
    ):
        return "takes"
    if wrapper == "env" and option.startswith(("-S", "--split-string")):
        return "split"
    if wrapper == "command" and not option.startswith("--") and ("v" in option or "V" in option):
        return "query"
    return "plain"


def _strip_prefix(segment: str) -> tuple[str, bool]:
    """Return the segment from its command word on and whether that command is opaque."""
    rest = trim_blanks(segment)
    # The wrapper whose options are being read, and the text from it on.
    wrapper: tuple[str, str] | None = None
    while True:
        word = rest[: _word_end(rest)]
        operator = word.lstrip(_DIGITS)
        if _is_array_assignment(word) or _is_fd_redirection(word):
            return rest, True
        assignment = _is_assignment(word)
        name = "" if assignment else _unquote(word)
        skip = 1
        if assignment:
            pass
        elif name in _RESERVED:
            wrapper = None
        elif name in _WRAPPERS:
            wrapper = (name, rest)
        elif wrapper is not None and word.startswith("-"):
            kind = _wrapper_option(wrapper[0], name)
            if kind == "takes":
                skip = 2
            elif kind == "split":
                return rest, True
            elif kind == "query":
                return wrapper[1], False
        elif operator.startswith(("<", ">")):
            # A bare operator (`>`, `2>&`) takes the next word as its target.
            if operator.lstrip("<>&|") == "":
                skip = 2
        else:
            return rest, name in ("function", "coproc")
        for _ in range(skip):
            rest = trim_blanks(rest[_word_end(rest) :])


def strip_command_prefix(segment: str) -> str:
    """Return ``segment`` without the words in front of its command.

    Those words may be quoted or escaped (``\\env``): ``NAME=value`` assignments,
    redirections (``>f``, ``2>&1``, ``< in``), reserved words (``if``, ``then``,
    ``else``, ``elif``, ``do``, ``while``, ``until``, ``!``, ``{``) and the wrappers
    ``time``, ``exec``, ``command``, ``builtin`` and ``env`` with their options
    (``env -u NAME``, ``time -o FILE``, ``exec -a NAME`` take an argument;
    ``command -v`` only looks a name up and is kept whole). Deny and ask rules are
    matched against this form as well, so a prefix cannot hide the command. Allow
    rules never use it: ``X=1 echo`` is not ``echo``. Name-based and best effort:
    ``/bin/rm``, ``sudo rm`` and ``sh -c 'rm x'`` are other commands.

    Args:
        segment: One command segment.

    Returns:
        The segment from its command word on, possibly empty.
    """
    return _strip_prefix(segment)[0]


def _command_is_not_in_the_text(segment: str) -> bool:
    """Return whether the command of ``segment`` is not in its text.

    That is the case when the shell reads it from a string (``env -S``), when the
    segment starts with a bash construct that is not modelled (``function``,
    ``coproc``, ``a[0]=1``, ``{fd}>log``), or when its word, after
    :func:`strip_command_prefix`, holds a ``$`` expansion outside single quotes or a
    brace expansion (``{rm,-rf}``).
    """
    text, opaque = _strip_prefix(segment)
    if opaque:
        return True
    quote: str | None = None
    escaped = False
    opened = False
    last = ""
    for c in text[: _word_end(text)]:
        if escaped:
            escaped = False
        elif c == "\\" and quote != "'":
            escaped = True
        elif quote is not None:
            if c == quote:
                quote = None
            elif c == "$" and quote == '"':
                return True
        elif c in "'\"":
            quote = c
        elif c == "$" or (opened and (c == "," or (c == "." and last == "."))):
            return True
        elif c == "{":
            opened = True
        last = c
    return False


def pattern_matches(pattern: str, segment: str) -> bool:
    """Return whether bash rule ``pattern`` matches one command segment.

    The pattern matches as a glob over the whole trimmed segment (``*`` spans
    anything, ``[...]`` classes work), or because the pattern's words are a prefix
    of the segment's words.

    Args:
        pattern: Glob or word-prefix pattern of a bash rule.
        segment: One command segment.

    Returns:
        Whether the segment matches.
    """
    segment = trim_blanks(segment)
    if fnmatch.fnmatch(segment, pattern):
        return True
    prefix = tokenize(pattern)
    return bool(prefix) and tokenize(segment)[: len(prefix)] == prefix


def redirects_to_file(segment: str) -> bool:
    """Return whether ``segment`` opens a file for writing with a redirection.

    Covers ``>``, ``>>``, ``>|`` and ``<>``, with or without a descriptor number.
    Descriptor duplication (``2>&1``, ``>&2``, ``>&-``) and ``/dev/null``,
    ``/dev/stdout`` and ``/dev/stderr`` are not files.

    Args:
        segment: One command segment.

    Returns:
        Whether an unquoted, unescaped redirection targets a file.
    """
    quote: str | None = None
    escaped = False
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if escaped:
            escaped = False
        elif c == "\\" and quote != "'":
            escaped = True
        elif quote is not None:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == ">" or (c == "<" and segment[i + 1 : i + 2] == ">"):
            end = i + 1
            if c == "<" or segment[end : end + 1] in (">", "|"):
                end += 1
            if c == ">" and segment[end : end + 1] == "&":
                # `>&2`, `>&-`: duplicates a descriptor.
                i = end + 1
                continue
            rest = trim_blanks(segment[end:])
            if _unquote(rest[: _word_end(rest)]) not in _DEVICES:
                return True
            i = end
            continue
        i += 1
    return False


def unquote_command_word(command: str) -> str:
    """Return ``command`` with the quotes and backslashes of its first word removed.

    ``\\rm -rf x`` becomes ``rm -rf x``; the rest stays as written, so a quoted
    argument keeps its boundaries.

    Args:
        command: Command text starting at its command word.

    Returns:
        The text with an unquoted command word.
    """
    end = _word_end(command)
    if not any(c in "'\"\\" for c in command[:end]):
        return command
    return _unquote(command[:end]) + command[end:]
