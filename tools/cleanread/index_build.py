# audience: internal
# # cleanread-index-build
# 建立或增量更新源码 SQLite 索引.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

if __package__:
    from . import resolve_cleanread_paths
else:
    from __init__ import resolve_cleanread_paths


LANGUAGE_EXTENSIONS = {
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
    ".glsl": "glsl",
}
DEFAULT_LANGUAGES = frozenset(LANGUAGE_EXTENSIONS.values())
DEFAULT_IGNORE_PARTS = frozenset(
    {".git", ".hg", ".svn", "__pycache__", "node_modules", "archive", "cleanread"}
)


# //// 规范化索引路径 [@x380kkm 2026-07-24] ////
def relative_path(source_root: Path, path: Path) -> str:
    return path.resolve().relative_to(source_root.resolve()).as_posix()


# //// /规范化索引路径 ////


# //// 判断文件是否应被忽略 [@x380kkm 2026-07-24] ////
def should_ignore(source_root: Path, path: Path) -> bool:
    try:
        parts = path.resolve().relative_to(source_root.resolve()).parts
    except ValueError:
        return True
    return any(part in DEFAULT_IGNORE_PARTS or part.startswith(".") for part in parts)


# //// /判断文件是否应被忽略 ////


# //// 读取索引配置 [@x380kkm 2026-07-24] ////
def configured_languages(source_root: Path, requested: str | None) -> set[str]:
    if requested:
        return {item.strip() for item in requested.split(",") if item.strip()}
    config_path = source_root / "cleanread" / "cleanread.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            config = {}
        languages = config.get("languages")
        if isinstance(languages, list):
            configured = {item for item in languages if isinstance(item, str)}
            if configured:
                return configured
    return set(DEFAULT_LANGUAGES)


# //// /读取索引配置 ////


# //// 枚举源码文件 [@x380kkm 2026-07-24] ////
def source_files(
    source_root: Path,
    languages: set[str],
    requested: set[str] | None = None,
) -> Iterable[Path]:
    if requested is not None:
        for relative in sorted(requested):
            path = (source_root / relative).resolve()
            try:
                path.relative_to(source_root.resolve())
            except ValueError:
                continue
            if path.is_file() and LANGUAGE_EXTENSIONS.get(path.suffix.lower()) in languages:
                yield path
        return
    for path in source_root.rglob("*"):
        if not path.is_file() or should_ignore(source_root, path):
            continue
        if LANGUAGE_EXTENSIONS.get(path.suffix.lower()) in languages:
            yield path


# //// 收集 Git 改动路径 [@x380kkm 2026-07-24] ////
def git_changed_paths(source_root: Path) -> set[str]:
    """只返回 Git 当前状态涉及的相对路径, 包括已删除路径."""
    commands = [
        ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", "HEAD", "--"],
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    ]
    changed: set[str] = set()
    for command in commands:
        result = subprocess.run(
            [command[0], "-C", str(source_root), *command[1:]],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "无法读取 Git 改动路径")
        if command[1] == "status":
            fields = result.stdout.split("\0")
            for field in fields:
                if not field:
                    continue
                path = field[3:] if len(field) >= 3 else field
                if " -> " in path:
                    path = path.rsplit(" -> ", 1)[-1]
                changed.add(Path(path).as_posix())
        else:
            changed.update(
                Path(item).as_posix()
                for item in result.stdout.splitlines()
                if item.strip()
            )
    return changed


# //// /收集 Git 改动路径 ////


# //// /枚举源码文件 ////


# //// 建立索引表 [@x380kkm 2026-07-24] ////
def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            sha256 TEXT NOT NULL,
            content TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            lang TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            start_byte INTEGER NOT NULL,
            end_byte INTEGER NOT NULL,
            parent_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
            attrs TEXT NOT NULL,
            provenance TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            src_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
            dst_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
            dst_name TEXT NOT NULL,
            relation TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols(name);
        CREATE INDEX IF NOT EXISTS symbols_file_idx ON symbols(file_id);
        CREATE INDEX IF NOT EXISTS edges_dst_name_idx ON edges(dst_name);
        """
    )
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5("
            "path UNINDEXED, content, tokenize='trigram'"
            ")"
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('fts_mode', 'fts5')"
        )
    except sqlite3.OperationalError:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS files_fts(path TEXT PRIMARY KEY, content TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('fts_mode', 'table')"
        )


# //// /建立索引表 ////


# //// 判断索引架构是否兼容 [@x380kkm 2026-07-24] ////
def schema_is_current(connection: sqlite3.Connection) -> bool:
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(files)")}
    except sqlite3.Error:
        return False
    return {"path", "ext", "size", "mtime", "sha256", "content"}.issubset(columns)


# //// /判断索引架构是否兼容 ////


# //// 删除旧索引表 [@x380kkm 2026-07-24] ////
def reset_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS files_fts;
        DROP TABLE IF EXISTS edges;
        DROP TABLE IF EXISTS symbols;
        DROP TABLE IF EXISTS files;
        DROP TABLE IF EXISTS metadata;
        """
    )


