#!/usr/bin/env python3
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import mysql.connector as mc
from mcp.server.fastmcp import FastMCP


server = FastMCP("yooztech_mcp_mysql")


class MySQLGuard:
    """封装连接、白名单校验与库推断，支持只读查询。"""

    def __init__(self) -> None:
        self.host = os.getenv("DB_HOST", "127.0.0.1")
        self.port = int(os.getenv("DB_PORT", "3306"))
        self.user = os.getenv("DB_USER", "root")
        self.password = os.getenv("DB_PASS", "")
        # 不再支持/读取 DB_NAME，全靠运行时推断或工具入参
        self.inferred_db: Optional[str] = None

        self._schema_cache: Dict[str, List[str]] = {}

        self._conn = mc.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=None,
            autocommit=False,
        )

    def _non_system_databases(self) -> List[str]:
        """列出非系统库，用于自动解析可访问库。
        注意：这里基于 schemata 过滤系统库名；并不精准检查权限，但在只读账号下一般可用。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT SCHEMA_NAME
                FROM information_schema.SCHEMATA
                WHERE SCHEMA_NAME NOT IN ('mysql','information_schema','performance_schema','sys')
                ORDER BY SCHEMA_NAME
                """
            )
            return [r[0] for r in cur.fetchall()]

    def _resolve_database(self, db: Optional[str]) -> str:
        if db:
            return db
        if self.inferred_db:
            return self.inferred_db
        # 自动推断一次（基于当前工作目录）
        guessed, _ = self._infer_database_internal(os.getcwd())
        if guessed:
            self.inferred_db = guessed
            return guessed
        # 回退：若仅有一个非系统库，直接使用
        candidates = self._non_system_databases()
        if len(candidates) == 1:
            self.inferred_db = candidates[0]
            return candidates[0]
        raise ValueError("存在多个可访问库且无法从项目中推断，请在参数中指定 db 或先调用 infer_database 工具")

    # --- 推断逻辑 ---
    def _extract_db_hints_from_text(self, text: str) -> List[str]:
        """从文本中提取可能的数据库名（简单启发式）。"""
        import re

        hints: List[str] = []
        # 常见 .env 键
        for key in [
            "MYSQL_DATABASE",
            "DB_NAME",
            "DATABASE_NAME",
            "MYSQL_DB",
        ]:
            m = re.search(rf"{key}\s*=\s*([A-Za-z0-9_\-]+)", text)
            if m:
                hints.append(m.group(1))

        # JDBC / URL 形式 .../(dbname)?
        for pat in [
            r"jdbc:mysql://[^/\s]+/([A-Za-z0-9_\-]+)",
            r"mysql:\/\/[^/\s]+/([A-Za-z0-9_\-]+)",
        ]:
            for m in re.finditer(pat, text, re.IGNORECASE):
                hints.append(m.group(1))

        return list(dict.fromkeys(hints))  # 去重且保序

    def _infer_database_internal(self, project_root: Optional[str]) -> Tuple[Optional[str], Dict[str, Any]]:
        """从项目目录推断数据库名，返回 (db, 证据)。"""
        if not project_root:
            project_root = os.getcwd()

        candidates = self._non_system_databases()
        evidence: Dict[str, Any] = {"candidates": candidates, "matches": []}
        if not candidates:
            return None, evidence

        # 仅扫描有限文件集合，防止开销过大
        prefer_names = [
            ".env",
            ".env.local",
            "env.example",
            "config.yml",
            "application.yml",
            "application.yaml",
            "config.json",
            "settings.py",
            "database.yml",
            "package.json",
            "pyproject.toml",
        ]

        scanned = 0
        max_files = 200
        size_limit = 256 * 1024
        found_hints: List[str] = []

        # 优先扫描常见文件名
        for name in prefer_names:
            path = os.path.join(project_root, name)
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read(size_limit)
                        found_hints.extend(self._extract_db_hints_from_text(text))
                        scanned += 1
                except Exception:
                    pass

        # 继续浅层扫描部分文件
        if scanned < max_files:
            for root, _dirs, files in os.walk(project_root):
                # 仅扫描前两级目录
                depth = root[len(project_root) :].count(os.sep)
                if depth > 2:
                    continue
                for fn in files:
                    if scanned >= max_files:
                        break
                    # 仅看文本向的后缀
                    if not any(
                        fn.lower().endswith(ext)
                        for ext in (".env", ".yml", ".yaml", ".json", ".py", ".ts", ".js", ".toml", ".ini", ".properties")
                    ):
                        continue
                    path = os.path.join(root, fn)
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                            text = f.read(size_limit)
                            hints = self._extract_db_hints_from_text(text)
                            if hints:
                                evidence["matches"].append({"file": path, "hints": hints})
                            found_hints.extend(hints)
                            scanned += 1
                    except Exception:
                        continue

        found_hints = list(dict.fromkeys(found_hints))

        # 与可访问库求交集
        intersection = [h for h in found_hints if h in candidates]
        if len(intersection) == 1:
            return intersection[0], {**evidence, "selected": intersection[0], "hints": found_hints}
        if not intersection and len(candidates) == 1:
            # 只有一个库可访问，直接使用
            return candidates[0], {**evidence, "selected": candidates[0], "hints": found_hints}
        # 无法唯一确定
        return None, {**evidence, "hints": found_hints}

    def _ensure_table_cached(self, db: str, table: str) -> None:
        key = f"{db}.{table}"
        if key in self._schema_cache:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (db, table),
            )
            cols = [r[0] for r in cur.fetchall()]
        if not cols:
            raise ValueError(f"表不存在或无列: {table}")
        self._schema_cache[key] = cols

    def list_tables(self, db: Optional[str] = None) -> List[str]:
        dbname = self._resolve_database(db)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME
                """,
                (dbname,),
            )
            return [r[0] for r in cur.fetchall()]

    def get_table_schema(self, table: str, db: Optional[str] = None) -> Dict[str, Any]:
        """返回指定表的结构信息：列定义、主键、索引与表注释。

        返回示例：
        {
            "db": "mydb",
            "table": "users",
            "comment": "table comment",
            "columns": [
                {
                    "name": "id",
                    "data_type": "int",
                    "column_type": "int(11)",
                    "nullable": false,
                    "default": null,
                    "key": "PRI",
                    "extra": "auto_increment",
                    "comment": "primary key",
                    "ordinal_position": 1
                },
                ...
            ],
            "primary_key": ["id"],
            "indexes": [
                {"name": "idx_email", "columns": ["email"], "unique": false, "index_type": "BTREE"}
            ]
        }
        """
        dbname = self._resolve_database(db)

        # 列定义
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
                       COLUMN_KEY, EXTRA, COLUMN_COMMENT, ORDINAL_POSITION
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (dbname, table),
            )
            col_rows = cur.fetchall()

        if not col_rows:
            raise ValueError(f"表不存在或无列: {table}")

        columns: List[Dict[str, Any]] = []
        primary_key_cols: List[str] = []
        for (
            column_name,
            data_type,
            column_type,
            is_nullable,
            column_default,
            column_key,
            extra,
            column_comment,
            ordinal_position,
        ) in col_rows:
            nullable_flag = (str(is_nullable).upper() == "YES")
            columns.append(
                {
                    "name": column_name,
                    "data_type": data_type,
                    "column_type": column_type,
                    "nullable": nullable_flag,
                    "default": column_default,
                    "key": column_key,
                    "extra": extra,
                    "comment": column_comment,
                    "ordinal_position": int(ordinal_position),
                }
            )
            if str(column_key).upper() == "PRI":
                primary_key_cols.append(column_name)

        # 表注释
        table_comment: Optional[str] = None
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_COMMENT
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (dbname, table),
            )
            row = cur.fetchone()
            if row:
                table_comment = row[0]

        # 索引信息（含 PRIMARY）
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT INDEX_NAME, NON_UNIQUE, INDEX_TYPE, SEQ_IN_INDEX, COLUMN_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY INDEX_NAME, SEQ_IN_INDEX
                """,
                (dbname, table),
            )
            idx_rows = cur.fetchall()

        indexes_map: Dict[str, Dict[str, Any]] = {}
        for index_name, non_unique, index_type, seq_in_index, col_name in idx_rows:
            if str(index_name).upper() == "PRIMARY":
                # 以 STATISTICS 为准，覆盖 primary_key_cols 的顺序
                if col_name not in primary_key_cols:
                    primary_key_cols.append(col_name)
                continue
            if index_name not in indexes_map:
                indexes_map[index_name] = {
                    "name": index_name,
                    "columns": [],
                    "unique": (int(non_unique) == 0),
                    "index_type": index_type,
                }
            indexes_map[index_name]["columns"].append(col_name)

        return {
            "db": dbname,
            "table": table,
            "comment": table_comment,
            "columns": columns,
            "primary_key": primary_key_cols,
            "indexes": list(indexes_map.values()),
        }

    def select_rows(
        self,
        table: str,
        db: Optional[str] = None,
        columns: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
        order_by: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit 必须在 1..1000 之间")

        dbname = self._resolve_database(db)
        self._ensure_table_cached(dbname, table)
        allowed_cols = self._schema_cache[f"{dbname}.{table}"]

        # 列白名单
        if columns:
            for c in columns:
                if c not in allowed_cols:
                    raise ValueError(f"非法列: {c}")
        else:
            columns = allowed_cols

        # 组装 WHERE，键必须是合法列，值参数化
        where_clauses: List[str] = []
        params: List[Any] = []
        if where:
            for k, v in where.items():
                if k not in allowed_cols:
                    raise ValueError(f"非法条件列: {k}")
                where_clauses.append(f"`{k}` = %s")
                params.append(v)

        # 组装 ORDER BY，列必须合法
        order_clause = ""
        if order_by:
            for ob in order_by:
                col = ob.lstrip("-+")
                if col not in allowed_cols:
                    raise ValueError(f"非法排序列: {col}")
            parts = [
                (ob.lstrip("-+"), "DESC" if ob.startswith("-") else "ASC")
                for ob in order_by
            ]
            order_clause = " ORDER BY " + ", ".join(f"`{c}` {d}" for c, d in parts)

        where_clause = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        col_sql = ", ".join(f"`{c}`" for c in columns)
        sql = (
            f"SELECT {col_sql} FROM `{dbname}`.`{table}`"
            f"{where_clause}{order_clause} LIMIT {int(limit)}"
        )

        with self._conn.cursor(dictionary=True) as cur:
            cur.execute("SET SESSION sql_safe_updates=1")
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return rows


guard = MySQLGuard()


@server.tool()
async def list_databases() -> List[str]:
    """列出当前账号可访问的非系统库。"""
    return guard._non_system_databases()


@server.tool()
async def infer_database(project_root: Optional[str] = None, include_evidence: bool = False) -> Dict[str, Any]:
    """从项目内容推断数据库。默认仅返回 { db }；当 include_evidence=true 时返回经过脱敏的证据统计（不含文件路径/内容）。"""
    db, ev = guard._infer_database_internal(project_root)
    if db:
        guard.inferred_db = db
    result: Dict[str, Any] = {"db": db}
    if include_evidence:
        result["evidence"] = {
            "candidates_count": len(ev.get("candidates", [])),
            "hint_count": len(ev.get("hints", [])) if isinstance(ev.get("hints"), list) else 0,
            "selected": ev.get("selected") is not None,
            "method": ev.get("selected") and "selected" in ev or None,
        }
    return result


@server.tool()
async def list_tables(db: Optional[str] = None) -> List[str]:
    """列出数据库中的所有表。db 省略时，使用已推断的库或自动推断。"""
    return guard.list_tables(db)


@server.tool()
async def get_table_schema(table: str, db: Optional[str] = None) -> Dict[str, Any]:
    """获取表结构：列定义、主键、索引。db 省略时自动推断。"""
    return guard.get_table_schema(table, db)


@server.tool()
async def select_rows(
    table: str,
    db: Optional[str] = None,
    columns: Optional[List[str]] = None,
    where: Optional[Dict[str, Any]] = None,
    order_by: Optional[List[str]] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """从指定表安全查询。order_by 项可用前缀 '-' 表示 DESC。db 省略时自动推断。"""
    return guard.select_rows(table, db, columns, where, order_by, limit)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

