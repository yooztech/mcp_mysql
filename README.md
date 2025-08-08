## yooztech_mcp_mysql —— 基于 MCP 的 MySQL 工具（只读、安全、支持库推断）

该 MCP 服务器在 MCP 客户端中提供安全的 MySQL 读取能力：
- `list_databases()`：列出当前账号可访问的非系统库
- `infer_database(project_root=None, include_evidence=false)`：根据项目内容推断库名（默认仅返回 `{ db }`，可选返回最小化证据统计，不含文件路径/内容）
- `list_tables(db=None)`：列出库内表（未传 `db` 时使用已推断库或自动选择）
- `select_rows(table, db=None, columns=None, where=None, order_by=None, limit=100)`：从库表安全查询

### 安全设计
- 表/列白名单：从 `information_schema` 动态获取并校验
- 参数化查询：拒绝拼接与多语句
- 限流：默认 `LIMIT 100`，最大 `LIMIT 1000`
- 隐私：推断时不回传文件路径/内容；如需证据仅返回脱敏统计

### 在 Cursor 中配置
在 Cursor 的设置中添加 MCP Server，示例（仅示意，不包含本地路径）：

```json
{
  "mcpServers": {
    "yooztech_mcp_mysql": {
      "command": "yooztech_mcp_mysql",
      "args": [],
      "env": {
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "3306",
        "DB_USER": "mcp_tool",
        "DB_PASS": "<your-strong-pass>"
      }
    }
  }
}
```

说明：
- 以上配置假设你已通过包管理器安装本工具（例如 `pip install yooztech_mcp_mysql`），从而可直接使用控制台脚本 `yooztech_mcp_mysql`。
- 若使用其他运行方式（如 uvx/容器），仅需将 `command` 与 `args` 改为相应启动方式即可，但不要在 README 中暴露你的本地路径。

### 使用 uvx 方式（推荐，免安装）
如希望在不全局安装包的情况下运行，可在 Cursor 中使用 `uvx`：

```json
{
  "mcpServers": {
    "yooztech_mcp_mysql": {
      "command": "uvx",
      "args": ["yooztech_mcp_mysql"],
      "env": {
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "3306",
        "DB_USER": "mcp_tool",
        "DB_PASS": "<your-strong-pass>"
      }
    }
  }
}
```

提示：确保系统已安装 `uv`/`uvx`。企业私有镜像可通过 `UV_INDEX`/`--index-url` 进行加速配置。

### 环境变量
- `DB_HOST`：数据库地址
- `DB_PORT`：数据库端口
- `DB_USER`：数据库只读账号（最小权限）
- `DB_PASS`：数据库密码

推荐在数据库中为该工具创建最小权限账号（示例）：

```sql
CREATE USER 'mcp_tool'@'10.0.%' IDENTIFIED BY '<your-strong-pass>';
GRANT SELECT ON *.* TO 'mcp_tool'@'10.0.%';
FLUSH PRIVILEGES;
```

如需进一步收紧权限，可按库或表粒度授予 `SELECT`。

### 使用建议（在 MCP 客户端中调用）
- 先调用 `list_databases` 查看可访问库
- 再调用 `infer_database` 自动推断当前项目使用的库（默认仅返回 `{ db }`）
- 随后调用 `list_tables` / `select_rows`（不传 `db` 时将使用推断库；如有歧义请显式传入 `db`）

### 版本与许可证
- 版本遵循语义化版本（SemVer）
- 许可证：MIT（详见包内 LICENSE）

