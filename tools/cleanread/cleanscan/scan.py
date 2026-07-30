# audience: internal
# # cleanscan-scan
# 在 cleanread SQLite 索引上执行有界的关系扫描.

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Sequence

if __package__:
    from .. import resolve_cleanread_paths
else:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from __init__ import resolve_cleanread_paths


DEFAULT_LIMIT = 100


# //// 检查扫描结果上限 [@x380kkm 2026-07-24] ////
def validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是正整数")
    return limit


# //// /检查扫描结果上限 ////


# //// 规范化接口目录 [@x380kkm 2026-07-24] ////
def normalize_target_root(target_root: str) -> str:
    if not isinstance(target_root, str):
        raise ValueError("target_root 必须是相对目录")
    raw = target_root.replace("\\", "/").strip()
    if (
        not raw
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:/", raw) is not None
    ):
        raise ValueError("target_root 必须是非空相对目录")
    normalized = raw.strip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = PurePosixPath(normalized).parts
    if not normalized or normalized in {".", ".."} or ".." in parts:
        raise ValueError("target_root 不能超出索引根目录")
    return normalized


# //// /规范化接口目录 ////


# //// 转义接口目录查询 [@x380kkm 2026-07-24] ////
def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# //// /转义接口目录查询 ////


def database_path(store: Path) -> Path:
    store = store.resolve()
    if store.is_file():
        return store
    return store / "source.db"


# //// 解析扫描使用的数据库路径 [@x380kkm 2026-07-29] ////
def resolve_scan_database_path(
    source_root: Path | None = None,
    workspace_root: Path | None = None,
    db_path: Path | None = None,
    store: Path | None = None,
) -> Path:
    if db_path is not None:
        return Path(db_path).resolve()
    if store is not None:
        return database_path(store)
    effective_source_root = source_root or workspace_root
    if effective_source_root is None:
        raise ValueError(
            "扫描需要 --db, --store, --root 或 --workspace-root"
        )
    return resolve_cleanread_paths(
        effective_source_root, workspace_root
    ).database_path


# //// /解析扫描使用的数据库路径 ////


def connect(store: Path) -> sqlite3.Connection:
    path = database_path(store)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def anchor(row: sqlite3.Row) -> str:
    return f"{row['path']}:{row['start_line']}"


