# 原有错误注入 hook：重复、上下文与重建映射

日期：2026-11-16。核对 HEAD：`82a6ae8`；本地开发源码以文末 SHA-256 为准。

**状态：静态审计与迁移建议。** 本文没有删除或改写内核 hook，没有完成新引擎实现，也没有运行新的 VM 注入验收。88 项已完成登记清点与初步分类；具体调用链仅在下文列出的案例中人工核对，不能把整个表读成 88 项语义等价性证明。

配套主文档：[运行时一致性主流程与错误注入重建设计](错误注入优化完整方案.md)。

## 1. 直接结论

原有 hook **存在共同失败原语、同边界不同名称以及上下层重叠**。也存在相反问题：一个名称覆盖不同对象、分支与前态。合理的重建既要合并重复实施，也要保留或补全上下文，不能只按名称数量做删减。

| 审计层级 | 当前结果 | 能说明什么 |
| --- | --- | --- |
| operation / enum / parser / name-switch 登记 | `hook_formula_matrix.py --check` 通过，88 个 fault entries、6 个 observation-only entries | 未发现该校验覆盖范围内的注册冲突 |
| 按 catalog 的 errno 来源文字分类 | write-access 35 项、dirty-metadata 41 项、其他 12 项 | 很多名称属于共同原语族；不是物理 site 数或可删除数量 |
| 同一源码调用由不同 operation 选择 | 已核对 bitmap access 四名、resize/xattr 三组、目录删除三名等 | 应共享实现，保留真实上下文 |
| caller 与 callee 两层注入 | 已核对 resize-buddy → common-buddy 调用链 | 存在叠层候选，需要唯一施加者；不等于已发生双重注入 |
| 名称相近但对象/阶段不同 | 已核对 rmdir child/parent dirty 等 | 不能仅因原语相同就删除区别 |
| 一个名称包含多个前态 | 已核对 mkdir append、inline conversion | 需要补充 context/必要时区分 site |

本文不声称“88 个点都是独立的”，也不声称“88 个只剩 14 个”。新点数由边界、副作用和上下文审查决定。

## 2. 为什么已有检查不能证明没有重复

`scripts/hook_formula_matrix.py` 中：

- `mutation_stage()` 主要从名称后缀推导阶段；`return_contract()` 主要从 `return_path` 的文字关键词分类。
- `build()` 生成 `canonical_key = operation|stage|contract`。
- `validate()` 检查 hook ID、operation、enum、fixture 与 canonical key 唯一，还检查源码符号和登记的一致性。

因为 operation 名称本来就唯一，canonical key 唯一并没有证明两个名称的真实失败边界、前态、返回效果或清理路径不同。该工具有登记完整性价值，不能替代语义去重。[S1]

本次只读执行：

```sh
python3 experiments/ext4-counter-trace/scripts/hook_formula_matrix.py --check
```

输出为 `passed=true, issues=[], hooks=88, observation_only=6`。它不是注入运行测试或 oracle 正确性测试。

## 3. 已人工核对的调用边界

### A1：四个名字共享 bitmap write-access 边界

`mballoc.c:ext4_mb_mark_context()` 约第 4030–4044 行根据 flag/state 选择：

```text
resize flag  → resize_extend_add_blocks
xattr flag   → xattr_release_bitmap_access
state=true   → mballoc_mark
state=false  → mballoc_free_mark
```

随后都把选中的枚举传入第 4059 行同一处 `ext4_journal_get_write_access_faultable(... bitmap_bh ...)`。[S2]

**已确认的重复层次：** 同一精确调用边界上的 context aliases；已有 wrapper 本身也复用实现，并非四份完全复制的函数。

**重建设计：** 一个 write-access 原语、一个 bitmap-access site，context 保留 alloc/free/resize/xattr-release。释放路径的错误消耗、resize 的容量更新与分配路径不同，不能把四个上下文合并成一个测试结果。

### A2：resize 与 xattr-release 的其他共享边界

同一个 `ext4_mb_mark_context()` 内还根据 context 选择三组枚举：[S2]

| 普通源码调用边界 | 旧名称 |
| --- | --- |
| 第 4077 行 descriptor write-access | `resize_extend_group_desc_access` / `xattr_release_group_desc_access` |
| 第 4161 行 bitmap dirty | `resize_extend_bitmap_dirty` / `xattr_release_bitmap_dirty` |
| 第 4170 行 descriptor dirty | `resize_extend_group_desc_dirty` / `xattr_release_group_desc_dirty` |

