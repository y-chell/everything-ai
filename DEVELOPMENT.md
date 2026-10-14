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
