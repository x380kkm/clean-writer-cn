# audience: internal
# # rule-index
# 该模块把 4 个运行规则文件解析为内存数据.
# 命令只读取规则文件并输出 JSON, 不写入文件.

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATHS = (
    Path("modules/composition.md"),
    Path("modules/language.md"),
    Path("modules/structure.md"),
    Path("modules/format.md"),
)
SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$")
PROTECTED_CONTROL_TEXT = re.compile(
    r"`[^`\r\n]+`|https?://[^\s<>()]*[A-Za-z0-9/#]|\[[^\]]+\]\([^)]+\)"
)


# //// 定义规则模块和章节数据 [@x380kkm 2026-07-20] ////
@dataclass(frozen=True)
class ModuleDocument:
    name: str
    path: str
    purpose: str
    sections: tuple["RuleSection", ...]


@dataclass(frozen=True)
class RuleSection:
    module: str
    path: str
    heading: str
    anchor: str
    start_line: int
    end_line: int
    content: str


# //// /定义规则模块和章节数据 ////


# //// 生成稳定的 Markdown 章节锚点 [@x380kkm 2026-07-20] ////
def slugify_heading(heading: str) -> str:
    slug = re.sub(r"\s+", "-", heading.strip().casefold())
    slug = re.sub(r"[^\w\-\u4e00-\u9fff]", "", slug)
    return re.sub(r"-+", "-", slug).strip("-")


# //// /生成稳定的 Markdown 章节锚点 ////


# //// 读取一个运行规则模块 [@x380kkm 2026-07-20] ////
def read_module(relative_path: Path, root: Path = ROOT) -> ModuleDocument:
    path = root / relative_path
    lines = path.read_text(encoding="utf-8").splitlines()
    headings = [
        (line_number, match.group(1))
        for line_number, line in enumerate(lines, start=1)
        if (match := SECTION_HEADING.match(line))
    ]
    sections: list[RuleSection] = []
    anchor_counts: dict[str, int] = {}

    for index, (start_line, heading) in enumerate(headings):
        end_line = (
            headings[index + 1][0] - 1 if index + 1 < len(headings) else len(lines)
        )
        content = "\n".join(lines[start_line - 1 : end_line])
        base_anchor = slugify_heading(heading) or "section"
        occurrence = anchor_counts.get(base_anchor, 0)
        anchor_counts[base_anchor] = occurrence + 1
        anchor = base_anchor if occurrence == 0 else f"{base_anchor}-{occurrence}"
        sections.append(
            RuleSection(
                module=relative_path.stem,
                path=relative_path.as_posix(),
                heading=heading,
                anchor=anchor,
                start_line=start_line,
                end_line=end_line,
                content=content,
            )
        )

    purpose = lines[2].strip() if len(lines) >= 3 else ""
    return ModuleDocument(
        name=relative_path.stem,
        path=relative_path.as_posix(),
        purpose=purpose,
        sections=tuple(sections),
    )


# //// /读取一个运行规则模块 ////


# //// 读取全部运行规则模块 [@x380kkm 2026-07-20] ////
def read_modules(root: Path = ROOT) -> tuple[ModuleDocument, ...]:
    return tuple(read_module(relative_path, root) for relative_path in MODULE_PATHS)


# //// /读取全部运行规则模块 ////


# //// 列出运行规则模块 [@x380kkm 2026-07-20] ////
def list_modules(root: Path = ROOT) -> list[dict[str, object]]:
    return [
        {
            "name": document.name,
            "path": document.path,
            "purpose": document.purpose,
            "section_count": len(document.sections),
        }
        for document in sorted(read_modules(root), key=lambda item: item.path)
    ]


# //// /列出运行规则模块 ////


# //// 列出模块章节 [@x380kkm 2026-07-20] ////
def list_sections(
    module: str | None = None, root: Path = ROOT
) -> list[dict[str, object]]:
    sections = (
        section
        for document in read_modules(root)
        for section in document.sections
        if module is None or section.module == module
    )
    return [
        {
            "module": section.module,
            "path": section.path,
            "heading": section.heading,
            "anchor": section.anchor,
            "start_line": section.start_line,
            "end_line": section.end_line,
        }
        for section in sorted(sections, key=lambda item: (item.path, item.start_line))
    ]


# //// /列出模块章节 ////


