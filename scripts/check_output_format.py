# audience: internal
# # check-output-format
# 检查中文正文中的半角标点和过长枚举.

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
INLINE_CODE_PATTERN = re.compile(
    r"(?P<ticks>`+)(?!`)(?P<content>[^\n]*?)(?<!`)(?P=ticks)"
)
MARKDOWN_LINK_PATTERN = re.compile(r"!?\[[^\]\n]*\]\([^\n]*\)")
MARKDOWN_DEFINITION_PATTERN = re.compile(r"^ {0,3}!?\[[^\]\n]*\]:[^\n]*$")
LINK_TARGET_PATTERN = re.compile(r"\]\([^\n)]*\)")
URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；：！？]+")
FENCE_PATTERN = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE_PATTERN = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})\s*$")
QUOTE_PREFIX_PATTERN = re.compile(r"^ {0,3}>[ \t]?")
LIST_PREFIX_PATTERN = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])[ \t]+")
ASCII_PUNCTUATION = frozenset(",.;:!?()\"'")
URL_TRAILING_PUNCTUATION = frozenset(".,;:!?")
URL_BRACKETS = {")": "(", "]": "[", "}": "{"}
FULLWIDTH_PAIRS = {
    "（": "）",
    "【": "】",
    "《": "》",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
    "〈": "〉",
    "〔": "〕",
}
FULLWIDTH_CLOSERS = frozenset(FULLWIDTH_PAIRS.values())


# //// 确定网址正文边界 [@x380kkm 2026-07-23] ////
def find_url_content_end(line: str, start: int, end: int) -> int:
    while end > start:
        character = line[end - 1]
        if character in URL_TRAILING_PUNCTUATION:
            end -= 1
            continue
        opening = URL_BRACKETS.get(character)
        if opening is None:
            break
        url = line[start:end]
        if url.count(opening) >= url.count(character):
            break
        end -= 1
    return end


# //// /确定网址正文边界 ////


# //// 遮住不改写的文字 [@x380kkm 2026-07-23] ////
def mask_protected_text(line: str) -> str:
    characters = list(line)
    for pattern in (
        INLINE_CODE_PATTERN,
        MARKDOWN_LINK_PATTERN,
        MARKDOWN_DEFINITION_PATTERN,
        LINK_TARGET_PATTERN,
        URL_PATTERN,
    ):
        for match in pattern.finditer(line):
            end = match.end()
            if pattern is URL_PATTERN:
                end = find_url_content_end(line, match.start(), end)
            characters[match.start() : end] = " " * (end - match.start())
    return "".join(characters)


# //// /遮住不改写的文字 ////


# //// 去掉 Markdown 容器前缀 [@x380kkm 2026-07-23] ////
def strip_markdown_container_prefix(line: str) -> str:
    remaining = line
    while True:
        quote = QUOTE_PREFIX_PATTERN.match(remaining)
        if quote is not None:
            remaining = remaining[quote.end() :]
            continue
        list_item = LIST_PREFIX_PATTERN.match(remaining)
        if list_item is not None:
            remaining = remaining[list_item.end() :]
            continue
        return remaining


# //// /去掉 Markdown 容器前缀 ////


# //// 读取代码块起始标记 [@x380kkm 2026-07-23] ////
def get_opening_fence(line: str) -> str | None:
    content = strip_markdown_container_prefix(line)
    match = FENCE_PATTERN.fullmatch(content)
    if match is None:
        return None
    marker = match.group("marker")
    info = match.group("info")
    if marker[0] == "`" and "`" in info:
        return None
    return marker


# //// /读取代码块起始标记 ////


# //// 判断代码块结束标记 [@x380kkm 2026-07-23] ////
def closes_fence(line: str, opening_character: str, opening_length: int) -> bool:
    content = strip_markdown_container_prefix(line)
    match = FENCE_CLOSE_PATTERN.fullmatch(content)
    if match is None:
        return False
    marker = match.group("marker")
    return marker[0] == opening_character and len(marker) >= opening_length


# //// /判断代码块结束标记 ////


# //// 判断标点是否属于标识 [@x380kkm 2026-07-23] ////
def is_protected_punctuation(text: str, index: int) -> bool:
    character = text[index]
    previous = text[index - 1] if index > 0 else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    if (
        previous.isascii()
        and not previous.isspace()
        and (following.isascii() or not following)
    ):
        left = text[:index].rstrip()
        right = text[index + 1 :].lstrip()
        if left and (not right or right[0].isascii()):
            return True
    if character == "." and previous.isascii() and following.isascii():
        return previous.isalnum() and following.isalnum()
    if character == "," and previous.isdigit() and following.isdigit():
        return True
    if character == ":" and previous.isdigit() and following.isdigit():
        return True
    if character == "'" and previous.isalpha() and following.isalpha():
        return previous.isascii() and following.isascii()
    return False


