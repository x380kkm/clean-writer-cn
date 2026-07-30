# audience: internal
# # cleanread-symbol-enrichment
# 对 index_build.py 生成的 SQLite 文件执行轻量符号与关系抽取。

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

if __package__:
    from . import resolve_cleanread_paths
else:
    from __init__ import resolve_cleanread_paths


LANGUAGE_BY_EXTENSION = {
    ".c": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".md": "markdown",
    ".markdown": "markdown",
    ".py": "python",
    ".sql": "sql",
}
CALL_KEYWORDS = frozenset(
    {
        "and",
        "catch",
        "class",
        "def",
        "do",
        "else",
        "for",
        "if",
        "is",
        "match",
        "new",
        "or",
        "switch",
        "template",
        "throw",
        "try",
        "unless",
        "using",
        "while",
        "with",
        "as",
        "assert",
        "await",
        "break",
        "continue",
        "elif",
        "except",
        "finally",
        "from",
        "global",
        "import",
        "lambda",
        "nonlocal",
        "pass",
        "raise",
        "return",
        "yield",
        "in",
        "not",
    }
)
CPP_DECLARATION_WORDS = frozenset(
    {
        "catch",
        "do",
        "for",
        "if",
        "switch",
        "while",
    }
)
PYTHON_BUILTINS = frozenset(
    {
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "open",
        "print",
        "range",
        "set",
        "str",
        "super",
        "tuple",
        "type",
        "zip",
    }
)


@dataclass
class SymbolCandidate:
    name: str
    kind: str
    lang: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    parent_index: int | None = None
    attrs: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceLines:
    lines: tuple[str, ...]
    starts: tuple[int, ...]
    total_bytes: int


# //// 计算源码行的字节跨度 [@x380kkm 2026-07-24] ////
def source_lines(content: str) -> SourceLines:
    lines = tuple(content.splitlines(keepends=True))
    if not lines:
        lines = ("",)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line.encode("utf-8"))
    return SourceLines(lines=lines, starts=tuple(starts), total_bytes=len(content.encode("utf-8")))


# //// /计算源码行的字节跨度 ////


# //// 计算行结束字节 [@x380kkm 2026-07-24] ////
def end_byte_for_line(layout: SourceLines, line_number: int) -> int:
    if line_number < len(layout.lines):
        return layout.starts[line_number]
    return layout.total_bytes


# //// /计算行结束字节 ////


# //// 规范化名称 [@x380kkm 2026-07-24] ////
def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).strip("# ") or "<anonymous>"


# //// /规范化名称 ////


# //// 遮住注释和字符串并保持偏移 [@x380kkm 2026-07-24] ////
def mask_non_code(text: str, lang: str) -> str:
    characters = list(text)
    line_comment = {"python": "#", "cpp": "//", "sql": "--"}.get(lang)
    block_comments = lang in {"cpp", "sql"}
    quote: str | None = None
    index = 0

    def blank(position: int) -> None:
        if characters[position] not in {"\n", "\r"}:
            characters[position] = " "

    while index < len(text):
        if quote is not None:
            if text.startswith(quote, index):
                for offset in range(len(quote)):
                    blank(index + offset)
                index += len(quote)
                quote = None
                continue
            if text[index] == "\\" and index + 1 < len(text):
                blank(index)
                blank(index + 1)
                index += 2
                continue
            blank(index)
            index += 1
            continue

        if line_comment and text.startswith(line_comment, index):
            while index < len(text) and text[index] not in {"\n", "\r"}:
                blank(index)
                index += 1
            continue
        if block_comments and text.startswith("/*", index):
            blank(index)
            blank(index + 1)
            index += 2
            while index < len(text) and not text.startswith("*/", index):
                blank(index)
                index += 1
            if index < len(text):
                blank(index)
                blank(index + 1)
                index += 2
            continue

        if lang == "cpp" and text.startswith('R"', index):
            raw_match = re.match(r'R"(?P<delimiter>[^\s()\\]{0,16})\(', text[index:])
            if raw_match is not None:
                delimiter = raw_match.group("delimiter")
                closing = ")" + delimiter + '"'
                closing_start = text.find(closing, index + raw_match.end())
                if closing_start >= 0:
                    closing_end = closing_start + len(closing)
                    for position in range(index, closing_end):
                        blank(position)
                    index = closing_end
                    continue

        if lang == "python" and text.startswith("'''", index):
            quote = "'''"
            for offset in range(3):
                blank(index + offset)
            index += 3
            continue
        if lang == "python" and text.startswith('\"\"\"', index):
            quote = '\"\"\"'
            for offset in range(3):
                blank(index + offset)
            index += 3
            continue
        if text[index] in {"'", '"'} or (lang == "sql" and text[index] == "`"):
            quote = text[index]
            blank(index)
            index += 1
            continue
        index += 1
    return "".join(characters)