# //// /删除旧索引表 ////


# //// 更新全文索引 [@x380kkm 2026-07-24] ////
def update_fts(connection: sqlite3.Connection, path: str, content: str) -> None:
    mode = connection.execute(
        "SELECT value FROM metadata WHERE key='fts_mode'"
    ).fetchone()
    connection.execute("DELETE FROM files_fts WHERE path = ?", (path,))
    if mode and mode[0] == "fts5":
        connection.execute("INSERT INTO files_fts(path, content) VALUES(?, ?)", (path, content))
    else:
        connection.execute("INSERT OR REPLACE INTO files_fts(path, content) VALUES(?, ?)", (path, content))


# //// /更新全文索引 ////


# //// 建立或更新源码索引 [@x380kkm 2026-07-24] ////
def build_index(
    source_root: Path,
    db_path: Path | None,
    languages: set[str],
    initialize: bool,
    requested_paths: set[str] | None = None,
    *,
    workspace_root: Path | None = None,
) -> dict[str, object]:
    paths = resolve_cleanread_paths(source_root, workspace_root, db_path)
    source_root = paths.source_root
    db_path = paths.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    if initialize or not schema_is_current(connection):
        reset_schema(connection)
    create_schema(connection)
    if initialize:
        connection.execute("DELETE FROM edges")
        connection.execute("DELETE FROM symbols")
        connection.execute("DELETE FROM files")
        connection.execute("DELETE FROM files_fts")

    existing = {
        row[0]: (row[1], row[2], row[3])
        for row in connection.execute("SELECT path, size, mtime, sha256 FROM files")
    }
    seen: set[str] = set()
    added = updated = skipped = 0
    errors: list[dict[str, str]] = []
    for path in source_files(source_root, languages, requested_paths):
        rel = relative_path(source_root, path)
        seen.add(rel)
        try:
            data = path.read_bytes()
            content = data.decode("utf-8")
            stat = path.stat()
        except (OSError, UnicodeError) as error:
            errors.append({"path": rel, "error": str(error)})
            continue
        digest = hashlib.sha256(data).hexdigest()
        old = existing.get(rel)
        if old and old == (len(data), stat.st_mtime, digest):
            skipped += 1
            continue
        if old:
            connection.execute("DELETE FROM edges WHERE file_id=(SELECT id FROM files WHERE path=?)", (rel,))
            connection.execute("DELETE FROM symbols WHERE file_id=(SELECT id FROM files WHERE path=?)", (rel,))
            connection.execute(
                "UPDATE files SET ext=?, size=?, mtime=?, sha256=?, content=? WHERE path=?",
                (path.suffix.lower(), len(data), stat.st_mtime, digest, content, rel),
            )
            updated += 1
        else:
            connection.execute(
                "INSERT INTO files(path, ext, size, mtime, sha256, content) VALUES(?, ?, ?, ?, ?, ?)",
                (rel, path.suffix.lower(), len(data), stat.st_mtime, digest, content),
            )
            added += 1
        update_fts(connection, rel, content)

    removed = 0
    removable = set(existing) - seen if requested_paths is None else set(existing).intersection(requested_paths) - seen
    for rel in removable:
        connection.execute("DELETE FROM files WHERE path=?", (rel,))
        connection.execute("DELETE FROM files_fts WHERE path=?", (rel,))
        removed += 1
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('root',?)",
        (str(source_root),),
    )
    connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('languages',?)", (json.dumps(sorted(languages)),))
    connection.commit()
    connection.close()
    return {
        "root": str(source_root),
        "source_root": str(source_root),
        "workspace_root": str(paths.workspace_root),
        "db": str(db_path),
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "removed": removed,
        "errors": errors,
    }


# //// /建立或更新源码索引 ////


# //// 运行索引命令 [@x380kkm 2026-07-24] ////
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--langs")
    parser.add_argument("--files", nargs="*")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--incremental", action="store_true")
    args = parser.parse_args(argv)
    if args.init and args.incremental:
        parser.error("--init 与 --incremental 不能同时使用")
    if args.files and args.incremental:
        parser.error("--files 与 --incremental 不能同时使用")
    try:
        paths = resolve_cleanread_paths(args.root, args.workspace_root, args.db)
        requested = None
        if args.files:
            requested = set()
            for value in args.files:
                candidate = Path(value)
                if candidate.is_absolute():
                    candidate = candidate.resolve().relative_to(paths.source_root)
                requested.add(candidate.as_posix())
        if args.incremental:
            requested = git_changed_paths(paths.source_root)
        result = build_index(
            paths.source_root,
            paths.database_path,
            configured_languages(paths.source_root, args.langs),
            args.init,
            requested,
            workspace_root=paths.workspace_root,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["errors"] else 0


# //// /运行索引命令 ////


if __name__ == "__main__":
    raise SystemExit(main())
