# audience: internal
# # cleanread-trace
# 管理跨回合查询定位记录, 不把旧查询结果当作新的源码证据.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

if __package__:
    from . import get_recorded_database_path, resolve_cleanread_paths
else:
    from __init__ import get_recorded_database_path, resolve_cleanread_paths

TRACE_DIRECTORY = Path("cleanread") / "short" / "traces"
DYNAMIC_NAME = "queries.jsonl"
KEPT_NAME = "kept.jsonl"
STATUSES = frozenset({"active", "kept", "shelved"})
SHA256_PATTERN = r"^[0-9a-f]{64}$"


# //// 确定查询轨迹文件位置 [@x380kkm 2026-07-24] ////
def trace_file(workspace_root: Path, name: str) -> Path:
    return workspace_root.resolve() / TRACE_DIRECTORY / name


# //// /确定查询轨迹文件位置 ////


# //// 规范化历史查询记录 [@x380kkm 2026-07-24] ////
def normalize_record(raw: object, index: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"查询轨迹第 {index} 项不是对象")
    sql = raw.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError(f"查询轨迹第 {index} 项缺少有效 SQL")
    query_id = raw.get("id")
    if not isinstance(query_id, str) or not query_id.strip():
        digest = hashlib.sha256(f"{index}\n{sql}".encode("utf-8")).hexdigest()[:16]
        query_id = f"legacy-{digest}"
    status = raw.get("status", "active")
    if status not in STATUSES:
        raise ValueError(f"查询轨迹第 {index} 项包含无效状态: {status}")
    rows = raw.get("rows", 0)
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise ValueError(f"查询轨迹第 {index} 项包含无效行数")
    created_at = raw.get("created_at", "")
    if not isinstance(created_at, str):
        raise ValueError(f"查询轨迹第 {index} 项包含无效时间")
    index_sha256 = raw.get("index_sha256", "")
    if not isinstance(index_sha256, str) or (
        index_sha256 and re.fullmatch(SHA256_PATTERN, index_sha256.lower()) is None
    ):
        raise ValueError(f"查询轨迹第 {index} 项包含无效索引摘要")
    index_path = raw.get("index_path", "")
    if not isinstance(index_path, str):
        raise ValueError(f"查询轨迹第 {index} 项包含无效索引路径")
    return {
        "id": query_id.strip(),
        "sql": sql,
        "rows": rows,
        "created_at": created_at,
        "status": status,
        "index_path": index_path,
        "index_sha256": index_sha256.lower(),
    }


# //// /规范化历史查询记录 ////


# //// 读取查询轨迹 [@x380kkm 2026-07-24] ////
def read_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"无法读取查询轨迹: {path}") from error
    seen_ids: set[str] = set()
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"查询轨迹第 {index} 行不是有效 JSON") from error
        record = normalize_record(raw, index)
        if record["id"] in seen_ids:
            raise ValueError(f"查询轨迹包含重复标识: {record['id']}")
        seen_ids.add(str(record["id"]))
        records.append(record)
    return records


# //// /读取查询轨迹 ////


