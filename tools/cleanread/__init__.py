# audience: internal
# # cleanread
# 提供只读源码索引和运行工作区的共享路径实现.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# //// 表示源码根, 工作区根和数据库路径 [@x380kkm 2026-07-29] ////
@dataclass(frozen=True)
class CleanreadPaths:
    source_root: Path
    workspace_root: Path
    database_path: Path


# //// /表示源码根, 工作区根和数据库路径 ////


# //// 解析源码根, 工作区根和数据库路径 [@x380kkm 2026-07-29] ////
def resolve_cleanread_paths(
    source_root: Path,
    workspace_root: Path | None = None,
    db_path: Path | None = None,
) -> CleanreadPaths:
    resolved_source_root = Path(source_root).resolve()
    resolved_workspace_root = (
        Path(workspace_root).resolve()
        if workspace_root is not None
        else resolved_source_root
    )
    resolved_database_path = (
        Path(db_path).resolve()
        if db_path is not None
        else resolved_workspace_root / "cleanread" / "source.db"
    )
    return CleanreadPaths(
        source_root=resolved_source_root,
        workspace_root=resolved_workspace_root,
        database_path=resolved_database_path,
    )


# //// /解析源码根, 工作区根和数据库路径 ////


# //// 记录工作区内相对数据库路径 [@x380kkm 2026-07-29] ////
def get_recorded_database_path(workspace_root: Path, database_path: Path) -> str:
    resolved_workspace_root = Path(workspace_root).resolve()
    resolved_database_path = Path(database_path).resolve()
    try:
        return resolved_database_path.relative_to(resolved_workspace_root).as_posix()
    except ValueError:
        return str(resolved_database_path)


# //// /记录工作区内相对数据库路径 ////
