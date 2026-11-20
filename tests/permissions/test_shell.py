"""Shell analysis for Bash rules: tokenizer, splitter, detector, prefix, redirects.

The cases are the unit tests of the Rust kernel's `permissions/shell.rs`
(`linch-rust/crates/linch-core/src/permissions/shell.rs`), copied so both
kernels analyse the same text the same way.
"""

from __future__ import annotations

import pytest

from linch.permissions.shell import (
    has_unparsed_construct,
    pattern_matches,
    redirects_to_file,
    split_composite,
    strip_command_prefix,
    tokenize,
    unquote_words,
)


def test_tokenizer_removes_quotes_and_escapes() -> None:
    assert tokenize("git  commit -m 'a b'") == ["git", "commit", "-m", "a b"]
    assert tokenize('echo "x \\" y" a\\ b') == ["echo", 'x " y', "a b"]
    assert tokenize("echo 'a\\' b") == ["echo", "a\\", "b"]
    assert tokenize("''") == [""]
    # Inside double quotes a backslash escapes only `$`, backtick, `"`, `\`
    # and newline; before any other byte the shell keeps it.
    assert tokenize('echo "a\\nb" "c\\$d" "e\\\\f" "g\\`h"') == [
        "echo",
        "a\\nb",
        "c$d",
        "e\\f",
        "g`h",
    ]
    assert tokenize("tail\\") == ["tail\\"]
    assert tokenize(" \t ") == []
    # Only space and tab separate words, as in bash.
    assert tokenize("a b") == ["a b"]


def test_splitter_honors_quotes_and_escapes() -> None:
    assert split_composite("a; b && c || d | e") == ["a", " b ", " c ", " d ", " e"]
    assert split_composite("echo 'a;b' \"c|d\" e\\;f") == ["echo 'a;b' \"c|d\" e\\;f"]
    assert split_composite(";;") == ["", "", ""]
    assert split_composite("é;ü") == ["é", "ü"]


_UNPARSED = [
    "a & b",
    "a &",
    "a\nb",
    "a\rb",
    "a \\\nb",
    "echo $(x)",
    'echo "$(x)"',
    "echo `x`",
    'echo "`x`"',
    "cat <(x)",
    "(cd a)",
    "echo 'open",
    'echo "open',
    "echo trailing\\",
    "a |& b",
    "echo \"${x#'a'}\"",
    "echo \"${x#'\"'}\" ; b ; echo \\'",
    'echo "${x:-${y}}"',
    "echo \"${x:+$[}]'\"'}\" ; b ; echo \\'",
    'echo "${x#\\"}"',
    "a &> log",
    "echo hi &>/tmp/f touch x",
    "echo \\>& x",
    "echo \\<& x",
    "echo >& file",
    "echo >&$fd",
    "echo '>'& x",
    "echo hi #'\ntouch x\n#'",
    "echo hi #",
    "echo hi;#x",
    "echo hi|#x",
    "#x",
    "a >| f",
    "$a -rf x",
    "x=1 $a",
    "a | ${b}",
    'a; "$b"',
    "a <<EOF",
    "a <<-EOF",
    "a <<'EOF'",
    "echo \\$(x)",
    "echo $'\\'' ; rm -rf / ; '\\'",
]

_PARSED = [
    "a && b",
    "a 2>&1",
    "a <&3",
    "a >&-",
    "a 1>&2 2>&1",
    "a <<< word",
    'a <<<"x;y"',
    "a $b",
    "x=$a b",
    "'$a' b",
    "a#b",
    "echo $#",
    "echo ${#x} ${x#p}",
    "echo \\# \"#\" '#'",
    'echo "a\\x"',
    'echo "a\\"b"',
    "{ a; }",
    "printf '&'",
    'echo "${x#a}" "${y:-b}" ${z#\'"\'}',
    "printf '$(x)'",
    'echo "\\$(x)"',
    'echo "a\nb"',
    "echo \\$'x'",
    "echo \"$'x'\"",
]


@pytest.mark.parametrize("command", _UNPARSED)
def test_detector_flags_what_the_splitter_cannot_model(command: str) -> None:
    assert has_unparsed_construct(command)