# //// 按目标中文格式呈现控制文字 [@x380kkm 2026-07-22] ////
def render_control_text(content: str) -> str:
    protected: list[str] = []

    def replace_protected(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\ue000{len(protected) - 1}\ue001"

    rendered = PROTECTED_CONTROL_TEXT.sub(replace_protected, content)
    lines: list[str] = []
    for line in rendered.splitlines():
        if re.search(r"[\u3400-\u9fff]", line):
            line = re.sub(r"(?<!\d),(?!\d)", "，", line)
            line = re.sub(r"(?<!\d)\.(?!\d)", "。", line)
            line = re.sub(r"(?<!\d):(?!\d)", "：", line)
            line = line.replace(";", "；").replace("!", "！").replace("?", "？")
            line = line.replace("(", "（").replace(")", "）")
            if line.count('"') % 2 == 0:
                opening = True
                characters: list[str] = []
                for character in line:
                    if character == '"':
                        characters.append("“" if opening else "”")
                        opening = not opening
                    else:
                        characters.append(character)
                line = "".join(characters)
            line = re.sub(r"([，。；：！？])\s+", r"\1", line)
        lines.append(line)
    rendered = "\n".join(lines)

    for index, token in enumerate(protected):
        rendered = rendered.replace(f"\ue000{index}\ue001", token)
    return rendered


# //// /按目标中文格式呈现控制文字 ////


# //// 读取一个规则章节 [@x380kkm 2026-07-22] ////
def get_section(reference: str, root: Path = ROOT) -> dict[str, object]:
    if "#" not in reference:
        raise ValueError("章节引用必须包含 #")
    path_text, anchor = reference.split("#", 1)
    if not path_text or not anchor:
        raise ValueError("章节引用必须包含路径和锚点")
    relative_path = Path(path_text)
    if relative_path not in MODULE_PATHS:
        raise ValueError("章节引用必须指向运行模块")

    document = read_module(relative_path, root)
    for section in document.sections:
        if section.anchor != anchor:
            continue
        lines = section.content.splitlines()
        return {
            "module": section.module,
            "heading": section.heading,
            "reference": f"{section.path}#{section.anchor}",
            "control_text": True,
            "copy_to_output": False,
            "rendered_for": "zh-Hans",
            "content": render_control_text("\n".join(lines[1:]).strip()),
        }
    raise ValueError("章节引用无法定位")


# //// /读取一个规则章节 ////


# //// 搜索规则章节并返回行号 [@x380kkm 2026-07-20] ////
def search_index(
    query: str,
    root: Path = ROOT,
    module: str | None = None,
) -> list[dict[str, object]]:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("查询词不能为空")

    sections = sorted(
        (
            section
            for document in read_modules(root)
            for section in document.sections
            if module is None or section.module == module
        ),
        key=lambda item: (item.path, item.start_line),
    )
    results: list[dict[str, object]] = []
    folded_query = normalized_query.casefold()

    for section in sections:
        if (
            folded_query not in section.heading.casefold()
            and folded_query not in section.content.casefold()
        ):
            continue
        lines = section.content.splitlines()
        offset = next(
            (
                index
                for index, line in enumerate(lines)
                if folded_query in line.casefold()
            ),
            0,
        )
        line_number = section.start_line + offset
        results.append(
            {
                "module": section.module,
                "heading": section.heading,
                "anchor": f"{section.path}:{line_number}",
                "section_anchor": f"{section.path}#{section.anchor}",
                "excerpt": lines[offset].strip(),
            }
        )

    return results


# //// /搜索规则章节并返回行号 ////


# //// 校验命令行查询词 [@x380kkm 2026-07-20] ////
def parse_query(value: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise argparse.ArgumentTypeError("查询词不能为空")
    return normalized_value


# //// /校验命令行查询词 ////


# //// 解析只读索引命令 [@x380kkm 2026-07-20] ////
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查询简体中文写作规则")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("modules", help="列出运行规则模块")

    sections_parser = subparsers.add_parser("sections", help="列出规则章节")
    sections_parser.add_argument(
        "--module", choices=[path.stem for path in MODULE_PATHS]
    )

    search_parser = subparsers.add_parser("search", help="按词语搜索规则")
    search_parser.add_argument("query", type=parse_query)
    search_parser.add_argument(
        "--module", choices=[path.stem for path in MODULE_PATHS]
    )

    section_parser = subparsers.add_parser("section", help="读取一个规则章节")
    section_parser.add_argument("reference", type=parse_query)

    bundle_parser = subparsers.add_parser("bundle", help="读取一组规则章节")
    bundle_parser.add_argument("references", nargs="+", type=parse_query)
    return parser


# //// /解析只读索引命令 ////


# //// 输出只读索引查询结果 [@x380kkm 2026-07-20] ////
def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "modules":
        result = list_modules()
    elif arguments.command == "sections":
        result = list_sections(arguments.module)
    elif arguments.command == "section":
        result = get_section(arguments.reference)
    elif arguments.command == "bundle":
        result = [get_section(reference) for reference in arguments.references]
    else:
        result = search_index(arguments.query, module=arguments.module)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# //// /输出只读索引查询结果 ////


if __name__ == "__main__":
    raise SystemExit(main())