# //// /遮住注释和字符串并保持偏移 ////


# //// 遮住 Markdown 代码围栏并保持偏移 [@x380kkm 2026-07-24] ////
def mask_markdown_fences(content: str) -> str:
    characters = list(content)
    opening: tuple[str, int] | None = None
    offset = 0
    for line in content.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(?P<marker>`{3,}|~{3,})", line)
        if opening is None and match is not None:
            marker = match.group("marker")
            opening = (marker[0], len(marker))
        elif opening is not None and match is not None:
            marker = match.group("marker")
            if marker[0] == opening[0] and len(marker) >= opening[1]:
                opening = None
        if opening is not None or (match is not None and match.group("marker")):
            for position in range(offset, offset + len(line)):
                if characters[position] not in {"\n", "\r"}:
                    characters[position] = " "
        offset += len(line)
    return "".join(characters)


# //// /遮住 Markdown 代码围栏并保持偏移 ////


# //// 创建文件级符号 [@x380kkm 2026-07-24] ////
def module_symbol(path: str, lang: str, layout: SourceLines) -> SymbolCandidate:
    name = Path(path).stem or path
    return SymbolCandidate(
        name=name,
        kind="module",
        lang=lang,
        start_line=1,
        end_line=len(layout.lines),
        start_byte=0,
        end_byte=layout.total_bytes,
        attrs={"extractor": "regex"},
    )


# //// /创建文件级符号 ////


# //// 计算 Python 符号范围 [@x380kkm 2026-07-24] ////
def python_end_line(lines: Sequence[str], start: int, indent: int) -> int:
    end = len(lines)
    for index in range(start + 1, len(lines)):
        text = lines[index].rstrip("\r\n")
        if not text.strip():
            continue
        current_indent = len(text) - len(text.lstrip(" \t"))
        if current_indent <= indent:
            end = index
            break
    return end


# //// /计算 Python 符号范围 ////


# //// 抽取 Python 符号 [@x380kkm 2026-07-24] ////
def extract_python(path: str, content: str, layout: SourceLines) -> list[SymbolCandidate]:
    symbols = [module_symbol(path, "python", layout)]
    masked_layout = source_lines(mask_non_code(content, "python"))
    definitions: list[tuple[int, int, str, str]] = []
    definition_pattern = re.compile(
        r"^(?P<indent>[ \t]*)(?:(?:async)[ \t]+)?(?P<kind>def|class)[ \t]+(?P<name>[A-Za-z_]\w*)"
    )
    for index, raw_line in enumerate(masked_layout.lines):
        line = raw_line.rstrip("\r\n")
        match = definition_pattern.match(line)
        if not match:
            continue
        indent_text = match.group("indent")
        indent = len(indent_text.expandtabs(8))
        kind = "class" if match.group("kind") == "class" else "function"
        end = python_end_line(layout.lines, index, indent)
        definitions.append((index, end, kind, match.group("name")))

    for index, (start, end, kind, name) in enumerate(definitions):
        indent_text = definition_pattern.match(masked_layout.lines[start].rstrip("\r\n")).group("indent")
        indent = len(indent_text.expandtabs(8))
        parent_index: int | None = None
        parent_depth = -1
        for candidate_index, (candidate_start, candidate_end, candidate_kind, _) in enumerate(definitions):
            if candidate_index == index:
                continue
            if candidate_start < start < candidate_end:
                candidate_indent = len(
                    definition_pattern.match(masked_layout.lines[candidate_start].rstrip("\r\n")).group("indent").expandtabs(8)
                )
                if candidate_indent < indent and candidate_indent > parent_depth:
                    parent_index = candidate_index + 1
                    parent_depth = candidate_indent
        if parent_index is not None:
            parent_kind = definitions[parent_index - 1][2]
            if parent_kind == "class" and kind == "function":
                kind = "method"
        symbols.append(
            SymbolCandidate(
                name=name,
                kind=kind,
                lang="python",
                start_line=start + 1,
                end_line=end,
                start_byte=layout.starts[start],
                end_byte=end_byte_for_line(layout, end),
                parent_index=parent_index,
                attrs={"indent": indent, "extractor": "regex"},
            )
        )
    return symbols


# //// /抽取 Python 符号 ////


# //// 计算大括号符号范围 [@x380kkm 2026-07-24] ////
def brace_end_line(lines: Sequence[str], start: int) -> int:
    balance = 0
    opened = False
    for index in range(start, len(lines)):
        text = lines[index]
        masked = re.sub(r"//.*|/\*.*?\*/|\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'", "", text)
        balance += masked.count("{") - masked.count("}")
        if "{" in masked:
            opened = True
        if opened and balance <= 0:
            return index + 1
        if not opened and ";" in masked:
            return index + 1
    return len(lines)


# //// /计算大括号符号范围 ////


# //// 抽取 C/C++ 符号 [@x380kkm 2026-07-24] ////
def extract_cpp(path: str, content: str, layout: SourceLines) -> list[SymbolCandidate]:
    symbols = [module_symbol(path, "cpp", layout)]
    masked_content = mask_non_code(content, "cpp")
    masked_layout = source_lines(masked_content)
    declarations: list[tuple[int, int, str, str, int]] = []
    type_pattern = re.compile(
        r"^(?P<indent>[ \t]*)(?P<kind>class|struct|namespace|enum)[ \t]+(?P<name>[A-Za-z_]\w*)"
    )
    for index, raw_line in enumerate(masked_layout.lines):
        line = raw_line.rstrip("\r\n")
        type_match = type_pattern.match(line)
        if type_match:
            declarations.append(
                (index, brace_end_line(masked_layout.lines, index), type_match.group("kind"), type_match.group("name"), len(type_match.group("indent")))
            )
            continue
    function_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)[^;{}\n]*?"
        r"(?P<name>(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)\s*"
        r"\([^;{}]*\)\s*(?:const\b|noexcept\b|override\b|final\b|[ \t\r\n])*\{"
    )
    for function_match in function_pattern.finditer(masked_content):
        name = function_match.group("name").split("::")[-1]
        if name in CPP_DECLARATION_WORDS:
            continue
        index = content.count("\n", 0, function_match.start())
        indent = len(function_match.group("indent"))
        declarations.append((index, brace_end_line(masked_layout.lines, index), "function", name, indent))

    declarations.sort(key=lambda item: (item[0], item[1], item[3]))
    bounded: list[tuple[int, int, str, str, int]] = []
    for index, declaration in enumerate(declarations):
        start, end, kind, name, indent = declaration
        if index + 1 < len(declarations) and declarations[index + 1][0] > start:
            end = min(end, declarations[index + 1][0])
        bounded.append((start, end, kind, name, indent))
    declarations = bounded

    for index, (start, end, kind, name, indent) in enumerate(declarations):
        parent_index: int | None = None
        parent_size: int | None = None
        for candidate_index, (candidate_start, candidate_end, candidate_kind, _, candidate_indent) in enumerate(declarations):
            if candidate_index == index:
                continue
            candidate_size = candidate_end - candidate_start
            if candidate_start < start < candidate_end and (parent_size is None or candidate_size < parent_size):
                parent_index = candidate_index + 1
                parent_size = candidate_size
        if parent_index is not None and declarations[parent_index - 1][2] in {"class", "struct"} and kind == "function":
            kind = "method"
        symbols.append(
            SymbolCandidate(
                name=name,
                kind=kind,
                lang="cpp",
                start_line=start + 1,
                end_line=end,
                start_byte=layout.starts[start],
                end_byte=end_byte_for_line(layout, end),
                parent_index=parent_index,
                attrs={"indent": indent, "extractor": "regex"},
            )
        )
    return symbols


# //// /抽取 C/C++ 符号 ////


# //// 抽取 SQL 符号 [@x380kkm 2026-07-24] ////
def extract_sql(path: str, content: str, layout: SourceLines) -> list[SymbolCandidate]:
    symbols = [module_symbol(path, "sql", layout)]
    masked_content = mask_non_code(content, "sql")
    create_pattern = re.compile(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?P<kind>FUNCTION|PROCEDURE|VIEW|TABLE|TRIGGER|TYPE)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[A-Za-z_][\w$]*)",
        re.IGNORECASE,
    )
    for match in create_pattern.finditer(masked_content):
        start_line = content.count("\n", 0, match.start()) + 1
        semi = content.find(";", match.end())
        end_offset = len(content) if semi < 0 else semi + 1
        end_line = content.count("\n", 0, max(match.end(), end_offset)) + 1
        start_byte = len(content[: match.start()].encode("utf-8"))
        end_byte = len(content[:end_offset].encode("utf-8"))
        symbols.append(
            SymbolCandidate(
                name=match.group("name"),
                kind=match.group("kind").lower(),
                lang="sql",
                start_line=start_line,
                end_line=end_line,
                start_byte=start_byte,
                end_byte=end_byte,
                attrs={"extractor": "regex"},
            )
        )
    return symbols


# //// /抽取 SQL 符号 ////


# //// 抽取 Markdown 符号 [@x380kkm 2026-07-24] ////
def extract_markdown(path: str, content: str, layout: SourceLines) -> list[SymbolCandidate]:
    symbols = [module_symbol(path, "markdown", layout)]
    masked_layout = source_lines(mask_markdown_fences(content))
    headings: list[tuple[int, int, str]] = []
    heading_pattern = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<name>.+?)[ \t]*#*[ \t]*$")
    for index, raw_line in enumerate(masked_layout.lines):
        match = heading_pattern.match(raw_line.rstrip("\r\n"))
        if match:
            headings.append((index, len(match.group("marks")), normalize_name(match.group("name"))))
    for index, (start, level, name) in enumerate(headings):
        end = len(layout.lines)
        for next_start, next_level, _ in headings[index + 1 :]:
            if next_level <= level:
                end = next_start
                break
        parent_index: int | None = None
        for candidate_index in range(index - 1, -1, -1):
            candidate_start, candidate_level, _ = headings[candidate_index]
            if candidate_start < start and candidate_level < level:
                parent_index = candidate_index + 1
                break
        symbols.append(
            SymbolCandidate(
                name=name,
                kind="section",
                lang="markdown",
                start_line=start + 1,
                end_line=end,
                start_byte=layout.starts[start],
                end_byte=end_byte_for_line(layout, end),
                parent_index=parent_index,
                attrs={"level": level, "extractor": "regex"},
            )
        )
    return symbols


# //// /抽取 Markdown 符号 ////


# //// 选择语言抽取器 [@x380kkm 2026-07-24] ////
def extract_symbols(path: str, content: str, lang: str) -> list[SymbolCandidate]:
    layout = source_lines(content)
    if lang == "python":
        return extract_python(path, content, layout)
    if lang == "cpp":
        return extract_cpp(path, content, layout)
    if lang == "sql":
        return extract_sql(path, content, layout)
    if lang == "markdown":
        return extract_markdown(path, content, layout)
    return [module_symbol(path, lang, layout)]


# //// /选择语言抽取器 ////


# //// 抽取调用名称 [@x380kkm 2026-07-24] ////
def call_names(body: str, lang: str) -> Iterable[str]:
    body = mask_non_code(body, lang)
    pattern = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
    seen: set[str] = set()
    for match in pattern.finditer(body):
        name = match.group(1)
        if match.start(1) > 0 and body[match.start(1) - 1] == ".":
            continue
        if name.lower() in CALL_KEYWORDS or (lang == "python" and name in PYTHON_BUILTINS) or name in seen:
            continue
        seen.add(name)
        yield name


# //// /抽取调用名称 ////


# //// 规范化目标文件列表 [@x380kkm 2026-07-24] ////
def selected_paths(
    source_root: Path, requested: Sequence[str] | None
) -> list[str] | None:
    if not requested:
        return None
    values: list[str] = []
    for item in requested:
        values.extend(part for part in item.split(",") if part)
    result: list[str] = []
    for value in values:
        candidate = Path(value)
        if candidate.is_absolute():
            candidate = candidate.resolve().relative_to(source_root.resolve())
        result.append(candidate.as_posix())
    return result


# //// /规范化目标文件列表 ////


# //// 写入符号和关系 [@x380kkm 2026-07-24] ////
def enrich_database(
    source_root: Path,
    db_path: Path | None,
    paths: Sequence[str] | None = None,
    *,
    workspace_root: Path | None = None,
) -> dict[str, int]:
    db_path = resolve_cleanread_paths(
        source_root, workspace_root, db_path
    ).database_path
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    where = "" if not paths else " WHERE path IN (%s)" % ",".join("?" for _ in paths)
    rows = connection.execute("SELECT id, path, ext, content FROM files" + where, tuple(paths or ())).fetchall()
    file_ids = [row[0] for row in rows]
    if file_ids:
        marks = ",".join("?" for _ in file_ids)
        connection.execute(f"DELETE FROM edges WHERE file_id IN ({marks})", file_ids)
        connection.execute(f"DELETE FROM symbols WHERE file_id IN ({marks})", file_ids)

    symbol_count = edge_count = 0
    for file_id, path, extension, content in rows:
        lang = LANGUAGE_BY_EXTENSION.get(extension.lower(), "")
        if not lang:
            continue
        candidates = extract_symbols(path, content, lang)
        ids: list[int] = []
        for candidate in candidates:
            cursor = connection.execute(
                "INSERT INTO symbols(file_id,name,kind,lang,start_line,end_line,start_byte,end_byte,parent_id,attrs,provenance) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    file_id,
                    candidate.name,
                    candidate.kind,
                    candidate.lang,
                    candidate.start_line,
                    candidate.end_line,
                    candidate.start_byte,
                    candidate.end_byte,
                    None,
                    json.dumps(candidate.attrs, ensure_ascii=False, sort_keys=True),
                    json.dumps({"extractor": "lightweight-regex", "path": path}, ensure_ascii=False, sort_keys=True),
                ),
            )
            ids.append(int(cursor.lastrowid))
            symbol_count += 1
        for index, candidate in enumerate(candidates):
            parent_index = candidate.parent_index if candidate.parent_index is not None else (None if index == 0 else 0)
            parent_id = ids[parent_index] if parent_index is not None and 0 <= parent_index < len(ids) else None
            if parent_id is not None:
                connection.execute("UPDATE symbols SET parent_id=? WHERE id=?", (parent_id, ids[index]))
                connection.execute(
                    "INSERT INTO edges(file_id,src_symbol_id,dst_symbol_id,dst_name,relation) VALUES(?,?,?,?,?)",
                    (file_id, parent_id, ids[index], candidate.name, "contains"),
                )
                edge_count += 1
        name_to_id: dict[str, int] = {}
        for index, candidate in enumerate(candidates):
            name_to_id.setdefault(candidate.name, ids[index])
        for index, candidate in enumerate(candidates):
            if index == 0 or candidate.kind not in {"function", "method"}:
                continue
            body = content.encode("utf-8")[candidate.start_byte : candidate.end_byte].decode("utf-8", errors="replace")
            for name in call_names(body, lang):
                if name == candidate.name:
                    continue
                connection.execute(
                    "INSERT INTO edges(file_id,src_symbol_id,dst_symbol_id,dst_name,relation) VALUES(?,?,?,?,?)",
                    (file_id, ids[index], name_to_id.get(name), name, "calls"),
                )
                edge_count += 1
    connection.commit()
    connection.close()
    return {"files": len(rows), "symbols": symbol_count, "edges": edge_count}


# //// /写入符号和关系 ////


# //// 运行命令行入口 [@x380kkm 2026-07-24] ////
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--files", nargs="*")
    args = parser.parse_args(argv)
    paths = resolve_cleanread_paths(args.root, args.workspace_root, args.db)
    selected = selected_paths(paths.source_root, args.files)
    try:
        result = enrich_database(
            paths.source_root,
            paths.database_path,
            selected,
            workspace_root=paths.workspace_root,
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


# //// /运行命令行入口 ////


if __name__ == "__main__":
    raise SystemExit(main())
