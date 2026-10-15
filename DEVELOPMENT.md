# 开发踩坑记录

这个项目从逆向 Everything 到重实现,中间踩了一堆坑。按"现象 → 根因 → 修复"整理如下——如果你要改这个项目的解析层,先读一遍能省很多时间。

## NTFS 底层

### boot sector 的 MFT 记录大小字段读出 246

- **现象**:解析 boot sector 得到 `bytes_per_record=246`,随后读卷设备报 `OSError err=87 (INVALID_PARAMETER)`。
- **根因**:两个错误叠加。① NTFS boot sector offset 0x40 的 `cMFTRecordSize` 是**带符号单字节**(0xF6 = -10 → 2^10 = 1024),读成 4 字节 DWORD 就得到 246;② 卷设备句柄(`\\.\C:`)的 `ReadFile` 要求**扇区对齐**的偏移和大小,246 字节不对齐直接 err=87。
- **修复**:0x40 用 `<b` 读带符号字节,负值 n 表示 2^-n;`ReadFile` 的 size 向上对齐到 512 再截断。Everything 的反编译代码里是 `*(_BYTE *)(v7 + 64)`,IDA 伪代码早就告诉你了。

### FSCTL_ENUM_USN_DATA 拿不到修改时间

- **现象**:官方枚举 API 全盘 151 万条只要 4.9 秒(30 万条/秒),但 `USN_RECORD.TimeStamp` 全是 1601-01-01(0)。
- **根因**:`FSCTL_ENUM_USN_DATA` 的输出不填充时间戳(voidtools 论坛上 .oisyn 有同样的报告)。Everything 手动解析 $MFT 就是为了从 `$STANDARD_INFORMATION` 拿时间。
- **取舍**:手动解析 $MFT(172 万条 19.6s)换全字段。如果只要文件名树,ENUM_USN_DATA 快 4 倍。

### MFT_ENUM_DATA_V1 结构没对齐

- **现象**:枚举行为异常、性能诡异。
- **根因**:`struct.pack("<QQQHH")` 打出 28 字节,而 `MFT_ENUM_DATA_V1` 是 32 字节(QWORD 对齐),NTFS 按 32 字节读输入缓冲,后面 4 字节是垃圾。
- **修复**:`struct.pack("<QQQHH4x")`。

### $STANDARD_INFORMATION 的修改时间偏移

- **现象**:索引里的"修改时间"对不上 `os.path.getmtime`。
- **根因**:$SI 内容布局是 Creation@0x00 / **Modification@0x08** / MftModification@0x10 / Access@0x18,拿 0x18 拿到的是访问时间。
- **教训**:别凭印象写偏移,拿一个已知文件对拍 `os.stat`。

### $DATA 大小要过滤 named stream

- **现象**:个别文件 size=0 或大小不对。
- **根因**:一条 MFT 记录可以有多个 $DATA 属性(unnamed 主流 + named 流如 `Zone.Identifier`),只取第一个会拿到 named 流。
- **修复**:只取 `NameLength == 0`(attr 头 offset 0x09)的非驻留 $DATA。

### 小文件 size 全是 0(resident $DATA 被跳过)

- **现象**:大文件对拍全绿(`size:>500mb` 一致),但 MCP 实测 `ext:py path:...` 结果里所有 .py 文件 size=0——这些文件明明有 16KB 内容。
- **根因**:`size:>500mb` 的对拍只覆盖非驻留 $DATA(RealSize@0x30)。小于几 KB 的文件,NTFS 把内容直接存在 MFT 记录里(resident $DATA),这种属性头没有 RealSize 字段,原解析直接跳过 → size=0。全盘约 30 万小文件受影响。
- **修复**:resident $DATA 的字节数 = 属性头 0x10 的 ValueLength;按 resident/non-resident 分支取样。
- **教训**:对拍查询要覆盖两类数据形态(`size:>500mb` 只打得中非驻留);常驻 MCP 进程不会热加载磁盘上的新代码——修完必须重启 server 并重建索引,实测才能确认。

