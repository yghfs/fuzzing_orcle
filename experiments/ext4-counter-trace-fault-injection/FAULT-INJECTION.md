# 程序级公式门控与错误序列变异

## 执行单位

一个程序是一段固定的顺序 syscall；每次执行用独立会话和 `run` 编号。

```text
program start: formula_seen = false
  syscall 0: error A                 -> 观测，不纳入候选
  syscall 1: formula F -> error B     -> B 可以变异
  syscall 2: error C                 -> C 可以变异
program end: 完整程序的稳定快照检查
下一次执行: formula_seen = false
```

门控发生在实际到达时。即使 A 的调用内部后来触发 F，也不会把此前 A 的 enter 改为合格；
F 之后真正执行的其他 enter 才能合格。每个 syscall 仍单独记录返回值并做公式操作出口检查，
最终再用程序结束后的稳定快照确认状态。

内核只接纳会话 owner 任务中已经显式 armed 的目标 syscall 窗口。用户态 marker 关联输入，
原生 `raw_syscalls:sys_enter/sys_exit` 确定内核执行窗口。运行时 stat、KCOV、marker 写入、
适配及 observer 扫描均在窗口之外。目标设备由工作目录确定，每次 syscall 再根据实际对象/
父目录设置 target；无法确认目标设备的调用不参与。公式 hook 额外核对 `sb->s_dev`，
并排除 `BEGIN/END/ALL` 和 `SNAPSHOT/MOUNT/UNMOUNT`。

## 插桩和失败语义

`0001` 在原 counter-trace 补丁上添加全部 265 个观测探针；`0002` 将这些表达式接入
[内核运行时](instrumentation/efi.c)。按 token、caller、TU 选择器和哈希匹配源码，
生成失败直接报错。[策略生成器](scripts/configure-injection.py) 对每条记录生成显式策略，
保存到 [injection.json](injection.json)：215 条支持注入，50 条只观测。

最终被测代码保留官方 Linux 6.12.1 的错误处理语义。`0002` 同时撤销基础补丁中
与插桩无关的修复和额外校验，具体恢复项与全树哈希检查见 [源码基线](SOURCE-BASELINE.md)。

| 接口类别 | 处理 |
| --- | --- |
| NULL 失败的分配/查找 | 跳过调用，返回同类型 0；带 GFP 的策略检查 `__GFP_NOFAIL` |
| ERR_PTR 接口，如 get_inode_acl | 同类型负错误指针 |
| errno 接口 | 与接口对应的 ENOMEM/EIO/EINVAL 等 |
| copy_from_user/copy_to_user | 返回整个请求的未复制长度；零长度不注入。copy_from_user 在目标大小检查通过后清零目标缓冲区，检查不通过则不写；copy_to_user 不写用户缓冲区 |
| trylock | 返回 0，不执行原加锁 |
| d_make_root | 失败仍 iput 消费输入 inode 引用 |
| quota/crypt/verity | 相应运行时条件满足时才允许注入 |
| 缓冲区状态谓词、日志状态操作、复杂初始化 | `supported=false`，保留探针；匹配计划时记为 blocked |

外层 wrapper 捕获原参数，只求值一次；`alloc_percpu` 的类型参数单独处理。
无注入时执行原调用，保留类型、短路与嵌套；注入时跳过调用，显式 cleanup 处理
失败时消费输入引用或清零目标缓冲区的契约，不先成功分配资源再丢掉返回值。
宏探针的边界是源码表达式，不表示宏底层的 callee 必然执行过。

## 点身份和反馈

可变异点为 `(call, site_id, occurrence, context)`。call 从 0 开始；occurrence 是该 syscall
内该 site 第几次 enter，每个 syscall 清零；context 是最多 24 个内核栈帧的指纹。
重复、递归、嵌套和不同调用上下文可以分别控制。