def symbol_rows(connection: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT symbols.id, symbols.name, symbols.kind, symbols.lang,
                  symbols.start_line, symbols.end_line, files.path
           FROM symbols JOIN files ON files.id=symbols.file_id
           WHERE symbols.name=? ORDER BY files.path, symbols.start_line, symbols.id""",
        (name,),
    ).fetchall()


def impact(store: Path, name: str, limit: int) -> dict[str, object]:
    validate_limit(limit)
    connection = connect(store)
    try:
        targets = symbol_rows(connection, name)
        target_ids = [int(row["id"]) for row in targets]
        if not target_ids:
            return {"query": name, "status": "unresolved", "targets": [], "results": [], "truncated": False}
        placeholders = ",".join("?" * len(target_ids))
        rows = connection.execute(
            f"""SELECT source.name AS source_name, source.kind AS source_kind,
                       source.start_line, source.end_line, source_file.path,
                       edges.dst_name, edges.relation
                FROM edges
                JOIN symbols AS source ON source.id=edges.src_symbol_id
                JOIN files AS source_file ON source_file.id=source.file_id
                WHERE edges.dst_symbol_id IN ({placeholders})
                   OR (edges.dst_symbol_id IS NULL AND edges.dst_name=? AND edges.relation IN ('calls','uses_type','imports'))
                ORDER BY source_file.path, source.start_line, source.name""",
            (*target_ids, name),
        ).fetchall()
        results = [
            {
                "symbol": row["source_name"],
                "kind": row["source_kind"],
                "relation": row["relation"],
                "target": row["dst_name"],
                "anchor": anchor(row),
            }
            for row in rows
        ]
        truncated = len(results) > limit
        return {
            "query": name,
            "status": "ambiguous" if len(targets) > 1 else "unique",
            "targets": [
                {"symbol": row["name"], "kind": row["kind"], "anchor": anchor(row)}
                for row in targets
            ],
            "results": results[:limit],
            "truncated": truncated,
        }
    finally:
        connection.close()


def interface(store: Path, target_root: str, limit: int) -> dict[str, object]:
    validate_limit(limit)
    normalized = normalize_target_root(target_root)
    escaped_root = escape_like(normalized)
    connection = connect(store)
    try:
        rows = connection.execute(
            """SELECT source.name AS source_name, source.kind AS source_kind,
                      source.start_line, source_file.path, edges.dst_name,
                      edges.relation, target.name AS target_name, target.id AS target_id,
                      target_file.path AS target_path
               FROM edges
               JOIN symbols AS source ON source.id=edges.src_symbol_id
               JOIN files AS source_file ON source_file.id=source.file_id
               LEFT JOIN symbols AS target ON target.id=edges.dst_symbol_id
               LEFT JOIN files AS target_file ON target_file.id=target.file_id
               WHERE source_file.path=? OR source_file.path LIKE ? ESCAPE '\\'
               ORDER BY source_file.path, source.start_line, source.name""",
            (normalized, escaped_root + "/%"),
        ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            target_path = row["target_path"]
            if target_path is None:
                target_name = row["target_name"] or row["dst_name"]
                candidate_count = 0
                if target_name:
                    candidate_count = connection.execute(
                        "SELECT count(*) FROM symbols WHERE name=?", (target_name,)
                    ).fetchone()[0]
                status = "ambiguous" if candidate_count > 1 else "unresolved"
            elif target_path == normalized or str(target_path).startswith(normalized + "/"):
                continue
            else:
                status = "unique"
            results.append(
                {
                    "symbol": row["source_name"],
                    "kind": row["source_kind"],
                    "relation": row["relation"],
                    "target": row["target_name"] or row["dst_name"],
                    "status": status,
                    "anchor": anchor(row),
                }
            )
        truncated = len(results) > limit
        return {"target_root": normalized, "results": results[:limit], "truncated": truncated}
    finally:
        connection.close()


def seam(store: Path, name: str, limit: int) -> dict[str, object]:
    validate_limit(limit)
    connection = connect(store)
    try:
        targets = symbol_rows(connection, name)
        if not targets:
            return {"query": name, "status": "unresolved", "results": [], "truncated": False}
        results: list[dict[str, object]] = []
        for target in targets:
            symbol_id = int(target["id"])
            incoming = connection.execute(
                "SELECT relation, count(*) AS count FROM edges WHERE dst_symbol_id=? GROUP BY relation",
                (symbol_id,),
            ).fetchall()
            outgoing = connection.execute(
                "SELECT relation, count(*) AS count FROM edges WHERE src_symbol_id=? GROUP BY relation",
                (symbol_id,),
            ).fetchall()
            results.append(
                {
                    "symbol": target["name"],
                    "kind": target["kind"],
                    "anchor": anchor(target),
                    "incoming": {row["relation"]: row["count"] for row in incoming},
                    "outgoing": {row["relation"]: row["count"] for row in outgoing},
                }
            )
        truncated = len(results) > limit
        return {
            "query": name,
            "status": "ambiguous" if len(targets) > 1 else "unique",
            "results": results[:limit],
            "truncated": truncated,
        }
    finally:
        connection.close()


# //// 添加扫描数据库参数 [@x380kkm 2026-07-29] ////
def add_database_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--store", type=Path)


# //// /添加扫描数据库参数 ////


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("impact", "seam"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("symbol")
        add_database_arguments(subparser)
        subparser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    interface_parser = subparsers.add_parser("interface")
    interface_parser.add_argument("--target-root", required=True)
    add_database_arguments(interface_parser)
    interface_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit 必须是正数")
    try:
        database = resolve_scan_database_path(
            args.root,
            args.workspace_root,
            args.db,
            args.store,
        )
        if args.command == "impact":
            result = impact(database, args.symbol, args.limit)
        elif args.command == "interface":
            result = interface(database, args.target_root, args.limit)
        else:
            result = seam(database, args.symbol, args.limit)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