@pytest.mark.parametrize("command", _PARSED)
def test_detector_leaves_modelled_syntax_alone(command: str) -> None:
    assert not has_unparsed_construct(command)


def test_ansi_c_quoting_cannot_hide_a_segment() -> None:
    # Bash reads `$'\''` as one quoted `'`, so `rm` runs; the quote tracker
    # sees the `;`s as quoted and would return one segment.
    command = "echo $'\\'' ; rm -rf / ; '\\'"
    assert len(split_composite(command)) == 1
    assert has_unparsed_construct(command)


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("rm -rf x", "rm -rf x"),
        ("  X=1 rm x", "rm x"),
        ("X=1 Y='a b' Z+=\"c d\" rm x", "rm x"),
        ("if rm x", "rm x"),
        ("then ! { rm x", "rm x"),
        ("time -p command -- rm x", "rm x"),
        ("env -i X=1 rm x", "rm x"),
        ("env -u 2>&1 X rm x", "rm x"),
        ("env -iu > /dev/null X rm x", "rm x"),
        (">log 2>&1 rm x", "rm x"),
        ("> log < in rm x", "rm x"),
        ("2>&1 rm x", "rm x"),
        ("X=1", ""),
        ("then", ""),
        ("echo if rm x", "echo if rm x"),
        ("X='a b rm x", ""),
        ('"X"=1 rm x', '"X"=1 rm x'),
        ("1x=1 rm x", "1x=1 rm x"),
    ],
)
def test_prefix_words_are_stripped_before_the_command(segment: str, expected: str) -> None:
    assert strip_command_prefix(segment) == expected


@pytest.mark.parametrize(
    ("pattern", "segment", "expected"),
    [
        ("rm -rf /tmp", "rm -rf /tmp", True),
        ("rm -rf /tmp", "rm -rf /tmp/../etc", False),
        ("ls", "ls", True),
        ("ls", "lsof -i :8080", False),
        ("npm install", "npm install lodash", True),
        ("git *", "git status", True),
        ("git *", "  git status\t", True),
        ("rm -rf /", "rm -rf /", True),
        ("rm [!a]*", "rm b", True),
        ("rm [!a]*", "rm a", False),
        ("", "ls", False),
    ],
)
def test_patterns_match_by_glob_or_word_prefix(pattern: str, segment: str, expected: bool) -> None:
    assert pattern_matches(pattern, segment) is expected


@pytest.mark.parametrize(
    "command",
    [
        "a > f",
        "a >f",
        "a >> f",
        "a 2>f",
        "a >| f",
        "a <> f",
        'a >"f"',
        "a 2>&1 > f",
        "a >",
        "a >&2file",
        "a >&1M",
        "a >&2-M",
        'a >&2"file"',
        "a >&-file",
        "a >&2-",
    ],
)
def test_file_redirects_are_found(command: str) -> None:
    assert redirects_to_file(command)


@pytest.mark.parametrize(
    "command",
    [
        "a",
        "a 2>&1",
        "a >&2",
        "a >&-",
        "a >&/dev/null",
        "a <&3",
        "a < f",
        "a <<< f",
        "a > /dev/null",
        "a 2>/dev/stderr >/dev/stdout",
        "a '>f' \"> f\" \\>f",
    ],
)
def test_file_redirects_are_not_found_in_quotes_or_for_descriptors_and_devices(
    command: str,
) -> None:
    assert not redirects_to_file(command)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("\\rm -rf x", "rm -rf x"),
        ("git 'push' \"a b\"", "git push 'a b'"),
        ("git 'commit' -m 'it'\\''s --force'", "git commit -m 'it'\\''s --force'"),
        ("r\"\"m '' x", "rm '' x"),
        ("rm  -rf   x", "rm -rf x"),
        ("git\tpush  origin", "git push origin"),
        ("git 2>&1 push origin", "git push origin"),
        ("git >/dev/null push origin", "git push origin"),
        ("git > /dev/null push origin", "git push origin"),
        ("git push 2>&1 origin", "git push origin"),
        ("git push < in", "git push"),
        ("  git  'push'  x ", "git push x"),
    ],
)
def test_unquote_words_unquotes_every_word_and_keeps_the_boundaries(
    command: str, expected: str
) -> None:
    assert unquote_words(command) == expected