每一行内部共享 site/context 分流；三行之间不应合并为同一个机会。它们访问不同 buffer，dirty 发生时 bitmap/descriptor 等更新已发生，与先前 access 边界的失败前态不同。

### A3：目录删除的共同实现与不同业务上下文

`rmdir_dirent_delete`、`unlink_dirent_delete`、`rename_delete_old` 在普通目录块分支进入 `namei.c:ext4_delete_entry_internal()`，在约第 2915 行共用同一个 write-access 调用。对应业务调用点可见 `ext4_rmdir()`、`__ext4_unlink()`、`ext4_rename_delete()`。[S3]

新模型可以共享这一边界，context 保留 rmdir/unlink/rename 和对象角色。该函数前面还有 inline-directory 分支，旧 operation 不能被误认为永远只有这一物理位置；inline 分支仍需独立追踪与契约审查。

### A4：resize-buddy 与 common-buddy 是上下叠层

已核对调用链：[S2]

```text
ext4_group_add_blocks()
  ├─ resize_extend_buddy_load       调用前，约第 6860 行
  └─ ext4_mb_load_buddy()           约第 1739 行，仅转调
       └─ ext4_mb_load_buddy_gfp()
            └─ mb_buddy_load        helper 入口，约第 1595 行
```

外层未注入时会进入内层。同一 resize buddy 请求可以经过两个登记机会；两者都位于此 helper 的实际 buddy 准备之前，但元数据参数、trace、匹配计数以及进入 helper 的诊断行为不同。**本次证明了叠层，不把两者对所有执行完全等价当作已验证结论。** 当前每段至多一次命中的配置也意味着“两个机会”不等于“一次执行已注入两次”。

重建时优先选择共同 helper 的规范边界，resize 信息通过 context 传递，取消调用前同类失败的施加者。迁移验收需逐 errno 比较调用者返回、清理、对象状态和必需副作用；不相同则按真实失败阶段分别建模。自然失败是否需要进入 helper 内更深层的分配/读取边界，另由失败真实性契约决定。

### A5：rmdir 子 inode 与父 inode 不能合并

在 `namei.c:ext4_rmdir()` 中，删除 dirent 后，先清除子 inode 的 nlink、设置 size 并进行 orphan 处理，再到 `rmdir_child_dirty`（约第 3462 行）；成功继续后才递减父目录计数并到 `rmdir_parent_dirty`（约第 3469 行）。[S3]

二者的对象、已经完成的更新和失败后的出口均有差异。可以共享 dirty-metadata 原语，不能丢掉 child/parent context。与它们配对的 access 也不能和 dirty 合并：`ext4_mark_inode_dirty_faultable()` 先经 `__ext4_reserve_inode_write()` 取得访问，之后才更新 raw inode 并到 dirty 边界。[S4]

### A6：mkdir 单一名称混合子目录与父目录扩展

`namei.c` 开头的 `ext4_mkdir_child_faults` 和 `ext4_mkdir_parent_faults` 都配置 `mkdir_append_alloc` 与 `mkdir_append_block_access`。前者用于新建子目录，后者可在向父目录加入条目而需扩展时进入；实际 helper 是 `ext4_append_faultable()`。[S3]

因此保留共同 primitive/site 时至少要增加 `object_role=child_dir/parent_dir` 与真实 branch/scope。父目录的 htree split 等进一步调用仍需追踪，不能仅凭调用次数区分所有语义。

### A7：inline conversion 单一名称混合不同前态

`inline_data_block_write_begin` 由 `ext4_block_write_begin_faultable()` 提供。`ext4_convert_inline_data_to_extent()` 中，调用前已经执行 `ext4_destroy_inline_data_nolock()`；`ext4_da_convert_inline_data_to_extent()` 的调用前仍保留 inline 表示。前一条路径还按 `dioread_nolock` 选择不同 get-block 回调。[S5]

新模型需要保存转换路径、inline 状态和回调类别，不能只用该旧名称或一个模糊的“conversion”标签复现。此次核对前态和调用链，没有宣称相关场景均已通过动态测试。

