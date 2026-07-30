# audience: internal
# # cleanread-query
# 对源码索引执行只读 SQL 查询.

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Sequence

if __package__:
    from . import get_recorded_database_path, resolve_cleanread_paths
else:
    from __init__ import get_recorded_database_path, resolve_cleanread_paths


# //// 注册按字节切片函数 [@x380kkm 2026-07-24] ////
def slice_bytes(content: str, start: int, length: int) -> str:
    data = content.encode("utf-8")
    return data[max(start, 0) : max(start, 0) + max(length, 0)].decode(
        "utf-8", errors="replace"
    )


# //// /注册按字节切片函数 ////


# //// 判断 SQL 是否只读 [@x380kkm 2026-07-24] ////
def ensure_read_only(sql: str) -> str:
    statement = sql.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if ";" in statement or re.match(r"(?is)^(select|with|pragma|explain)\b", statement) is None:
        raise ValueError("只允许单条只读 SELECT, WITH, PRAGMA 或 EXPLAIN 查询")
    return statement


# //// /判断 SQL 是否只读 ////


# //// 执行只读查询 [@x380kkm 2026-07-24] ////
def run_query(db_path: Path, sql: str) -> list[dict[str, object]]:
    statement = ensure_read_only(sql)
    connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        connection.create_function("slice_bytes", 3, slice_bytes)
        connection.execute("PRAGMA query_only = ON")
        cursor = connection.execute(statement)
        columns = [item[0] for item in cursor.description or ()]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


# //// /执行只读查询 ////


# //// 记录查询轨迹 [@x380kkm 2026-07-24] ////
def log_query(
    workspace_root: Path,
    sql: str,
    row_count: int,
    db_path: Path | None = None,
) -> str:
    workspace_root = workspace_root.resolve()
    trace_dir = workspace_root / "cleanread" / "short" / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    query_id = f"q-{uuid.uuid4().hex}"
    index_sha256 = ""
    index_path = ""
    if db_path is not None:
        resolved_db = db_path.resolve()
        index_path = get_recorded_database_path(workspace_root, resolved_db)
        index_sha256 = hashlib.sha256(resolved_db.read_bytes()).hexdigest()
    record = {
        "id": query_id,
        "sql": sql,
        "rows": row_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "index_path": index_path,
        "index_sha256": index_sha256,
    }
    with (trace_dir / "queries.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return query_id


# //// /记录查询轨迹 ////


# //// 运行查询命令 [@x380kkm 2026-07-24] ////
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("sql")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args(argv)
    paths = resolve_cleanread_paths(args.root, args.workspace_root, args.db)
    try:
        rows = run_query(paths.database_path, args.sql)
        if not args.no_log:
            log_query(
                paths.workspace_root,
                args.sql,
                len(rows),
                paths.database_path,
            )
    except (OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(rows, ensure_ascii=False))
    return 0


# //// /运行查询命令 ////


if __name__ == "__main__":
    raise SystemExit(main())
