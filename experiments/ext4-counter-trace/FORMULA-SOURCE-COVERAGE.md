# ext4 公式—源码覆盖矩阵

日期：2026-10-09  
检查对象：`kernel/linux-6.12.1/`，官方 Linux 6.12.1 原始源码  
公式定义：主线项目根目录 `公式.md`

本文件以干净上游行号记录初版设计，供 review 与插桩定位。初版表中的 pending 是设计时状态；当前实现状态以文末“第一版实现状态”及 `PROBE-API-SNAPSHOT.md` 为准。development 源码与上游参照分开保存。

当前四项长跑门槛以本表“公式—变量—独立来源”的逐公式状态及实现状态表为准。下方源码点位矩阵描述写入路径/锁边界及定向验证范围；其中保留的 `pending` 表示该源码路径没有独立 fault/layout fixture 或细粒度阶段证明，不表示该写入点未被 runtime trace 覆盖。未验证布局必须返回 `unverified`，不能按一致计入。

## 结论与插桩规则

1. 要覆盖公式相关的全部权威写入路径和特殊转换路径；不要求对每次 getter、每条赋值机器指令或所有上层调用者分别插桩。
2. probe 放在语义状态转换点：bitmap bit 改变、group descriptor 发布、global counter 发布、bigalloc 延迟释放、inode/link 更新和 operation/journal 边界。必要时在相邻发布步骤各记一点，以保留可解释的中间态。
3. 同一 group 的 bitmap 和 descriptor 通常在 `ext4_lock_group()` 内更新；锁内不得做可能睡眠的全 bitmap/group 扫描，也不得为全局检查重复获取 group lock。锁内只记录本次 bit transition、group、计数前后值和阶段；总量及 bitmap 交叉检查放到证明稳定的快照。
4. `percpu_counter_sum()` 能得精确聚合值，但不会令其与 descriptors/bitmaps 同时快照。并发下的总量 mismatch 必须带有效性状态，不能直接判错。
5. syscall 请求量不能作为实际分配量。expected 增量来自实际 bitmap 0→1/1→0 转换；错误路径、回滚和部分转换均按实际转换事件计量。
6. 普通固定容量更新与 resize、lazy bitmap 初始化、corrupt group 排除、fast-commit replay 分开建模，禁止混在普通 `A/F`。

## 公式—变量—独立来源

