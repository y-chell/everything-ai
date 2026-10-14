# Everything 1.4.1.1032 (x64) 机制逆向笔记

分析对象:everything.exe 1.4.1.1032(x64,2,272,424 字节,PE64)
工具:IDA Professional 9.5 (ida-mcp) + voidtools 官方论坛(作者 void 回复)交叉验证
日期:2026-10-14

## 1. 全量索引:`ntfs_enum_mft`(assert 字符串暴露源文件 `.\src\ntfs.c`)

入口链:打开卷 `\\.\C:`(GENERIC_READ)→ 读 boot sector → 定位 $MFT → 解析 data runs → 大块读记录。

### boot sector 解析(sub_1400CFA70)
- 读 offset 0 起始 ≥0x50 字节,校验 offset 3 处 8 字节签名 `"NTFS    "`
- `bytes_per_sector` = WORD @0x0B
- `sectors_per_cluster` = BYTE @0x0D(带符号,负值 n 表示 2^-n)
- `mft_lcn` = QWORD @0x30($MFT 起始簇)
- `bytes_per_file_record` = DWORD @0x40(带符号,负值 n 表示 2^-n,通常 1024)
- `serial_number` = QWORD @0x48

### $MFT 定位与读取(sub_1400D13A0 = ntfs_enum_mft)
- 读 MFT record 0,遍历属性找 TypeCode 0x80($DATA),非驻留(form flags & 1)
- 解析 mapping pairs(data runs):每段首字节低 4 位=长度字节数、高 4 位=偏移字节数;偏移有符号(稀疏支持);cluster offset 累计
- 按 run 逐段 SetFilePointer + ReadFile,缓冲区 max(64KB, 记录大小对齐),读满后按 `bytes_per_file_record` 切片批量解析
- 每条记录先做 **USA fixup**(更新序列数组):记录末尾 fixup 值必须等于 USA number(@0x06 count),逐扇区替换回原始 2 字节;不匹配报 `bad US %x != %x`
- FILE_RECORD_HEADER 字段(日志字符串逐字段印证):USA Offset@0x04、USA Count@0x06、LSN@0x08、sequence_number@0x10、link_count@0x12、attribute_offset@0x14、flags@0x16(0x01=in-use,0x02=directory)、bytes_in_use@0x18、bytes_allocated@0x1C、base_frn@0x20(attribute list 扩展记录指回 base)
- 属性遍历:TypeCode 0xFFFFFFFF 结束;length<0x10 报 BAD;0x80 $DATA 非驻留时读 file_size/valid_data_length/mapping_pairs_offset
- 处理 extended MFT 记录(attribute list)与 fragmented MFT;`base_fragmented_record_count` 二分查找合并
- 内存索引:72 字节/条紧凑结构(步长 9*8),回调逐条产出

## 2. 增量更新:monitor 线程(sub_14004FB70)

- 每卷一个 OVERLAPPED 异步 `DeviceIoControl(0x900BB = FSCTL_READ_USN_JOURNAL)`,输入 0x28=40 字节(READ_USN_JOURNAL_DATA_V0):
  - StartUsn = journal NextUsn
  - ReasonMask = 0xFFFFFFFF(全部事件;对应 ini `usn_record_filter=0xffffffff`)
  - BytesToWaitFor = 9(任意新记录立即完成 → 实时)
- 完成端口式投递队列,worker 解析 USN_RECORD 更新索引
- 唤醒定时器上限 1000ms(ini `monitor_update_delay=1000`,等待值 +17ms 余量)
- 错误处理:
  - 996 ERROR_IO_INCOMPLETE → 继续;997 ERROR_IO_PENDING → 标记等待
  - 1177-1179(ERROR_JOURNAL_DELETE_IN_PROGRESS 等)/1181(ERROR_JOURNAL_ENTRY_DELETED)→ journal 过期,标记 out of date → 全量重建
  - 其他 → 30 秒重试(ini `monitor_retry_delay=30000`)
- `SetThreadPriority(THREAD_MODE_BACKGROUND_BEGIN)`(ini `monitor_thread_mode_background=1`)
- ReFS 卷走独立路径(`refs monitor read error` 日志)

## 3. 单记录查询

`FSCTL_GET_NTFS_FILE_RECORD`(0x90068)两处调用:按 FRN 取单条 MFT 记录(ini `allow_ntfs_open_file_by_id=1` 对应 OpenFileById 路径)。

## 4. 查询与 IPC

- 查询 = 内存线性扫描;语法:空格 AND、`|` OR、`!` NOT、`path:`/`ext:`/`size:`/`dm:` 等修饰符、`case:`/`regex:`
- IPC = WM_COPYDATA,窗口类 `EVERYTHING_IPC_WNDCLASS`,QUERY2W 结构(reply_hwnd/search_flags/offset/max_results/sort/request_flags + 字符串);es.exe 即此通道
- 持久化 = Everything.db(本机 113MB)

## 5. 官方论坛交叉验证(void 亲自回复)

- 首次索引读 MFT;USN Journal 只用于增量(每秒);修改时间取自 MFT $STANDARD_INFORMATION
- USN Journal 由 NTFS 驱动维护,Everything 仅请求启用;断档(约一周)即全量重建
- 来源:[voidtools forum t=12779](https://www.voidtools.com/forum/viewtopic.php?t=12779)、[t=8635](https://www.voidtools.com/forum/viewtopic.php?t=8635)、[IPC C Example](https://www.voidtools.com/support/everything/sdk/ipc_c_example/)

## 6. 对自研实现的指导意义

1. 全量枚举可直接用官方 `FSCTL_ENUM_USN_DATA`(0x900B3,MFT_ENUM_DATA 续传,错误 38 结束)——比 Everything 的手动 $MFT 解析慢一点但可靠得多;或手动解析(快,需处理 USA fixup/attribute list/碎片)。**取舍:用 FSCTL_ENUM_USN_DATA,牺牲少量速度换正确性**;TimeStamp 字段在枚举时由 NTFS 填充($SI 修改时间),实现时验证,不可靠再退回手动解析。
2. 增量用同步 FSCTL_READ_USN_JOURNAL(按需拉取即可,MCP 查询前刷一次),记录 NextUsn 检查点;journal 过期(1177-1179/1181)→ 全量重建。
3. 索引存 FRN 树(frn → parent_frn/name/flags/size/mtime),路径按需向上解析(Everything 同款 deferred resolution)。
4. 查询前缀语法保持 Everything 兼容子集,AI 无需学习新语法。
