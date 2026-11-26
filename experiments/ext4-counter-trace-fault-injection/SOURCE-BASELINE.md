# Linux 6.12.1 源码基线

本实验以官方 **Linux 6.12.1** 为被测对象。允许的源码差异只有公式追踪、checker、
错误点观测、故障注入及它们需要的构建配置和接口；不合入漏洞修复或额外的生产路径校验。
未计划注入时，保留上游的原调用、返回值、错误处理和资源释放顺序。

## 补丁与开发树

官方对照来自原实验保存的 [Linux 6.12.1](../ext4-counter-trace/kernel/linux-6.12.1)，
发行包 SHA256 为 `0193b1d86dd372ec891bae799f6da20deef16fc199f30080a4ea9de8cef0c619`。
逐项基准见 [pristine-manifest.jsonl](../ext4-counter-trace/source/pristine-manifest.jsonl)。

在官方源码上依次应用：

1. [checker 与公式插桩](../ext4-counter-trace/patches/0001-counter-trace-core-and-probes.patch)。
2. [错误点观测](patches/0001-reviewed-error-site-trace.patch)。
3. [本实验调整和故障注入](patches/0002-formula-gated-injection.patch)。

第三层由 [configure-injection.py](scripts/configure-injection.py) 生成，同时撤销前两层中
夹带的非插桩改动。因此必须检查**三层补丁全部应用后的最终源码**，不能只看某一层。
实际编译目录为 [本实验开发树](dev/linux-6.12.1)。

## 已恢复的上游行为

| 位置 | 最终被测行为 |
| --- | --- |
| extent tree 增深 | `sb_getblk_gfp()` 失败后返回 `-ENOMEM`，不额外调用 `ext4_free_blocks()` 撤销分配；保留 checker 观察原有块泄漏问题 |
| unlink | 撤销删除目录项之前新增的两次 inode 写权限预留，恢复上游调用和释放顺序 |
| inline data | `ext4_prepare_inline_data()` 长度参数恢复为 `unsigned int`，不带后续长度溢出修复 |
| orphan file 初始化 | 移除新增的大小、对齐和块数拒绝条件，恢复 `inode->i_size >> sb->s_blocksize_bits` |
| xattr | 移除新增的 header 引用计数拒绝条件；恢复原有 `!ext4_handle_valid(handle)` 分支 |
| mballoc | 恢复 `AGGRESSIVE_CHECK__`，不额外启用 allocator 内部检查 |
| namespace | mkdir 回滚只清零一次链接数；rename 只写入一次目录项 inode；delete 成功后直接返回，只记录一次成功事件 |
| dirty metadata | 恢复原版函数和 journal/no-journal 分支，保留原错误点包装 |

同时恢复无关的空白、注释和分支/返回值改写。checker 的记录、采样和操作范围包装仍然存在；
它们不负责修复被测文件系统。

## 完整源码检查

[efi_source_audit.py](scripts/efi_source_audit.py) 从官方清单出发，逐层核对补丁前后哈希，
得到唯一允许的最终源码。它读取开发树中的每一个普通文件，并检查目录、符号链接目标和可执行位。
硬链接文件也会重新计算内容哈希。源码树中新增未列出的文件、删除文件或修改任何未插桩文件均会失败；
编译产物使用独立的 `O=` 目录。

```sh
# 在本实验目录执行；生成器确定性检查、补丁重放和完整开发树校验。
python3 scripts/configure-injection.py --check --prepare-source
```

当前通过检查的开发树含 86,630 个普通文件：86,581 个与官方内容完全相同，
37 个上游文件包含允许的插桩差异，12 个为新增插桩文件；另有 62 个符号链接。
完整检查报告位于 [source-audit.json](build/linux-error-site-trace/source-audit.json)，
记录所有差异文件、官方/最终内容哈希和补丁栈哈希。

[构建脚本](scripts/build.sh) 和 [campaign 入口](scripts/run-campaign.py) 会自动执行检查。
源码报告与内核、配置、补丁一起纳入构建绑定，并复制到新 campaign 的结果目录。
内容哈希检查负责发现未经审阅的改动；允许的差异通过源码审阅和关键错误路径回归验证。

[源码回归测试](tests/test-injection.py) 对补丁重放后的实际源码检查上游 xattr 校验、
dirty-metadata 和 rename 分支，确认 unlink 顺序、extent 错误路径及 namespace 写入次数；
另通过篡改临时源码树，确认清单外修改、缺文件、额外文件、链接和可执行位变化会被拒绝。
全部 265 条错误点记录及原始 `site_id` 保留，其中 215 条支持注入，50 条只观测。