| 公式 | actual / 单位 | independent expected | 主要检查阶段 | 状态 |
| --- | --- | --- | --- | --- |
| `M-B` | `R_B`，cluster | 挂载初始化扫描实际纳入的 `C_g` 之和；来源路径需记录 corrupt-group 排除 | percpu 初始化完成 | implemented；只验证初始化来源，不证明 bitmap 正确 |
| `M-I` | `R_I`，inode | 挂载时 `I_g` 之和 | percpu 初始化完成 | implemented；只验证初始化来源，不证明 bitmap 正确 |
| `M-U` | `R_U`，directory inode | 挂载时 `U_g` 之和 | percpu 初始化完成 | implemented；只验证初始化来源，不证明 bitmap 正确 |
| `R-BG` | `C_g`，cluster | 有效 block bitmap 的逻辑空闲 cluster bit 数 `L_g^B`；`BLOCK_UNINIT` 时按 ext4 布局计算 | 组局部更新事件；可靠稳定快照 | implemented；运行时逻辑位图重算；未支持布局返回 `unverified` |
| `R-BSUM` | `R_B`，cluster | 纳入健康计数域的 `sum(C_g)` | global 发布点仅记录；稳定内存快照判等 | implemented；稳定窗口与 corrupt-group 域匹配时判定 |
| `R-IG` | `I_g`，inode | 有效 inode bitmap 的逻辑空闲 bit 数 `L_g^I`；处理 reserved inode、padding、`INODE_UNINIT` | 组局部更新事件；可靠稳定快照 | implemented；运行时逻辑位图重算；未支持布局返回 `unverified` |
| `R-IH` | inode table 已初始化前缀长度 `H_g = inodes_per_group - bg_itable_unused` | 已初始化 inode bitmap 中最高的 allocated bit；只要求所有 set bit 都小于 `H_g`，不要求高水位等于当前最高 bit | 稳定 bitmap/descriptor 快照或离线恢复后镜像；跳过 `INODE_UNINIT` 及无 descriptor checksum 的文件系统 | implemented（runtime + offline）；普通/bigalloc 定向 VM 命中，旧格式/未初始化组为 unverified |
| `R-IEVICT` | 最终 eviction 后目标 inode bitmap bit | eviction 前目标 bit 与退出后 bit 的独立读取 | 最后引用释放、没有并发 inode 转换的操作出口 | implemented（runtime）；错误出口 fault fixture pending |
| `R-IDELTA-EVICT` | eviction 对应 group/global free-inode 增量 | 同一目标 inode 的 bitmap 释放与 descriptor/global 差值 | 稳定、独占的 eviction 后置窗口 | implemented（runtime）；并发或异步更新为 unverified |
| `O-LIFE` / `O-LIFE-DELTA` | orphan slot owner 与删除后空槽/计数增量 | slot 内容、inode orphan owner、`ob_free_entries` | orphan-file 启用且 block 可读；旧 orphan 链表不适用 | implemented（runtime）；regular/bigalloc/inline 定向 VM 命中 |
| `XATTR-REF` / `XATTR-REF-DELTA` | xattr header 引用数与独立 inode `i_file_acl` 集合 | live/raw inode table 扫描、header 读取、detach 后实际释放结果 | 稳定内存快照；零引用泄漏和持久化需离线 bitmap/recovery | implemented（runtime）；共享/分离 xattr 定向 VM 命中 |
| `R-ISUM` | `R_I`，inode | `sum(I_g)` | global 发布点仅记录；稳定内存快照判等 | implemented；稳定窗口与纳入域匹配时判定 |
| `R-USUM` | `R_U`，directory inode | `sum(U_g)` | group/global 发布点；稳定内存快照判等 | implemented；稳定窗口与纳入域匹配时判定 |
| `R-UG` | `U_g`，directory inode | 对 group 内有效、已分配 inode 独立识别 mode 并计数所得 `Q_g` | 稳定完整 inode 扫描 | implemented（runtime 与 offline）；竞争窗口返回 `unverified` |
| `R-BDELTA` | `R_B(t1)-R_B(t0)`，cluster | 窗口内所有实际 block bitmap 0→1 分配数取负、1→0 释放数取正；并发事件必须完整 | 更新点序列；完整窗口边界；操作出口/静止快照 | implemented；完整健康 trace 窗口判定；跨线程因果不明时标记不确定 |
| `R-IDELTA` | `R_I(t1)-R_I(t0)`，inode | inode bitmap 实际 0→1 分配和 1→0 释放数 | 更新点序列；完整窗口边界；操作出口/静止快照 | implemented；完整健康 trace 窗口判定；跨线程因果不明时标记不确定 |
| `R-UDELTA` | `R_U(t1)-R_U(t0)`，directory inode | 新建/释放目录 inode 的实际成功转换数；mkdir/rmdir/rename 调用次数不能直接代替 | directory inode 生命周期事件；完整窗口边界 | implemented；按 directory inode 生命周期及发布事件判定 |
| `R-LINK` | inode 内存 `i_nlink`，link count | 稳定命名空间全量扫描得到该 inode 的有效 dirent 数，包含 `.`、`..` | 命名空间更新事件保留中间态；完整稳定 namespace snapshot 判等 | implemented（runtime 与 offline）；竞争窗口返回 `unverified` |
| `P-SB` | 内存 `s_es->s_free_blocks_count`（block）、`s_free_inodes_count`（inode） | 同次发布读取的 `rho * R_B`、`R_I` | `ext4_update_super()` 字段复制完成；需记录聚合时间窗 | implemented；判定内存汇总发布，不等同于磁盘持久化 |
| `P-DISK` | 磁盘 superblock `D_B/D_I`，block/inode | 同一持久状态下磁盘 descriptor 求和及逻辑 bitmap 扫描 | 干净卸载、可靠冻结/持久化或 replay 后离线副本 | implemented；`scripts/check-disk.py`，`BLOCK_UNINIT` 布局仍明确 unverified；同一扫描也核对 `R-IH` |

## 权威写入和阶段点矩阵

下表中“点位建议”是语义事件，而不是要求每个底层赋值单独发事件。源码行号指向干净 6.12.1 初始树；后续补丁应保留这些基准引用。

