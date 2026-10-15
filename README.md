# everything-ai

AI agent 原生的全盘文件搜索 MCP server。机制逆向自 Everything 1.4.1.1032(NTFS MFT 直接解析 + USN Journal 增量),重新实现为 AI 可直接调用的工具——**无需手动启动任何 GUI 程序挂后台**。

## 与 Everything / WinShun 的关系

| | Everything | everything-ai |
|---|---|---|
| 索引机制 | 手动解析 $MFT(C 实现) | 同款机制(Python ctypes) |
| 增量更新 | USN Journal 每秒轮询 | 查询前按需拉取(节流,默认 1s) |
| 路径解析 | 内存延期解析 | 同款:只记忆化目录路径,文件路径现算 |
| 查询求值 | 名字位图预筛 + 分片 TopK | 分阶段过滤 + 有界堆 |
| 排序 | 精确名优先 / 目录降权 | 移植 WinShun 的打分模型 |
| 调用方式 | GUI / es.exe CLI / WM_COPYDATA | MCP 工具 |

WinShun(`LingCore/WinShun`)对 AI 有价值的部分已吸收:**延迟路径解析**、**便宜条件先筛/昂贵条件只跑候选**、**最长正项先测**、**`!词` 走排除快速路径**、**打分公式**(整名相等 / 名字开头 / 词首 / 覆盖率 / 深度惩罚 / 低优先级目录降权)、**`recent_files` / `list_folder` / `file_info` 三件套**。未吸收的是图形界面、剪贴板历史、对话框搜索、拼音匹配和全文内容索引(那是独立的大工程,本项目的定位是文件名搜索,内容检索交给 ripgrep)。

机制逆向笔记(IDA 反编译 + 官方论坛交叉验证):[docs/mechanism-notes.md](docs/mechanism-notes.md)

## 工具

### search(query, kind, sort, limit, offset, path_scope, include_system)

主力工具,95% 的场景用这一个。

Everything 语法兼容子集:

- 空格 = AND,`|` = OR,`!` = NOT,`"..."` = 短语
- `path:xxx` 全路径匹配(词含 `\` 时自动按路径匹配)
- `ext:py;md` 扩展名多值
- `size:>1mb` / `<=100kb` / `=5gb`(b/kb/mb/gb)
- `dm:today` / `yesterday` / `thisweek` / `thismonth` / `thisyear` / `>2026-01-01` / `2026-10-14`
- `regex:xxx` 正则、`case:xxx` 区分大小写、`*` `?` 通配符
- `kind`: `all` / `files` / `folders`;`sort`: `relevance`(默认)/ `modified` / `oldest` / `size` / `name`
- `path_scope`: 只在某个目录子树里找;`limit` 1-200(默认 30)
- 空 query 返回最近修改的文件

默认**相关性排序**(移植 WinShun):整名相等 > 去扩展名相等 > 名字开头命中 > 词首命中 > 普通子串;命中覆盖率越高越前;目录略前、`node_modules`/`.git`/构建目录/系统目录里的降权 35、隐藏项降权 15、路径越深越靠后;同分时最近修改的靠前。

### recent_files(folder, extensions, days, kind, limit, include_system)

最近修改的文件,倒序。问"刚写了什么""今天改了什么"用这个,不要用 `search`。

### list_folder(path, extensions, days, kind, include_hidden, limit)

列出某个目录的直接子项(不递归,实读磁盘,永远是最新状态)。

### file_info(path)

单个路径的元数据:是否存在、类型、大小、三个时间、扩展名、隐藏/只读。

### index_status() / rebuild_index(volume="")

索引状态;强制全量重建(USN journal 断档时自动触发,一般无需手动)。

## 性能

C: 卷 157 万条、三卷 223 万条记录 / 274 万行扫描表(2026-10-15 实测,Windows 10,Python 3.14):

| 查询 | 优化前 | 现在 |
|---|---|---|
| `python.exe` | 1045 ms | **199 ms** |
| `README.md` | 1010 ms | **217 ms** |
| `config` | 1015 ms | **230 ms** |
| `node_modules` | 1010 ms | **199 ms** |
| `ext:py`(35.6 万命中) | 1270 ms | **455 ms** |
| `*.log dm:today` | 2768 ms | **197 ms** |
| `path:windows size:>10mb` | 2710 ms | **291 ms** |
| `path:system32 ext:dll` | 2798 ms | **273 ms** |
| `!node_modules ext:json` | 3276 ms | **250 ms** |
| 空 query(最近修改) | 838 ms | **347 ms** |

`scripts/bench_search.py` 同一次运行里,每个查询还会用"逐条完整 matcher"的暴力实现各跑一遍对结果总数——12 组全部一致;另外抽样比对索引里的 `size` 与磁盘 `os.stat`。

USN 增量是逐 FRN 落盘的;新建/改过的文件会回读磁盘补上 USN 记录里没有的文件大小(修改时间只向前推)。所以 agent 刚写出的文件不仅立刻能搜到,`size` 和 `size:` 查询也是对的。

启动:冷建索引(全量读 $MFT)约 70 s/三卷;之后从 SQLite 恢复 + USN 增量回放约 12-17 s。查询前的 USN 刷新只写变过的 FRN。

## 运行要求

- Windows + NTFS 卷,**管理员权限**(读 `\\.\X:` 卷设备;非 admin 时 search 返回明确错误)
- Python 3.12+,`pip install mcp`

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

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `EVERYTHING_AI_EXCLUDE` | 空 | 分号分隔的路径前缀(大小写不敏感),匹配的结果被剔除 |
| `EVERYTHING_AI_REFRESH_INTERVAL` | `1.0` | 查询前拉取 USN 增量的最小间隔(秒);`0` = 每次都拉 |

## 已知边界

- **词只匹配文件名,不匹配父目录名**:`ext:py myproj` 找不到 `myproj\a.py`,要写 `ext:py path:myproj`(WinShun 允许词落在父文件夹名上,这里没做——它会让预筛少一级,代价是候选循环里每个词都要再解析一次路径)。
- **首次运行会重建索引**:SQLite 的 `PRAGMA user_version` 与代码里的 `SCHEMA_VERSION` 不一致时会丢掉缓存表重建(约 70 s)。升级后第一次启动属于这种情况。
- ReFS 卷未支持(Everything 支持;本机全 NTFS,未实现 V3/V4 record)
- USN 回放不跟踪硬链接别名增删(HARD_LINK_CHANGE),重建索引时校正
- 文件名搜索为主;`file_info` / `list_folder` 直读磁盘,内容搜索请用 grep/ripgrep
- 内存:索引含 `lower`(预折叠名)、`parents/sizes/mtimes` 平行数组和全部目录路径缓存,实测 274 万行约 1.34 GB

## 测试

```bash
python -m pytest tests/ -q          # 217 个单测(合成 fixture,不碰真实卷)
python scripts/bench_search.py      # 性能基线 + 与暴力实现的结果对拍(需管理员)
python scripts/compare_es.py        # 与 es.exe 对拍(需管理员 + Everything 索引在跑;
                                    #  es.exe 需在 PATH 或设 ES_EXE 指向完整路径)
python scripts/e2e_mcp.py           # MCP stdio 端到端(全部工具各调一次)
```

## 开发踩坑

从逆向到重实现、再到查询层优化踩过的坑按"现象 → 根因 → 修复"整理在 [DEVELOPMENT.md](DEVELOPMENT.md)——改解析层或查询层之前先读它。