反馈使用真正发生的联合轨迹，包含公式点、错误点身份、实际注入位和返回类别。
原始返回值保留作证据，成功地址和任意字节数不作为新覆盖率。新轨迹产生原计划和实际命中
故障子集两类种子，对实际合格点逐一翻转故障位，去重后入队；支持关闭故障、加入新点、
组合故障，最多同时计划 64 个故障。调度交替探索普通队列与组合，避免单点计划挤满短测预算。

每轮独立重新检查资格和身份；前一变体修改了目录，下轮就可能出现：

| 状态 | 含义 |
| --- | --- |
| injected | 完整身份匹配，已有公式资格，策略允许，实际跳过调用 |
| blocked | 身份匹配，但本轮没有资格或策略不允许 |
| not_reached | 没有到达与计划完整身份一致的点 |

新联合轨迹允许输入进入语料池，后续交替从资源种子生成及对保存的 syzkaller 程序做类型变异。
保留比例、调用来源、seed、变异方法、是否回退均由原生成器写入 `program.json`。
沿用原实验 allowlist、稳定数据 FD 适配和 I/O 大小限制，不声称完全无约束输入。
变体之间固定程序及适配规则，实际 FD 和路径成功与否随文件系统状态变化，并在 syscall 证据中记录。

## 持续状态和 oracle

每个 campaign 新建一次 ext4 数据盘并只挂载一次，全部输入及变体共用该挂载。
每个新输入在独立子目录准备一次种子文件，同一输入的后续变体不重新准备目录。
运行时打开现有备用数据文件，不在每轮重建或恢复文件内容。
因此不以“必须再次到达某点”为前提，也不把未命中当成注入成功。

每轮复用 [生命周期 checker](../ext4-counter-trace/scripts/trace_lifecycle.py)，
分开报告逐点瞬态差值、操作退出差值、对象后置条件及最终稳定态。
快照、namespace、relations 无效，原生 syscall 关联不全，配对/顺序不全，
会话与 trace 计数不符，或丢失统计异常时，不纳入正常反馈。
确认的操作出口/持续差值、对象关系错误或新内核诊断停止 campaign 并保存证据。
errno、NULL、EFAULT、trylock 失败本身不是 bug。

`complete` 表示证据完整性，`checker_passed` 表示 oracle 通过，二者不能替代。
campaign 状态为 complete、finding、incomplete、error 或 interrupted；受时限和重放上限限制，
complete 不表示穷尽整个故障空间。campaign 结束才卸载、关机并做 `e2fsck -fn`。
异常退出尽力保存 partial trace、dmesg 和 guest 文件；无法收全则保留串口及 round-state。

## 会话接口与重放

root 打开 `/sys/kernel/debug/ext4_efi/session` 并持有 FD，一次只能一个程序拥有会话。
[用户态运行时](tests/efi-runtime.h) 使用受保护的 FD 248：

```text
plan CALL SITE_ID OCCURRENCE CONTEXT  # begin 前加载，最多 64 条
begin MAJOR MINOR RUN_ID             # RUN_ID 非零，清空资格
call CALL MAJOR MINOR NR             # 下一个 syscall，CALL 从 0 连续增长
leave                               # 原生 syscall 返回后
end                                 # 程序结束后
```

read 得到总计数及计划状态，close/进程退出释放会话。每次执行新开会话，超时或崩溃而没有
正常 end 时不伪造完整结果。`efi_formula` 与 `efi_site` 共用递增的 enter/公式 `order`，
对应 return 复用 enter 的 order 和资格决定。旧的 `ext4_error:error_site` 全局观测流
仍能独立启用，故障 campaign 使用 owner 范围的 `ext4_efi:*` 流。

context 基于当前启动的内核地址，不跨重启直接复用。`--program-dir` 核对保存输入的哈希，
重新导出、重新采集基线和计划；它不恢复旧 campaign 的磁盘状态。
当前只支持单 worker 顺序 syscall，不将异步线程和并发程序合成精确注入轨迹。