| Point ID | 公式 | 源码路径与权威写入 | 锁、事务和顺序 | 可独立检查的内容 / 插桩处理 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `MOUNT.COUNTERS_INIT` | `M-B/M-I/M-U` | [super.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/super.c#L1205) `ext4_percpu_param_init()` 调用 [balloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/balloc.c#L770) `ext4_count_free_clusters()`、[ialloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L1435) `ext4_count_free_inodes()` / `ext4_count_dirs()`，再初始化三个 percpu counter | mount 初始化路径；检查点需晚于 recovery、描述符/组状态准备和 counter 初始化 | 记录每个初始化返回值及来源；`M-*` 主要是“初始化来源”关系。它们不能证明 bitmap 与 descriptor 正确。bitmap 重扫是独立 `R-BG/R-IG` 式检查 | implemented；来源关系有 trace 与 snapshot 检查，不等同于独立 bitmap 判定 |
| `GROUP.BITMAP_INIT.LAZY` | `R-BG/R-IG`、局部 delta 转换分类 | [balloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/balloc.c#L181) `ext4_init_block_bitmap()` / `ext4_free_clusters_after_init()`；[ialloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L794) 新 inode 路径中的 `BLOCK_UNINIT` 初始化和 descriptor 修正；`ext4_init_inode_bitmap()`、`INODE_UNINIT` 读取/验证路径需一并审计 | `ext4_lock_group()` 下发布 `bg_flags`/descriptor；inode lazy init 由 `alloc_sem` 协调，后台 `ext4lazyinit` 路径见 `super.c:3669-3883` | 不能把初始化时预设的 metadata bits 当普通分配；记录 `uninit_before/after`、逻辑位图解释版本和 descriptor 调整量。bitmap 全量重算不放在 group spinlock 内 | pending |
| `BLOCK.ALLOC.LOCAL` | `R-BG/R-BDELTA` | [mballoc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/mballoc.c#L3980) `ext4_mb_mark_context()`；实际置位和 `C_g` 扣减在 4022-4051；入口 [mballoc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/mballoc.c#L4088) `ext4_mb_mark_diskspace_used()` | bitmap 与 descriptor 更新位于该 group lock 内，group unlock 后更新 flex summary；Journal write access 在持锁前取得 | 在该 helper 中记录实际 `changed`（从原 bitmap 状态算出）、group、bitmap 前后状态、`C_g` 前后值；不在锁内扫描整图。`changed` 是本次真实 bit 转换来源，不用请求长度代替 | implemented；真实 bitmap 变化与 descriptor 局部发布分点记录 |
| `BLOCK.ALLOC.GLOBAL` | `R-BSUM/R-BDELTA` | [mballoc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/mballoc.c#L4143) `s_freeclusters_counter` 扣减；常规 mballoc 成功路径 | 在 group lock 释放后，发生在 group descriptor 更新之后；可能与其他 CPU 更新交错 | 记录精确 update delta、CPU、operation context 和 event sequence。局部 point 与 global publish point 分开；不在此处断言全局 `R_B == sum(C_g)` | implemented；concurrent 事件保留原始顺序并按不确定性分类 |
| `BLOCK.FREE.LOCAL` | `R-BG/R-BDELTA` | [mballoc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/mballoc.c#L6401) `ext4_mb_clear_bb()` 调用 `ext4_mb_mark_context(..., false, ...)`；核心 bit 清除和 `C_g` 增加同 `BLOCK.ALLOC.LOCAL` 的 `super.c`/`mballoc.c` helper | group lock 内更新 bitmap/descriptor；journal transaction tid 用于追踪释放延迟复用 | 记录成功清除的 `changed`，区分当即进入 buddy 可重用态和 journal transaction 延迟释放态；跨 group 输入会拆成多次内部转换 | implemented；延迟复用与 replay 分类保留；长时间异步因果仍不作 syscall 归属推断 |
| `BLOCK.FREE.GLOBAL` | `R-BSUM/R-BDELTA` | [mballoc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/mballoc.c#L6531) `ext4_mb_clear_bb()` 普通 global add；[mballoc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/mballoc.c#L6664) `ext4_group_add_blocks()` | `EXT4_FREE_BLOCKS_RERESERVE_CLUSTER` 会抑制当前 global add；metadata free 可入 transaction 的 pending-free tree | 覆盖正常释放/组扩容的 `s_freeclusters_counter` 增量来源；记录 `flags`、transaction tid、真实 changed 数。`RERESERVE_CLUSTER` 不在此处算已发布；replay 见 `FAST_COMMIT.BLOCK.REPLAY` | implemented；rereserve 与 FC replay 独立核算 |
| `FAST_COMMIT.BLOCK.REPLAY` | `R-BG/R-BSUM/R-BDELTA` 特殊维护路径 | [fast_commit.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/fast_commit.c#L1837) `ext4_mb_mark_bb(..., false)` 释放；同文件 1965-1972 以 `true` 重放已分配 extent；[mballoc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/mballoc.c#L4159) `ext4_mb_mark_bb()` 逐 group 调 `ext4_mb_mark_context()`；[mballoc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/mballoc.c#L6378) `ext4_free_blocks_simple()` 是 replay 中 `ext4_free_blocks()` 的分支 | 仅在 `s_mount_state & EXT4_FC_REPLAY` 时发生；`EXT4_MB_SYNC_UPDATE` 只同步写 bitmap/descriptor，不更新 percpu counter；挂载之后初始化 global，helper 声称幂等 | 独立于普通 alloc/free 的 `replay` 分类：记录 replay tag/原因、实际 changed、group、局部 descriptor 变化和 replay 进度。不能把它混入活动期 syscall 的 delta，也不能据请求 range 代替 changed | implemented；定向 special 负载命中；只证明该 replay fixture 路径 |
| `BLOCK.BIGALLOC.RERESERVE` | `R-BDELTA` | [extents.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/extents.c#L2440) `ext4_rereserve_cluster()` 更新 `s_dirtyclusters_counter` 和 `s_freeclusters_counter`；其调用通过 `ext4_remove_space` 等路径 | inode reservation lock 包围 inode reservation 和 dirty counter 更新；global free counter 在锁外增加；前序 `ext4_free_blocks()` 可能使用 `RERESERVE_CLUSTER` 延迟 | 作为单独的 delayed publish point，记录 cluster、reservation 状态和相关 inode；不能把前序局部 free 当作 global `F_B` 已完成 | pending |
| `INODE.ALLOC.LOCAL` | `R-IG/R-IDELTA/R-UG/R-UDELTA` | [ialloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L923) `__ext4_new_inode()`：选零 bit 后在 1094-1108 置 inode bitmap；descriptor free-inodes 与目录 used-dirs 更新在 1177-1228 | bitmap bit 在 group lock 内设置；descriptor 更新另一次 group lock；`alloc_sem` 读锁与 lazy inode table 初始化协调 | 要区分 `bitmap bit claimed` 与 `descriptor/global publication`。对目录，记录 inode mode、group 和成功/失败状态；mkdir 后续可失败并 orphan/释放，不按请求计数 | pending |
| `INODE.ALLOC.GLOBAL` | `R-ISUM/R-USUM/R-IDELTA/R-UDELTA` | [ialloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L1237) 成功分配后的 `s_freeinodes_counter--`、目录的 `s_dirs_counter++` | 在 bitmap/descriptor group lock 释放且 descriptor dirty 标记后；仍在 inode 创建 transaction 内 | 分别记录 inode global 发布和 directory global 发布；不由 global 读数的旧快照反推 `A_I/A_U` | pending |
| `INODE.FREE.LOCAL` | `R-IG/R-IDELTA/R-UG/R-UDELTA` | [ialloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L234) `ext4_free_inode()`：bitmap clear 与 `I_g++` 在 314-331；目录释放时 `U_g--` | inode 真正回收时才清 bitmap；unlink/rmdir 使 nlink 为 0 不代表 inode 当场释放。group lock 内更新 bitmap/descriptor | 目录 R-U 以 inode 最终释放为准，不以 `rmdir` syscall 次数代替。记录 inode `i_count/i_nlink`、orphan/reclaim 阶段；持锁位置只记录局部值 | pending |
| `INODE.FREE.GLOBAL` | `R-ISUM/R-USUM/R-IDELTA/R-UDELTA` | [ialloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L333) 成功 bitmap clear 后 `s_freeinodes_counter++`、目录 `s_dirs_counter--` | free-inodes increment 在 group lock 外，directory global decrement 在 group lock 内；释放可由 iput/orphan cleanup 延迟触发，非 unlink 返回边界 | 作为后台/异步事件记录来源和 parent operation 关联；无关联时标记 asynchronous/unattributed | pending |
| `FAST_COMMIT.INODE.REPLAY` | `R-IG/R-ISUM/R-IDELTA/R-UG` 特殊维护路径 | [ialloc.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/ialloc.c#L751) `ext4_mark_inode_used()` 改 inode bitmap、`I_g`，以及必要时的 block bitmap/`C_g`；其两个调用者均为 [fast_commit.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/fast_commit.c#L1608) 与 [fast_commit.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/fast_commit.c#L1663) 的 FC replay 路径 | replay 写盘后调用；函数本身不更新 `s_freeinodes_counter`/`s_dirs_counter`。若遇 `INODE_UNINIT`/`BLOCK_UNINIT` 还会初始化相应 bitmap 并修正 descriptor | 作为 `replay` 而非普通 `A_I` 事件：记录 inode/group、是否清除 uninit、descriptor 前后值，以及该 inode 是否为目录。不得直接用此路径构造活动期 `R_IDELTA` expected | pending |
| `BITMAP.CORRUPT.EXCLUDE` | `M-B/M-I`、`R-BSUM/R-ISUM`、delta 特殊分类 | [super.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/super.c#L1096) `ext4_mark_group_bitmap_corrupted()`：首次标 corrupt 时从 `R_B/R_I` 扣除组的 `bb_free`/`I_g`；corrupt 标记来源遍布 `balloc.c`, `ialloc.c`, `mballoc.c` | atomic test-and-set 避免重复扣 global；可能表示健康计数域变化，不是普通 bitmap 分配 | 每个快照保存 corrupt group 集合/epoch；公式求和只对同一纳入域比较。以 corruption adjustment 事件解释变更，不报普通 delta mismatch | pending |
| `RESIZE.NEW_GROUP.DESC` | group 域、`P-SB`、delta 特殊转换 | [resize.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/resize.c#L1351) 初始化新 group descriptor；count 从 1375 起设置 | 先准备新 bitmap/descriptor 与 metadata，再在 `ext4_update_super()` 里发布容量及 `s_groups_count`；有 resize serialization 和 barrier 规则 | resize 前后 `G`、`N_g^B/N_g^I` 改变，普通固定容量 delta 不适用；先记录准备、启用、global counters 和容量发布各边界 | pending |
| `RESIZE.COUNTERS.PUBLISH` | `R-BSUM/R-ISUM/R-BDELTA/R-IDELTA/P-SB` | [resize.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/resize.c#L1419)：superblock 容量/free summary 更新，`s_groups_count` 增长，然后 global free counters 在 1491-1494 增加 | `lock_buffer(s_sbh)`；`smp_wmb()` 后发布 group count；percpu additions 在 group count 公开后 | 存在短暂“容量/descriptor 已发布但 global counter 尚更新”或 summary 发布差异。记录为 resize transition，不应用普通总量关系到不完整阶段 | pending |
| `FLEX.SUMMARY` | 无独立公式；用于核对源代码覆盖面 | [super.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/super.c#L3176) 初始化；mballoc/ialloc/resize 中有 flex-group atomics | 派生加速统计，不是本文的 `C_g/I_g/U_g` 或 percpu global counter 权威来源 | 不把 flex summary 当公式 expected 或 substitute；可记录变更但不为本矩阵单独设公式 probe | out of formula |
| `SUPER.PUBLISH` | `P-SB` | [super.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/super.c#L6081) `ext4_update_super()` 汇总 counter 并复制到 `s_es`；[super.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/super.c#L6155) `ext4_commit_super()` 随后提交/写 superblock | `s_sbh` buffer lock 下更新；聚合字段依次读入，其他 CPU 仍可更新 percpu counters。commit-super 还直接写 I/O 并等待 | 在字段复制完成后记录一对 `R_B/R_I` 和 `S_B/S_I`；若计数窗口不能锁定/版本验证，结果只能 `unverified`。区分内存汇总完成和 I/O 完成 | pending |
| `NAMESPACE.CREATE` | `R-LINK`; directory 对象生命周期也影响 `R-UDELTA` | [namei.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L2820) create / `ext4_add_nondir()`；mkdir 在 3000 起，new inode 由 `ialloc.c` 分配 | transaction 内先 inode/dirent/nlink 写入；失败可 `drop_nlink`、orphan、释放；mkdir 增加父 directory link count 在 3045 | 事件记录 dirent add、target nlink 和 parent nlink 更新顺序及 rollback；不得把创建 inode 数/目录项数混为 `R_U` | pending |
| `NAMESPACE.UNLINK.RMDIR` | `R-LINK`; 后续 `R-UDELTA` | [namei.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3139) rmdir；[namei.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3228) `__ext4_unlink()` | dirent 删除、nlink 更新和 orphan 操作在 handle 中；实际 inode free 往往在后续 iput/orphan cleanup | 记录 namespace update begin/dirent/nlink/handle-stop/end。rmdir 的 `U_g/R_U` 通常不是此处立即减，而是 free inode 路径；打开中的 unlink inode 可合法 `nlink=0` | pending |
| `NAMESPACE.LINK` | `R-LINK` | [namei.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3453) `__ext4_link()` | transaction 内先 inc_nlink，尝试 add entry；失败时 drop_nlink 回滚；handle stop 后结束 ext4 op | 记录 nlink 与 dirent 两步及失败回滚。全局引用扫描只放稳定 namespace snapshot | pending |
| `NAMESPACE.RENAME` | `R-LINK`，目录 nlink 的特例/哨兵约束 | [namei.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/namei.c#L3791) `ext4_rename()`；辅助函数 `ext4_setent/resetent/rename_delete/update_dir_count` 在 3635-3745 | 一个 journal handle 中可能依次写目标项、删旧项、更新目录 `..`、替换目标 inode link、dir nlink；错误处理可能 reset entry，不能把子步骤误判为稳定状态 | 记录各 namespace 子步骤和 rollback；`dir_nlink` feature 下 `i_nlink==1` 可能是未知子目录数哨兵，R-LINK 对此标 `unsupported`/special semantics | pending |
| `SNAPSHOT.MEMORY.STABLE` | `R-BG/R-BSUM/R-IG/R-ISUM/R-USUM/R-UG/R-LINK` 及完整 delta 窗口 | 读取 getter 位于 [super.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/super.c#L338)，bitmap 读/初始化在 `balloc.c`、`ialloc.c`；global 使用 percpu sum | 必须对 counter 更新者和 descriptor/bitmap 发布者建立有效的稳定窗口；逐 group lock 后相加不构成同一时刻快照。需覆盖 lazyinit、resize、orphan/reclaim、后台释放 | 扫描不要求 sync。若无法证明期间状态未变，全部结果写 `concurrent_or_ambiguous` 或 `unverified`；不把 checker 自己的锁当成 filesystem quiescence | pending |
| `DISK.POST.RECOVERY` | `P-DISK` | 非运行时源码点；分析磁盘副本的 superblock、group descriptors、bitmap。内核恢复语义见 [super.c](https://github.com/torvalds/linux/blob/v6.12.1/fs/ext4/super.c#L6207) `ext4_mark_recovery_complete()` | 干净卸载或正确冻结/持久化；崩溃镜像先保全原件，在副本上 replay 后再扫 | 离线工具比较同一持久状态的 `D_B/D_I`、descriptor sum、logical bitmap sum；单独保留 replay/fsck 输出和退出码。不要在 ext4 runtime 代码中插桩磁盘解析 | pending |

## 变量写入点盘点

对干净 6.12.1 的 `fs/ext4` 执行直接字段/helper/percpu 更新搜索，当前找到的权威路径如下。后续代码修改应重新运行检索并更新此表。

| 权威状态 | 直接写入口 | 主要调用/变化 |
| --- | --- | --- |
| `C_g` | `ext4_free_group_clusters_set()` 定义 `super.c:394` | `mballoc.c:4026,4041,4045`；`ialloc.c:814,1163` 的 BLOCK_UNINIT 初始化；`resize.c:1375` 新组初始化 |
| `I_g` | `ext4_free_inodes_set()` 定义 `super.c:402` | `ialloc.c:322,852,1214` 的 free/mark-used/alloc；resize 新组初始化还需复核新 descriptor 的默认值与 group publication |
| `U_g` | `ext4_used_dirs_set()` 定义 `super.c:410` | `ialloc.c:325,1216` 的目录 inode 最终 free/alloc |
| `R_B` | `s_freeclusters_counter` update | `mballoc.c:4143` alloc；`6534` free；`6718` group add；`extents.c:2452` bigalloc rereserve；`resize.c:1491` resize；`super.c:1111` corrupt exclusion |
| `R_I` | `s_freeinodes_counter` update | `ialloc.c:333,1237` free/alloc；`resize.c:1493` resize；`super.c:1122` corrupt exclusion |
| `R_U` | `s_dirs_counter` update | `ialloc.c:327,1239` free/alloc；初始化在 `super.c:1221` |
| `S_B/S_I` | `s_es` free summary | `super.c:1211,1216` 初始化来源；`super.c:6105-6111` 汇总发布；`resize.c:1453,1456` 容量变化时直接加新容量计数 |
| `i_nlink` | VFS helpers 与 ext4 特例 | `namei.c:2766-2782,2986,3045,3196,3207,3287,3468,3482,3741-3744,3961,3971,3978`；inode 从磁盘装入在 `inode.c:4808`。扫描器需排除 EA/reserved inode 并处理 `dir_nlink` 和 orphan lifecycle |

注意：通用 descriptor setter 只提供字段读写，不自动形成 probe 语义；`ext4_mb_mark_context()` 和 inode allocator/free path 才能提供 bitmap 实际转换数和相邻发布顺序。

## Boundary 与操作关联

| operation 类别 | 开始/内部边界 | ext4 操作结束候选 | 不可混淆之处 |
| --- | --- | --- | --- |
| block alloc/free | allocator 请求进入、`ext4_mb_mark_context` 实际 bit transition、global counter 发布 | 外层 extent/write/fallocate/truncate path 返回及 journal handle 结束 | allocator 请求不等于实际分配；free 可以 deferred、rereserved 或后台完成 |
| inode alloc/free | `__ext4_new_inode` / `ext4_free_inode` | create/mkdir 成功或失败、iput/orphan cleanup 的独立结束 | namespace syscall 返回不等于 inode 最终 bitmap free |
| link/unlink/mkdir/rmdir/rename | 对应 `namei.c` operation 和 dirent/nlink 子步骤 | ext4 inode operation 返回、`ext4_journal_stop`；另记录 syscall 边界需 VFS/trace 关联 | Journal stop 不等于 syscall return，也不自动代表 transaction 已 checkpoint |
| resize/lazy init | resize group prepare、flags/descriptor 写、capacity/group-count publish；lazyinit alloc_sem begin/end | resize operation return；每个 lazyinit group 完成 | 改变公式域 `G` 和容量；不得拿固定容量 delta 判定 |

operation ID 不能仅用 PID、inode 或 JBD2 tid。至少保留独立 event sequence、operation/parent operation ID、tid、group、inode、阶段和异步归属状态；不能精确关联时明确标注。

## 本轮发现的关键限制

- `ext4_count_free_clusters()` 默认只汇总 descriptor 且排除 corrupt group；不是 bitmap 独立重算。其 `EXT4FS_DEBUG` bitmap 统计使用固定 group bitmap 范围，不能直接作为支持 bigalloc、最后一个短 group、`BLOCK_UNINIT`、FLEX_BG 的新 checker expected。
- `ext4_count_free_inodes()` 默认 descriptor 求和；debug bitmap count 同样不能未经有效 bit/padding/`INODE_UNINIT` 审计就复用。
- `ext4_mark_inode_used()` 会改 inode bitmap 和 group descriptor，但该函数自身不更新 `s_freeinodes_counter`；已确认其两个调用者都在 fast-commit replay。它必须作为 replay 事件建模，不能把所有 inode bit transition 直接计入 active-runtime 的 expected。
- fast-commit 还有独立的 block replay helper `ext4_mb_mark_bb()`：它按 group 同步写 bitmap/descriptor，不在该 helper 更新 global；恢复后的 global 由挂载初始化，且被 `ext4_free_blocks_simple()` 的 FC-replay 分支间接调用。它与普通 syscall 的 `ext4_mb_mark_context()` 需要用 `replay` 标签区分；replay helper 的“幂等”不替代本次实际 changed 的记录。
- bitmap corruption 会从 global counter 排除某 group，group descriptor 字段仍可能保留；求和域必须记录 corrupt group 集合。
- online resize 中新 descriptor、superblock 容量、`s_groups_count`、percpu counters 分阶段公开。Phase 2 普通 syscall checker可先将 resize 标记为 special/unverified，但矩阵保留其写入覆盖，避免误报或漏算。
- `R-LINK` 需要完整 namespace 和 inode 生命周期判定。锁内/transaction 内不得遍历全 filesystem；稳定扫描需要独立机制，并对 `dir_nlink`、open-unlinked inode、orphan、EA inode 和 reserved inode 分类。
- `P-SB` 是内存 `s_es` 缓冲内容。`ext4_commit_super()` 会提交同步写，但这仍不等同于所有 descriptor/bitmap 与该值构成一致的最新磁盘快照；`P-DISK` 留给离线同状态检查。

## 覆盖状态与下一步

矩阵 review 及第一版 API/快照协议已完成，实现和限制见 `PROBE-API-SNAPSHOT.md`。原始上游树保持不变，probe 在 `dev/linux-6.12.1/` 中开发；不能将部分实现理解为全部公式或特殊路径已验证。

## 第一版实现状态

| 公式/点位 | 当前状态 | 实现与边界 |
| --- | --- | --- |
| `M-B/M-I/M-U` | implemented | 初始化后 descriptor 与 global 来源比较；corrupt/error 状态 unverified，不证明 bitmap 正确 |
| `R-BG/R-IG/R-BSUM/R-ISUM/R-USUM` | implemented | sysfs 乐观稳定快照，活动窗口与版本校验；无效扫描保留观察值但不判错 |
| `R-BDELTA/R-IDELTA/R-UDELTA` | implemented，完整健康 trace 窗口 | 独立 bitmap/目录生命周期事件与 global 发布事件分开记录；分析器比较稳定快照的真实总量变化；特殊 adjustment 已由 special analyzer 独立检查 |
| `R-UG/R-LINK` | implemented（运行时） | `counter_snapshot` 全 filesystem 扫描：inode bitmap + `ext4_iget()` + 目录数据块 dirent 引用，与 `i_nlink` 逐 inode 比较；writer 活动、FC replay、resize、corrupt/error 状态下记 `unverified`。离线 `check-disk.py` 独立实现同一公式的持久镜像版本 |
| `P-SB` | implemented | 同次发布使用的样本与内存字段复制结果比较，block/inode 分开记录 |
| `P-DISK` | implemented | 离线 `scripts/check-disk.py` 独立解析 superblock、descriptor、bitmap、inode table、extent directory；smoke 四种镜像通过，负向篡改测试通过 |
| block/inode 普通转换与发布 | implemented | 外层 begin/end 覆盖 bitmap 到 global 的窗口；真实 bit 转换独立记录，上游请求长度与 changed 均未改变 |
| bigalloc rereserve | implemented，独立 expected，已命中 | `ext4_rereserve_cluster()` 记录 free cluster +1 与 dirtyclusters +1；special analyzer 检查两次事件及 delta |
| corrupt exclusion/resize/FC replay | implemented，独立 expected，已命中 | corruption 扣减 group `bb_free`/inode 计数；resize 发布 clusters/inodes/groups；FC block replay 与 inode replay（global/descriptor 不变）分别断言；special analyzer 强制检查全部点位 |
| lazy init | implemented coverage；细粒度阶段 pending | 计数写入由 allocator/replay 活动窗口覆盖；snapshot 使用上游逻辑 bitmap 初始化，不直接数未初始化磁盘位 |
| namespace create/link/unlink/mkdir/rmdir/rename 等 | implemented，含子步骤 | VFS inode-op wrapper、唯一嵌套 operation ID；dirent add/delete 与 nlink inc/dec/clear/rollback 均单独成事件 |
| trace loss detection | implemented | mount epoch/序号及端点、BEGIN/END、per-CPU dropped/overrun；缺证据则失败 |

操作上下文在当前线程嵌套范围传播，后台任务独立归属；跨线程 parent 与准确 syscall 返回关联仍 pending。稳定扫描在 writer 静止时给出有效 R-UG/R-LINK 结论，竞争窗口明确返回 unverified。

## 第一条链的实现更新（2026-10-10）

详细实现、语义、点位和限制见 [FIRST-CHAIN.md](FIRST-CHAIN.md)。
普通活动期 `R-BDELTA/R-IDELTA/R-UDELTA` 已有独立转换、descriptor、global 的逐点流，
block descriptor LOCAL 在锁内记录真实发布增量；后续 PUBLISH 不再次累计。
`R-UDELTA` 补齐目录 descriptor 事件，bitmap 已清除但后续发布失败仍保留目录 release。
`R-LINK` 补齐 low-level applied insert/delete、rename setent/..、live dot、inline init、
rollback/tmpfile 与 inode birth/death。稳定扫描支持 inline 内容，并验证 nlink0 的零引用。
分析输出 group/全局/每 inode 差值生命周期、外层 END 与原生 syscall 出口、独立稳定
确认及重建连续性。跨线程/后台精确因果未实现，按不确定报告；special 域需独立基线。