### A8：共享 JBD2 原语不等于所有 dirty/access 都重复

`ext4_jbd2.c` 的两个 faultable wrapper 已集中部分共同逻辑；write-access wrapper 包含 journal/handle abort 处理，dirty wrapper 保留 buffer 标志和共同错误处理。现有代码不是完全没有失败副作用模型。[S6]

35/41 的登记分类只说明有原语复用空间，不能推出所有调用者、buffer、阶段或前置状态等价。重建要重新校验契约，尤其 EIO、EROFS、credit ENOSPC、checksum/corruption 的状态条件；errno 名称合法不代表本轮达到了对应原生分支。

## 4. 去重规则与迁移状态

按下面的维度比较，不能只比较名称、errno 集合或共享 helper：

`primitive + 实际失败边界 + 调用前状态 + 对象角色 + 更新阶段 + 所需副作用 + 返回/消耗与清理路径`

| 状态 | 意义 |
| --- | --- |
| `share_primitive` | 共用后端实现与契约模板，site/context 不一定合并 |
| `merge_boundary` | 已确认同边界别名，映射到一个 site 并保留 context |
| `retain_context` | 对象、时序或错误传播不同，必须保留可选择性 |
| `split_context` | 一个旧 ID 隐藏多个前态/调用来源，需补充区别 |
| `replace_outer_layer` | 同一失败请求多层施加；审查后只保留指定 owner |
| `defer` | 当前 workload、公式前提或失败模型尚不支持，不进入新默认 registry |
| `review_pending` | 只有登记/词法证据，尚无完整语义与动态验证 |

这些是设计状态，可以组合。没有新契约与 witness 的条目不能仅凭这张迁移表获得 campaign 准入资格。

此次审计针对故障施加点。`ext4_ct_*`、计数转换、dirent/nlink 和生命周期观测属于 checker 的事实来源；即使两个扰动别名合并，也不能顺带删掉必要观测。

## 5. 全部 88 项的初步清单

原语族依据 catalog 的 errno 来源文字识别：`JWA` = journal write-access，`JDM` = journal dirty-metadata，`OTHER` = 其他登记。表内“登记级”表示只做清点/分类，不表示已检查真实边界。A1–A8 引用上文人工核对的范围；仍不代表动态验收。