# //// /判断标点是否属于标识 ////


# //// 查找中文格式错误 [@x380kkm 2026-07-23] ////
def find_format_errors(
    text: str, punctuation_mode: str = "fullwidth"
) -> list[dict[str, object]]:
    if punctuation_mode not in {"fullwidth", "ascii"}:
        raise ValueError("punctuation_mode 必须是 fullwidth 或 ascii")
    errors: list[dict[str, object]] = []
    open_fence: tuple[str, int, int, int] | None = None
    list_context_indent: int | None = None
    paragraph_lines: list[tuple[int, str]] = []
    delimiter_stack: list[tuple[str, int, int]] = []

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        paragraph = "\n".join(item[1] for item in paragraph_lines)
        first_line = paragraph_lines[0][0]
        for sentence in re.split(r"[。！？!?]", paragraph):
            if sentence.count("，") + sentence.count(",") < 8:
                continue
            errors.append(
                {
                    "line": first_line,
                    "column": 1,
                    "kind": "long_enumeration",
                    "text": sentence.strip(),
                }
            )
        paragraph_lines.clear()

    for line_number, line in enumerate(text.splitlines(), start=1):
        if open_fence is not None:
            closing_line = line
            if open_fence[3] and line.startswith(" " * open_fence[3]):
                closing_line = line[open_fence[3] :]
            if closes_fence(closing_line, open_fence[0], open_fence[1]):
                open_fence = None
            continue
        if LIST_PREFIX_PATTERN.match(line) is not None:
            list_context_indent = 4
        elif line.strip() and (
            list_context_indent is None
            or not line.startswith(" " * list_context_indent)
        ):
            list_context_indent = None
        marker = get_opening_fence(line)
        fence_indent = 0
        if marker is None and list_context_indent:
            nested_line = line[list_context_indent :]
            marker = get_opening_fence(nested_line)
            if marker is not None:
                fence_indent = list_context_indent
        if marker is not None:
            flush_paragraph()
            open_fence = (marker[0], len(marker), line_number, fence_indent)
            continue

        editable = strip_markdown_container_prefix(mask_protected_text(line))
        if CJK_PATTERN.search(editable) is None:
            if not editable.strip():
                flush_paragraph()
            continue
        if punctuation_mode == "fullwidth":
            for index, character in enumerate(editable):
                if character not in ASCII_PUNCTUATION:
                    continue
                if is_protected_punctuation(editable, index):
                    continue
                errors.append(
                    {
                        "line": line_number,
                        "column": index + 1,
                        "kind": "ascii_punctuation",
                        "text": character,
                    }
                )
        if editable.lstrip().startswith("。"):
            errors.append(
                {
                    "line": line_number,
                    "column": line.find("。") + 1,
                    "kind": "line_start_period",
                    "text": "。",
                }
            )
        if editable.lstrip().startswith("#") and editable.rstrip().endswith(
            ("。", "．", ".")
        ):
            errors.append(
                {
                    "line": line_number,
                    "column": len(line.rstrip()),
                    "kind": "heading_period",
                    "text": line.rstrip()[-1],
                }
            )
        for index, character in enumerate(editable):
            if character in FULLWIDTH_PAIRS:
                delimiter_stack.append((character, line_number, index + 1))
            elif character in FULLWIDTH_CLOSERS:
                if delimiter_stack and FULLWIDTH_PAIRS[delimiter_stack[-1][0]] == character:
                    delimiter_stack.pop()
                else:
                    errors.append(
                        {
                            "line": line_number,
                            "column": index + 1,
                            "kind": "unmatched_closer",
                            "text": character,
                        }
                    )
        paragraph_lines.append((line_number, editable))
    flush_paragraph()
    for opener, line_number, column in reversed(delimiter_stack):
        errors.append(
            {
                "line": line_number,
                "column": column,
                "kind": "unmatched_opener",
                "text": opener,
            }
        )
    if open_fence is not None:
        errors.append(
            {
                "line": open_fence[2],
                "column": 1,
                "kind": "unclosed_code_fence",
                "text": open_fence[0] * open_fence[1],
            }
        )
    return errors


# //// /查找中文格式错误 ////


# //// 运行中文格式检查 [@x380kkm 2026-07-23] ////
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--punctuation",
        choices=("fullwidth", "ascii"),
        default="fullwidth",
    )
    args = parser.parse_args()
    try:
        text = args.artifact.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        print(json.dumps({"valid": False, "errors": [str(error)]}, ensure_ascii=False))
        return 2

    errors = find_format_errors(text, args.punctuation)
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))
    return 0 if not errors else 1


# //// /运行中文格式检查 ////


if __name__ == "__main__":
    raise SystemExit(main())