# //// 原子写入查询轨迹 [@x380kkm 2026-07-24] ////
def write_records(path: Path, records: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            stream.write(payload)
            temporary_path = stream.name
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


# //// /原子写入查询轨迹 ////


# //// 预检历史定位查询 [@x380kkm 2026-07-24] ////
def preflight(
    source_root: Path,
    sql: str,
    limit: int = 20,
    db_path: Path | None = None,
    *,
    workspace_root: Path | None = None,
) -> list[dict[str, object]]:
    matches, _ = preflight_details(
        source_root,
        sql,
        limit,
        db_path,
        workspace_root=workspace_root,
    )
    return matches


# //// 确定当前索引路径 [@x380kkm 2026-07-24] ////
def current_index_path(
    source_root: Path,
    db_path: Path | None = None,
    *,
    workspace_root: Path | None = None,
) -> tuple[Path, str]:
    paths = resolve_cleanread_paths(source_root, workspace_root, db_path)
    recorded_path = get_recorded_database_path(
        paths.workspace_root, paths.database_path
    )
    return paths.database_path, recorded_path


# //// 计算当前索引摘要 [@x380kkm 2026-07-24] ////
def current_index_sha256(
    source_root: Path,
    db_path: Path | None = None,
    *,
    workspace_root: Path | None = None,
) -> str | None:
    index_path, _ = current_index_path(
        source_root, db_path, workspace_root=workspace_root
    )
    if not index_path.is_file():
        return None
    try:
        return hashlib.sha256(index_path.read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError(f"无法读取当前源码索引: {index_path}") from error


# //// /计算当前索引摘要 ////


# //// 预检查询并区分过时记录 [@x380kkm 2026-07-24] ////
def preflight_details(
    source_root: Path,
    sql: str,
    limit: int = 20,
    db_path: Path | None = None,
    *,
    workspace_root: Path | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("预检需要非空 SQL")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("预检 limit 必须是正整数")
    matches: list[dict[str, object]] = []
    stale: list[dict[str, object]] = []
    seen_records: dict[str, dict[str, object]] = {}
    paths = resolve_cleanread_paths(source_root, workspace_root, db_path)
    _, recorded_index_path = current_index_path(
        paths.source_root,
        paths.database_path,
        workspace_root=paths.workspace_root,
    )
    index_sha256 = current_index_sha256(
        paths.source_root,
        paths.database_path,
        workspace_root=paths.workspace_root,
    )
    for track, name in (("kept", KEPT_NAME), ("dynamic", DYNAMIC_NAME)):
        for record in read_records(trace_file(paths.workspace_root, name)):
            query_id = str(record["id"])
            previous = seen_records.get(query_id)
            if previous is not None:
                if previous["sql"] != record["sql"]:
                    raise ValueError(f"查询轨迹标识对应多个 SQL: {query_id}")
                continue
            seen_records[query_id] = record
            if record["status"] == "shelved" or record["sql"] != sql:
                continue
            result = {**record, "track": track}
            if not index_sha256:
                result["stale_reason"] = "缺少当前索引摘要"
                stale.append(result)
            elif not record["index_sha256"]:
                result["stale_reason"] = "记录缺少索引摘要"
                stale.append(result)
            elif record["index_path"] != recorded_index_path:
                result["stale_reason"] = "索引路径变化"
                stale.append(result)
            elif record["index_sha256"] != index_sha256:
                result["stale_reason"] = "源码索引已变化"
                stale.append(result)
            else:
                matches.append(result)
            if len(matches) >= limit:
                return matches, stale
    return matches, stale


# //// /预检历史定位查询 ////


# //// 更新查询轨迹状态 [@x380kkm 2026-07-24] ////
def update_status(workspace_root: Path, query_id: str, status: str) -> bool:
    if status not in {"kept", "shelved"}:
        raise ValueError("状态只能是 kept 或 shelved")
    path = trace_file(workspace_root, DYNAMIC_NAME)
    records = read_records(path)
    changed = False
    for record in records:
        if record["id"] == query_id:
            record["status"] = status
            changed = True
    if not changed:
        return False
    write_records(path, records)
    kept_path = trace_file(workspace_root, KEPT_NAME)
    kept_records = read_records(kept_path)
    if status == "kept":
        if not any(record["id"] == query_id for record in kept_records):
            kept_records.append(next(record for record in records if record["id"] == query_id))
            write_records(kept_path, kept_records)
    else:
        filtered_kept = [record for record in kept_records if record["id"] != query_id]
        if len(filtered_kept) != len(kept_records):
            write_records(kept_path, filtered_kept)
    return True


# //// /更新查询轨迹状态 ////


# //// 清理已搁置查询 [@x380kkm 2026-07-24] ////
def clear_shelved(workspace_root: Path) -> int:
    path = trace_file(workspace_root, DYNAMIC_NAME)
    records = read_records(path)
    kept_records = [record for record in records if record["status"] != "shelved"]
    removed = len(records) - len(kept_records)
    if removed:
        write_records(path, kept_records)
    return removed


# //// /清理已搁置查询 ////


# //// 运行查询轨迹命令 [@x380kkm 2026-07-24] ////
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--workspace-root", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("sql")
    preflight_parser.add_argument("--limit", type=int, default=20)
    preflight_parser.add_argument("--db", type=Path)

    status_parser = subparsers.add_parser("keep")
    status_parser.add_argument("id")
    status_parser = subparsers.add_parser("shelve")
    status_parser.add_argument("id")

    subparsers.add_parser("clear")
    args = parser.parse_args(argv)
    paths = resolve_cleanread_paths(
        args.root,
        args.workspace_root,
        args.db if args.command == "preflight" else None,
    )
    try:
        if args.command == "preflight":
            matches, stale = preflight_details(
                paths.source_root,
                args.sql,
                args.limit,
                paths.database_path,
                workspace_root=paths.workspace_root,
            )
            result: object = {
                "matched": bool(matches),
                "records": matches,
                "stale": stale,
            }
        elif args.command == "keep":
            result = {
                "updated": update_status(paths.workspace_root, args.id, "kept")
            }
        elif args.command == "shelve":
            result = {
                "updated": update_status(paths.workspace_root, args.id, "shelved")
            }
        else:
            result = {"removed": clear_shelved(paths.workspace_root)}
    except (OSError, ValueError, StopIteration) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


# //// /运行查询轨迹命令 ////


if __name__ == "__main__":
    raise SystemExit(main())