| # | 旧 operation | 登记原语族 | catalog 首个源码锚点 | 初始处理建议 | 本次核验范围 |
| --- | --- | --- | --- | --- | --- |
| 1 | `mballoc_mark` | JWA | `mballoc.c:ext4_mb_mark_context` | merge_boundary + retain_context | A1：共同边界已核；动态待验 |
| 2 | `mballoc_free_mark` | JWA | `mballoc.c:ext4_mb_mark_context` | merge_boundary + retain_context | A1：共同边界已核；动态待验 |
| 3 | `inode_evict_final_access` | JWA | `inode.c:ext4_mark_inode_dirty_faultable` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 4 | `inode_evict_final_dirty` | JDM | `inode.c:ext4_mark_inode_dirty_faultable` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 5 | `orphan_file_delete_access` | JWA | `orphan.c:ext4_orphan_file_del` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 6 | `orphan_file_delete_dirty` | JDM | `orphan.c:ext4_orphan_file_del` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 7 | `xattr_release_access` | JWA | `xattr.c:ext4_xattr_release_block` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 8 | `xattr_release_dirty` | JDM | `xattr.c:ext4_xattr_release_block` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 9 | `rmdir_dirent_delete` | JWA | `namei.c:ext4_delete_entry_internal` | merge_boundary + retain_context | A3：普通目录块分支已核；inline 待细审 |
| 10 | `rmdir_dirent_dirty` | JDM | `namei.c:ext4_rmdir` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 11 | `rmdir_child_dirty` | JDM | `namei.c:ext4_rmdir` | share_primitive + retain_context | A5：对象/阶段及配对链已核；动态待验 |
| 12 | `rmdir_parent_dirty` | JDM | `namei.c:ext4_rmdir` | share_primitive + retain_context | A5：对象/阶段及配对链已核；动态待验 |
| 13 | `unlink_dirent_dirty` | JDM | `namei.c:__ext4_unlink` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 14 | `unlink_dirent_delete` | JWA | `namei.c:ext4_delete_entry_internal` | merge_boundary + retain_context | A3：普通目录块分支已核；inline 待细审 |
| 15 | `unlink_dirent_block_dirty` | JDM | `namei.c:__ext4_unlink` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 16 | `unlink_inode_dirty` | JDM | `namei.c:__ext4_unlink` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 17 | `rename_dir_finish` | JDM | `namei.c:ext4_rename_dir_finish` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 18 | `rename_dir_prepare_access` | JWA | `namei.c:ext4_rename_dir_prepare` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 19 | `rename_exchange_new_parent_dirty` | JDM | `namei.c:ext4_update_dir_count` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 20 | `rename_exchange_old_parent_dirty` | JDM | `namei.c:ext4_update_dir_count` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 21 | `rename_delete_old` | JWA | `namei.c:ext4_delete_entry_internal` | merge_boundary + retain_context | A3：普通目录块分支已核；inline 待细审 |
| 22 | `rename_old_inode_dirty` | JDM | `namei.c:ext4_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 23 | `rename_new_parent_dirty` | JDM | `namei.c:ext4_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 24 | `rename_old_parent_dirty` | JDM | `namei.c:ext4_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 25 | `rename_replaced_inode_dirty` | JDM | `namei.c:ext4_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 26 | `rename_setent_access` | JWA | `namei.c:ext4_setent` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 27 | `rename_setent_dirty` | JDM | `namei.c:ext4_setent` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 28 | `rename_setent_inode_dirty` | JDM | `namei.c:ext4_setent` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 29 | `rename_delete_old_dirty` | JDM | `namei.c:ext4_rename_delete` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 30 | `mkdir_child_dirty` | JDM | `namei.c:ext4_mkdir` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 31 | `mkdir_dirblock_dirty` | JDM | `namei.c:ext4_init_new_dir_faultable` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 32 | `mkdir_parent_dirty` | JDM | `namei.c:ext4_mkdir` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 33 | `resize_extend_add_blocks` | JWA | `mballoc.c:ext4_mb_mark_context` | merge_boundary + retain_context | A1：共同边界已核；动态待验 |
| 34 | `resize_extend_super_access` | JWA | `resize.c:ext4_group_extend_no_check` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 35 | `group_add_desc_access` | JWA | `resize.c:ext4_add_new_descs` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 36 | `group_add_desc_dirty` | JDM | `resize.c:ext4_setup_new_descs` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 37 | `group_add_super_dirty` | JDM | `resize.c:ext4_flex_group_add` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 38 | `resize_extend_group_desc_access` | JWA | `mballoc.c:ext4_mb_mark_context` | merge_boundary + retain_context | A2：分组三个边界已核；动态待验 |
| 39 | `resize_extend_bitmap_dirty` | JDM | `mballoc.c:ext4_mb_mark_context` | merge_boundary + retain_context | A2：分组三个边界已核；动态待验 |
| 40 | `resize_extend_group_desc_dirty` | JDM | `mballoc.c:ext4_mb_mark_context` | merge_boundary + retain_context | A2：分组三个边界已核；动态待验 |
| 41 | `resize_extend_super_dirty` | JDM | `resize.c:ext4_group_extend_no_check` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 42 | `xattr_release_bitmap_access` | JWA | `xattr.c:ext4_xattr_release_block` | merge_boundary + retain_context | A1：共同边界已核；动态待验 |
| 43 | `xattr_release_group_desc_access` | JWA | `xattr.c:ext4_xattr_release_block` | merge_boundary + retain_context | A2：分组三个边界已核；动态待验 |
| 44 | `xattr_release_bitmap_dirty` | JDM | `xattr.c:ext4_xattr_release_block` | merge_boundary + retain_context | A2：分组三个边界已核；动态待验 |
| 45 | `xattr_release_group_desc_dirty` | JDM | `xattr.c:ext4_xattr_release_block` | merge_boundary + retain_context | A2：分组三个边界已核；动态待验 |
| 46 | `fast_commit_inode_claim` | OTHER | `ialloc.c:ext4_mark_inode_used` | defer：独立 recovery 协议 | 登记级；不进入默认 runtime 注入集合 |
| 47 | `mkdir_child_access` | JWA | `namei.c:ext4_mkdir` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 48 | `mkdir_parent_access` | JWA | `namei.c:ext4_mkdir` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 49 | `rmdir_child_access` | JWA | `namei.c:ext4_rmdir` | share_primitive + retain_context | A5：对象/阶段及配对链已核；动态待验 |
| 50 | `rmdir_parent_access` | JWA | `namei.c:ext4_rmdir` | share_primitive + retain_context | A5：对象/阶段及配对链已核；动态待验 |
| 51 | `unlink_dirent_inode_access` | JWA | `namei.c:__ext4_unlink` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 52 | `unlink_inode_access` | JWA | `namei.c:__ext4_unlink` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 53 | `rename_dir_finish_inode_access` | JWA | `namei.c:ext4_rename_dir_finish` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 54 | `rename_setent_inode_access` | JWA | `namei.c:ext4_setent` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 55 | `rename_old_inode_access` | JWA | `namei.c:ext4_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 56 | `rename_new_parent_access` | JWA | `namei.c:ext4_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 57 | `rename_old_parent_access` | JWA | `namei.c:ext4_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 58 | `rename_replaced_inode_access` | JWA | `namei.c:ext4_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 59 | `rename_exchange_new_parent_access` | JWA | `namei.c:ext4_cross_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 60 | `rename_exchange_old_parent_access` | JWA | `namei.c:ext4_cross_rename` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 61 | `resize_extend_buddy_load` | OTHER | `mballoc.c:ext4_group_add_blocks` | replace_outer_layer 候选 | A4：叠层已核；逐 errno 等价性待验 |
| 62 | `resize_extend_bitmap_read` | OTHER | `mballoc.c:ext4_mb_mark_context` | review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 63 | `group_add_super_access` | JWA | `resize.c:ext4_flex_group_add` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 64 | `group_add_bitmap_read` | OTHER | `resize.c:ext4_set_bitmap_checksums` | review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 65 | `group_add_groupinfo_alloc` | OTHER | `resize.c:ext4_setup_new_descs` | review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 66 | `mkdir_append_alloc` | OTHER | `namei.c:ext4_append_faultable` | split_context + retain_context | A6：child/parent 共用已核；其他分支待细审 |
| 67 | `mkdir_add_entry_read` | OTHER | `namei.c:ext4_add_entry` | review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 68 | `mkdir_append_block_access` | JWA | `namei.c:ext4_append_faultable` | split_context + retain_context | A6：child/parent 共用已核；其他分支待细审 |
| 69 | `mkdir_add_entry_access` | JWA | `namei.c:add_dirent_to_buf` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 70 | `mkdir_add_entry_dirty` | JDM | `namei.c:add_dirent_to_buf` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 71 | `inode_evict_bitmap_read` | OTHER | `ialloc.c:ext4_free_inode` | review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 72 | `inode_evict_bitmap_access` | JWA | `ialloc.c:ext4_free_inode` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 73 | `inode_evict_group_desc_access` | JWA | `ialloc.c:ext4_free_inode` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 74 | `inode_evict_bitmap_dirty` | JDM | `ialloc.c:ext4_free_inode` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 75 | `inode_evict_group_desc_dirty` | JDM | `ialloc.c:ext4_free_inode` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 76 | `migrate_swap_inode_data` | JDM | `migrate.c:ext4_ext_swap_inode_data` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 77 | `xattr_set_inode_dirty` | JDM | `xattr.c:ext4_xattr_set_handle` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 78 | `ea_inode_ref_dirty` | JDM | `xattr.c:ext4_xattr_inode_update_ref` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 79 | `acl_mode_dirty` | JDM | `acl.c:ext4_set_acl` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 80 | `inline_dir_block_dirty` | JDM | `inline.c:ext4_finish_convert_inline_dir` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 81 | `inline_dir_inode_dirty` | JDM | `inline.c:ext4_finish_convert_inline_dir` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 82 | `fileattr_flags_dirty` | JDM | `ioctl.c:ext4_ioctl_setflags` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 83 | `fileattr_project_dirty` | JDM | `ioctl.c:ext4_ioctl_setproject` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 84 | `inline_data_block_write_begin` | OTHER | `inline.c:ext4_convert_inline_data_to_extent` | split_context | A7：不同转换前态已核；动态待验 |
| 85 | `es_insert_alloc` | OTHER | `extents_status.c:__es_insert_extent` | review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 86 | `delalloc_reserve` | OTHER | `inode.c:ext4_da_reserve_space` | review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 87 | `extent_insert_dirty` | JDM | `extents.c:__ext4_ext_dirty_faultable` | share_primitive + review_pending | 登记级；实际 site/context/model 待逐项核验 |
| 88 | `mb_buddy_load` | OTHER | `mballoc.c:ext4_mb_load_buddy_gfp` | retain_context / 规范 owner 候选 | A4：共同 helper 已核；深层模型待审 |

## 6. 新子系统的验收要求

1. 关闭注入后，正常 workload、运行时 checker、输入/状态反馈与报告完整工作，不读取 legacy catalog。
2. 同一动态失败请求只有一个 owner；别名不增加机会数或覆盖奖励；动态 attempt 身份不进入新颖性计数。
3. bitmap/descriptor、父/子对象、更新前/后等有意义差异仍可精确选择。
4. 同 syscall 中重复调用按独立 occurrence 选择，下一 syscall 与退出清理不继承计划。
5. `off/observe/planned` 分开验证；实际原生返回和替代返回来源清楚。
6. 每个新组合包含失败前提、副作用、返回/消耗路径、有效公式边界、无注入控制与重放证据。
7. 旧/新注入器不可同时启用；旧证据通过固定 legacy 解释器读取，新控制接口不要求兼容旧 operation。
8. 以有效运行时关系状态、独立根因和总体成本评价，不能把压缩后的 hook 数量当作研究成果。

## 7. 复核来源与指纹

源码行号只对应本次本地工作树，主要以符号和内容 hash 定位。登记分类可按以下只读脚本复核；它有意不宣称完成语义去重。

```python
import json
from collections import Counter
from pathlib import Path

