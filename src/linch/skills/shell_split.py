from __future__ import annotations


def split_shell_args(raw: str) -> list[str]:
    tokens: list[str] = []
    current = ""
    in_token = False
    quote: str | None = None
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                current += ch
            i += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            elif ch == "\\" and i + 1 < len(raw):
                i += 1
                current += raw[i]
            else:
                current += ch
            i += 1
            continue
        if ch.isspace():
            if in_token:
                tokens.append(current)
                current = ""
                in_token = False
            i += 1
            continue
        in_token = True
        if ch == "'" or ch == '"':
            quote = ch
        elif ch == "\\" and i + 1 < len(raw):
            i += 1
            current += raw[i]
        else:
            current += ch
        i += 1
    if quote is not None:
        tokens.append(current)
    elif in_token:
        tokens.append(current)
    return tokens