## FRN 树(最坑的一节)

### 全盘记录号 off-by-one,树整体断裂

- **现象**:索引建完,查询结果为 0;Windows 目录的 FRN 比子项 $FN 里存的父 FRN 大 1。
- **根因**:`record_number += 1` 写在了 `frn = record_number` **前面**,所有记录号偏移 +1。
- **教训**:MFT 记录号 = 流内位置索引,从 0 开始;先取后加。

### FRN 拼完整 64 位导致 3.3 万孤儿

- **现象**:路径表大量缺失,查询缺文件。
- **根因**:NTFS FileReferenceNumber 高 16 位是**序号**,目录删除重建后序号会变;子项 $FN 里存的父序号是**创建时**的旧值。用完整 64 位做树 key,序号一对不上就断链。
- **修复**:全链路(枚举、$FN parent、USN_RECORD)只保留低 48 位记录号。

### 缓存加载后路径表是空的

- **现象**:从 SQLite 加载索引后,每次查询要 50 秒(全走爬树兜底)。
- **根因**:路径表不持久化,`_load_volume` 加载完条目却没重建路径表。
- **修复**:加载后必须调 `_rebuild_paths()`。

### 硬链接丢 1790 条路径

- **现象**:与 es.exe 对拍,`*.dll path:system32` 缺 1790 条——System32 约 35% 的 dll 与 WinSxS 硬链接。
- **根因**(三层,逐层修完才收敛):
  1. 一条 MFT 记录可有多个 $FILE_NAME(硬链接 / Win32 长名 + DOS 8.3 短名),只取第一个会丢其他父目录的路径;
  2. 硬链接的多个 $FN **名字可以相同**(不同目录下的同名文件),按名字去重会把同名不同目录的过滤掉——去重键必须是 **(parent, name) 对**;
  3. attribute list 扩展记录里的 $FN 也要并入(见下条)。
- **顺带**:DOS 短名(ns=2)不能进别名表,否则结果里全是 `XXXXXX~1.EXT`。

### attribute list 扩展记录可以先于 base 出现

- **现象**:部分文件(base 记录号大)的 $DATA/$FN 合并不生效,size=0 或缺名字。
- **根因**:NTFS 不保证 base 记录在扩展记录之前(itss.dll 的 base=1675442 在扩展 1326450 之后);单遍"遇到扩展就找 base"的合并会扑空。而且 `_merge_extended` 最初只合并扩展记录的主名,扩展记录**自己的 aliases** 被丢弃。
- **修复**:base 未出现时把扩展记录暂存 `pending[base_frn]`,base 出现时双向合并;合并函数同时并入 `ext.aliases`。

## Python / 工程实现

### 重命名变量残留 → NameError

- **现象**:索引构建直接失败,`NameError: name 'attr_len' is not defined`。
- **根因**:重构时把解包变量 `attr_len` 改名成 `_l`,函数体里还有一处引用旧的。
- **教训**:改解包签名后全文搜旧名;这类错误单测第一次跑就会抓到——**先写测试再重构**。

### dict 少了 .items()

- **现象**:`TypeError: cannot unpack non-iterable int object`。
- **根因**:`for frn, rows in _group_aliases(...)` 对 dict 迭代得到的是 key。
- **教训**:同上,测试先行的价值。

### datetime.max.astimezone() 溢出

- **现象**:`dm:>2026-01-01` 这类无界时间区间查询抛 `OSError: [Errno 22]`。
- **根因**:`datetime.max`(9999 年)无时区对象 `.astimezone()` 转 UTC 溢出。
- **修复**:无界边界用 FILETIME 哨兵(`0x7FFFFFFFFFFFFFFF`)表示,不构造 datetime。

## es.exe 对拍(拿它当基准时)