catalog = json.loads(Path(
    "experiments/ext4-counter-trace/fault-errno-catalog.json").read_text())
counts = Counter()
for hook in catalog["hooks"].values():
    sources = " ".join(x["source"] for x in hook["native_errnos"])
    family = ("JWA" if "jbd2_journal_get_write_access" in sources else
              "JDM" if "jbd2_journal_dirty_metadata" in sources else "OTHER")
    counts[family] += 1
print(len(catalog["hooks"]), dict(counts))
# 本次输出：88 {'JWA': 35, 'JDM': 41, 'OTHER': 12}
```

以下路径均相对 `experiments/ext4-counter-trace/`。

| 文件 | SHA-256 |
| --- | --- |
| `fault-errno-catalog.json` | `a397b72649f296293734b07be9f69121081fa878b3b2412077b5e8b41617a2cf` |
| `scripts/hook_formula_matrix.py` | `0ffa59df357d238b2e53c260054623bc075709726b786dfb70e6243f34dd3379` |
| `dev/linux-6.12.1/fs/ext4/mballoc.c` | `8aa6a11fa208cc9306b6abbd5e0aa6422176098ecaa0f4c1ee80a8e5be65ccf7` |
| `dev/linux-6.12.1/fs/ext4/namei.c` | `ed7bcbe6b0a0d53179105a8ce512da9700f0728d169d36e83ce6380bae6683aa` |
| `dev/linux-6.12.1/fs/ext4/inode.c` | `153cc1ec967c12d3dd761ccd9e415e07a7c62e83189fe689f7c1063cfe97360e` |
| `dev/linux-6.12.1/fs/ext4/inline.c` | `d879ae5ea385511b08b501b585580e37e62b41253afd637a673576251f7bf908` |
| `dev/linux-6.12.1/fs/ext4/ext4_jbd2.c` | `fa8cdb4d47aee6ec6cd12e4954d99d9cc54eac184203efbef42db3959add4780` |

[S1]: experiments/ext4-counter-trace/scripts/hook_formula_matrix.py
[S2]: experiments/ext4-counter-trace/dev/linux-6.12.1/fs/ext4/mballoc.c
[S3]: experiments/ext4-counter-trace/dev/linux-6.12.1/fs/ext4/namei.c
[S4]: experiments/ext4-counter-trace/dev/linux-6.12.1/fs/ext4/inode.c
[S5]: experiments/ext4-counter-trace/dev/linux-6.12.1/fs/ext4/inline.c
[S6]: experiments/ext4-counter-trace/dev/linux-6.12.1/fs/ext4/ext4_jbd2.c
