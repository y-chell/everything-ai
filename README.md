# everything-ai

AI agent 原生的全盘文件搜索 MCP server。机制逆向自 Everything 1.4.1.1032(NTFS MFT 直接解析 + USN Journal 增量),重新实现为 AI 可直接调用的工具——**无需手动启动任何 GUI 程序挂后台**。

## 与 Everything 的关系

| | Everything | everything-ai |
|---|---|---|
| 索引机制 | 手动解析 $MFT(C 实现) | 同款机制(Python ctypes) |
| 增量更新 | USN Journal 每秒轮询 | USN Journal 查询前按需拉取 |
| 运行方式 | 用户手动启动 GUI/服务常驻 | MCP client(Claude Code / Codex 等)按需拉起,索引持久化到 SQLite |
| 调用方式 | GUI / es.exe CLI / WM_COPYDATA IPC | MCP 工具(search / index_status / rebuild_index) |
| 查询语法 | Everything 语法 | Everything 语法兼容子集 |

机制逆向笔记(IDA 反编译 + 官方论坛交叉验证):[docs/mechanism-notes.md](docs/mechanism-notes.md)

## 工具

### search(query, max_results=100, offset=0, sort="name")

Everything 语法兼容子集:

- 空格 = AND,`|` = OR(`a b|c` = a AND (b OR c)),`!` = NOT,`"..."` = 短语
- `path:xxx` 全路径匹配(词含 `\` 时自动按路径匹配)
- `ext:py;md` 扩展名多值
- `size:>1mb` / `<=100kb` / `=5gb`(b/kb/mb/gb)
- `dm:today` / `yesterday` / `thisweek` / `thismonth` / `thisyear` / `>2026-01-01` / `2026-10-14`
- `regex:xxx` 正则、`case:xxx` 区分大小写、`*` `?` 通配符
- sort: `name` | `date_modified` | `size`

### index_status()

各卷记录数、构建时间、USN 检查点、就绪状态。

### rebuild_index(volume="")

强制全量重建(USN journal 断档时自动触发,一般无需手动)。

## 运行要求

- Windows + NTFS 卷,**管理员权限**(读 `\\.\X:` 卷设备;非 admin 时 search 返回明确错误)
- Python 3.12+,`pip install mcp`
- 首次建索引约 20-60 秒/卷(157 万条 ≈ 45s);之后从 SQLite 缓存秒级恢复 + USN 增量回放

## 注册到 MCP client

标准 stdio MCP server,任何支持 MCP 的 harness(Claude Code、Codex、Cline、DSH 等)都能直接挂载,配置项一致:

```json
{
  "mcpServers": {
    "everything-ai": {
      "command": "python",
      "args": ["-m", "everything_ai.server"],
      "env": { "PYTHONPATH": "<本仓库克隆路径>" }
    }
  }
}
```

`pip install -e .` 后 `PYTHONPATH` 可省,command 可直接用 `everything-ai`(pyproject 里定义了同名入口)。各 client 的注册入口以其自身文档为准。

## 已知边界

- 可选排除前缀:设环境变量 `EVERYTHING_AI_EXCLUDE=前缀1;前缀2`(分号分隔、大小写不敏感),匹配的路径会从结果中剔除。用于把"agent 拿到路径后递归读取会有副作用"的目录(比如被手机同步软件托管的目录,读取会触发云端下载)挡在结果之外;默认无排除
- ReFS 卷未支持(Everything 支持;本机全 NTFS,未实现 V3/V4 record)
- USN 回放不跟踪硬链接别名增删(HARD_LINK_CHANGE),重建索引时校正
- 文件名搜索;内容搜索请用 grep/ripgrep

## 测试

```bash
python -m pytest tests/ -q          # 58 个单测(合成 fixture,不碰真实卷)
python scripts/compare_es.py        # 与 es.exe 对拍(需管理员 + Everything 索引在跑;
                                    #  es.exe 需在 PATH 或设 ES_EXE 指向完整路径)
python scripts/e2e_mcp.py           # MCP stdio 端到端
```

对拍结果(2026-10-14,C: 卷 157.8 万条):5 组查询 4 组完全一致,1 组仅建索引期间的时间漂移(18 条),无系统性缺失。

## 开发踩坑

从逆向到重实现踩过的坑(FRN 序号漂移、硬链接多 $FN、attribute list 乱序、es.exe 对拍语法等)按"现象 → 根因 → 修复"整理在 [DEVELOPMENT.md](DEVELOPMENT.md)——改解析层之前先读它。