- **多词查询必须拆成多个 argv**:`es.exe 'dm:today ext:log'`(单个含空格参数)返回 0 条——es.exe 把整个 argv 当**一个词**;要 `es.exe 'dm:today' 'ext:log'`。
- **`\ & | > < ^` 需要转义**:`path:windows\system32` 里的 `\` 被 es.exe 吃掉返回 0 条;要么双引号包裹,要么避开反斜杠(`path:system32`)。
- **输出编码**:管道输出是控制台代码页(GBK),中文路径乱码;用 `-json` 拿 UTF-8。
- **对拍差异要分类**:建索引期间文件系统在变(时间漂移类差异,如 `dm:today` 多几条新日志)是正常的;要修的是系统性差异(树断裂/短名/硬链接)。

## 工程集成

- **MCP client 新增 server 需重启应用**才随启动配置拉起(改注册配置后不重启不生效)。
- MCP server 的索引构建放后台线程,`initialize` 握手不阻塞;`search` 在索引未就绪时返回状态而不是挂起。

## 查询层重构(2026-10-15)

把查询从"每条记录调一次 Python 闭包 matcher"改成"分阶段过滤 + 有界堆"。这轮全部结论都来自实测,不来自推理——下面每条都附了实测数字,因为它们和直觉不一致。

### 先用数据否决掉三个"看起来很美"的方案

- **每项一次 `re.search`**:335 ns/项,比 `needle in name` 的 56 ns 慢 6 倍。正则只在 C 层快,每次调用本身的固定开销在短文件名上完全不划算。**热路径不要用正则。**
- **所有名字拼成一个大 blob,用带前瞻的整块正则一次扫完**:2191 ns/项(3.4 s),比逐项还慢 40 倍。`(?=[^\n]*a)[^\n]*\n` 这类前瞻在每个起点都要重扫一次行,退化成平方级。**整块扫描在 Python 里不成立。**
- **多条件合并成一个前瞻正则**(`(?=.*a)(?=.*b)`):10915 ns/项。同上。

相反,`list[str]` 上做 `[i for i, x in enumerate(names) if needle in x]` 是 57-79 ns/项,这才是 Python 里最快的形状。

### `iter_items()` 的生成器本身就是最大开销

旧实现 `for drive, frn, e, path in index.iter_items()` 空转(什么都不判)**588 ms / 158 万条 = 373 ns/项**,比 `list(entries.items())` 的 127 ns 高 3 倍——多出来的是 `dict.get` + 生成器 yield + 元组拆包。而真正做子串判断只要 57 ns。**迭代骨架比判断本身贵 6 倍。**

### 分阶段过滤:便宜的先跑,贵的只跑候选

`path:system32 ext:dll`:先 `ext:dll` 整表筛出 5.7 万候选(106 ms),再在这 5.7 万上做路径匹配(21 ms),合计 127 ms;旧的全量 path+ext 扫描是 520 ms,再加闭包开销实际 2798 ms。

实现要点(见 `query.py` 的 `Plan` / `Stage`):

- 只有顶层 AND 链上的条件可拆成阶段;`or` / `not` / 正则 / 路径条件整体退化成 `Full` 阶段,代价最高、排最后。
- 代价序:`NameContains`(0) < `ExtIn`(1) < `SizeCmp`(2) < `MtimeRange`(3) < `NameExcludes`(4) < `Full`(5)。
- 同代价内**最长的子串先测**(WinShun `NameMatcher` 同款):它拒绝的名字最多。
- 通配型取最长字面片段做预筛;**模式里含 `[` 时直接放弃预筛**——`fnmatch` 把 `[abc]` 当字符类,此时任何片段都不是必要条件,预筛会漏结果。
- `!词` 走 `NameExcludes` 阶段(排除条件不缩小候选集,所以排在收窄阶段之后)。

### `ExtIn` 少了"目录不算有扩展名"

- **现象**:分阶段求值的 `ext:git` 返回了 `.git` 目录,暴力实现返回 0 条,新增的对拍测试立刻红。
- **根因**:原来的 `_ext_matcher` 里写着 `if is_dir: return False`,重写成阶段时只照抄了 `endswith`。
- **修复**:`ExtIn` 的 `scan`/`filter` 都加 `not (flags[i] & FLAG_DIR)`。
- **教训**:把闭包拆成阶段时,闭包里**所有**分支都要过一遍,尤其是不显眼的早退。同一次重构里 `TestStagedEqualsBruteForce` 的 90 组(查询 × kind)参数化用例是唯一能抓住它的东西。

### 单表按 FRN 打"死亡标记"会误杀自己刚写的新行

USN 增量最初的做法是"把变更过的 FRN 记进 `dead` 集合,查询时跳过"。它必然错两种情况:

1. **新建文件**:FRN 被标死,随后为它追加的新行也被同一个 FRN 过滤掉;
2. **删除后重建同一 FRN**:DELETE 标死之后再 CREATE,新行照样被滤掉。

- **修复**:分**基表 + 增量表**两张。基表(full build 时定稿)按 FRN 失效,增量表按 FRN **就地覆盖**且永不失效。重复的 DATA_CHANGE 因为覆盖在同一个行号上,不会堆重复行。
- **顺带**:碎片回收阈值除了绝对值,还用了"失效行占比 > 1/4",但**必须加行数下限**——小表(测试里 9 行)上 3 行失效就触发全量重建,反复重建把增量表压得只剩几百行。加了 `SCAN_COMPACT_FLOOR = 4096`。

### `aliases` 表主键漏了 parent,冷热索引结果不一致

- **现象**:冷建索引 `ext:py` 有 355,850 条命中,从 SQLite 恢复后只剩 101,439 条;总行数 274 万 vs 239 万。查询结果取决于"这次是不是刚重建过索引"。
- **根因**:内存里已经按 **(parent, name) 对**去重硬链接别名(见上文"硬链接丢 1790 条路径"那条),但 `aliases` 表的 `PRIMARY KEY (volume, frn, name)` 少写了 parent——同一 FRN 下同名、不同父目录的别名(`WinSxS` 里全是这种)在 `INSERT OR IGNORE` 时被吞掉。
- **修复**:主键改成 `(volume, frn, parent, name)`,`SCHEMA_VERSION` 提到 3。**内存层修完要检查持久化层的 key 是不是同一个。**
- **教训**:这类"冷热不一致"只有把冷建的数和缓存后的数**放在一起比**才会暴露。单看任何一次跑都是自洽的。

### 候选循环里最贵的两件事,都不该在循环里做

实测 35.6 万候选,完整循环 **1259 ns/项**:

| 环节 | 单独成本 |
|---|---|
| 数组下标取值 | 76 ns |
| + `dir_path()`(目录路径查缓存) | 357 ns |
| + 文件名拼接 | 517 ns |
| + `_excluded()` | 183 ns |
| + 排序键元组 | 332 ns |
| + 构造 `Hit` 对象 | 470 ns |

路径解析+拼接 ~440 ns、`Hit` 构造 ~470 ns,合计占七成,而最终只会用到 30-200 条。**修复**:循环里只产出"排序键 + `(vol, scan, 行号)`",路径解析和 `Hit` 构造推迟到选出分页之后(`_materialize`)。排序键里原本用完整路径做末位 tiebreak,改成文件名(更便宜且同样确定)。

优化后 `ext:py` 从 1051 ms 降到 455 ms。

### 别用 tracemalloc 测加载耗时

带 `tracemalloc.start()` 测出的加载是 **37.5 s**,去掉之后是 **16.8 s**。tracemalloc 会 hook 每一次分配,而加载路径全是分配(220 万个 `Entry`)。**测时间就不要开 tracemalloc;要测内存就单独跑一次。**

### `pending_changes` 的记账位置

`pending_changes` 的更新必须放在 `_apply_scan_delta` 里 `if self.scan is None: return` 这个早退**之前**。内存里还没建扫描表时(比如刚 `build_from_mft` 前的路径)也得记账,否则这些变更永远不会落盘,重启后又得靠 USN 重放补。

### USN 记录里没有文件大小,新建的文件 size 恒为 0

- **现象**:索引 size 抽检 E: 卷的 4846 个 `.py`,225 个(4.6%)索引里是 0、磁盘上不是;`everything_ai\index.py` 索引 0 字节、实际 26 KB。
- **根因**:`USN_RECORD` **不带文件大小**。只有 `FSCTL_ENUM_USN_DATA` / 手动解析 $MFT 才拿得到 `$DATA` 的 RealSize。旧的 USN 回放对 CREATE 写 `Entry(..., size=0, ...)`,对 DATA_CHANGE 只更新 mtime——注释里写的"USN 不带新 size,保持旧值"对**已存在的**文件是对的,对**新建的**文件就永远是 0。
- **影响**:agent 写文件 → 立刻能搜到,但拿到的 size 是 0,`size:` 查询也打不中它。恰好吃掉这次刚接上的"实时增量"的全部好处。
- **修复**:同一批 USN 里凡是 CREATE 或 DATA_CHANGE 且不是目录的 FRN,回读磁盘补 `size`/`mtime`(mtime 只向前推)。
- **第一次修错了**:给这轮回读加了单批上限 `USN_STAT_MAX = 2000`,想防批量解压。结果抽样对拍里仍有 244/1599 条 `idx=0`——因为 `read_usn_changes` 一次就把积压的 USN 全部读完(**一个批次 = 全部积压**),上限只是把超出部分**静默丢掉**,而 USN 检查点已经前进,这些文件之后再也不会出现在 USN 里,size 永久停在 0。**用上限截断"必须处理完的补偿动作"等于数据丢失**;要限流就得把检查点退回到最后一个完整处理的位置,否则不许截断。
- **顺带**:`SCHEMA_VERSION` 提到 5 强制重建,否则库里已经写坏的 size=0 行会一直留着。
- **教训**:增量路径的字段完备性要逐个核对"这个事件带不带这个字段"。`entries` 里 `size` 有两个来源(全量解析 / 增量回放),两条路径必须都能填对。

### 抽样对拍要覆盖到"字段值",不能只对"命中条数"

上面两个 bug(别名被落盘吞掉、size 恒为 0)都不会让查询报错,也都不会让"命中条数对拍"失败——它们只让**字段值**错。`scripts/bench_search.py` 现在除了逐条 matcher 的条数对拍,还抽样比对索引里的 size 与 `os.stat`,这一类才算真正盖住。

### `path_scope` 前缀比较要补分隔符

`C:\proj` 直接做 `startswith` 会把 `C:\project\...` 也命中。补一个尾部 `\` 再比(`scope_l = path.rstrip("\\/").lower() + "\\"`),并补一组"`proj` 不能命中 `project`"的用例。

### 有变更就整表重写 SQLite

`build_all` / `refresh_if_stale` 原本在 `changed` 时调 `_save_volume`(`DELETE` + 重插全表)。三卷 220 万条下,启动只要有一卷有 USN 变更就要重写整个库,占掉启动时间的一半以上(16.8 s → 36.3 s 的差别就来自这里)。

**修复**:`VolumeIndex.pending_changes` 记录自上次落盘以来变过的 FRN,`_save_delta` 只 upsert/delete 这些 FRN 的行,并顺带推进 `meta.next_usn`。全量 `build_from_mft` 仍然走 `_save_volume`。`pending_changes` 的更新要放在 `_apply_scan_delta` 的 `scan is None` 早退**之前**——内存里还没建扫描表时也得记账,否则这些变更永远不会落盘。
