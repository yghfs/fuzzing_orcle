# 联合实验验证记录

以下记录 2026-11-25 至 26 日的观测层和公式门控故障注入验证结论。按用户要求，
本实验 `results/` 下的历史运行目录已清空；下列历史统计保留为文字记录，原始 trace、报告和镜像不再随仓库保存。
后续运行会重新生成独立的 campaign 目录。当前功能说明见 [README](README.md)。

## 静态与构建核验

- 输入为 [realistic_review.csv](../../realistic_review.csv)，SHA256 为
  `8e68ed6bdc631a10221ef430f68bd175b52b82dfbbbdfd00d7d48891a1c3bbcf`。
- 265 条 `realistic=1` 记录全部映射并插桩，保留原始 ID；263 个源码位置、31 个文件、
  28 个翻译单元。322 条 `realistic=0` 记录不生成探针 ID。
- [逐点映射](sites.csv)、[完整清单](sites.json) 和
  [补丁](patches/0001-reviewed-error-site-trace.patch) 的确定性再生成与补丁重放通过。
- 表达式类型、单次求值、短路、嵌套、共享头文件范围，以及并发 invocation 配对和
  缺失事件处理共 7 项测试通过。
- GCC 14 完整构建 Linux 6.12.1 `bzImage` 通过，开启 counter-trace、错误点追踪和 KASAN。
  构建记录有一条 objtool 对 `cpuset_write_resmask()` 的控制流告警；本轮 VM 未出现内核运行诊断。
- 原 counter-trace 开发树的源码审计通过；新实验在独立开发树、构建目录中实施。

## QEMU 实测

历史运行目录标识：`error-sites-KPhWJPt4`（目录已清理）。
机器使用 TCG、2 vCPU、2 GiB 内存和根盘 overlay。
普通数据盘为默认 256 字节 inode，inline_data 数据盘为 512 字节 inode；均关闭 orphan_file。

| 文件系统与采集阶段 | 不同 site_id | enter 数 | return 数 | 配对 |
| --- | ---: | ---: | ---: | --- |
| 普通 ext4，mount 进程 | 54 | 120 | 120 | 完整 |
| 普通 ext4，工作负载进程 | 59 | 1,145 | 1,145 | 完整 |
| inline_data，mount 进程 | 52 | 118 | 118 | 完整 |
| inline_data，工作负载进程 | 62 | 1,112 | 1,112 | 完整 |

这些计数按采集阶段和任务过滤条件统计，不能相加当成独立覆盖点总数，也不代表全部
265 条记录都在短测中执行过。debugfs 的累计计数还包括根盘、checker 等其他 ext4 调用。

验证的行为：

- inline 目录/小文件增长命中 `175` 及共享头文件内的 `176`，观察到实际嵌套顺序。
- 使用非法用户指针的 GETFSMAP 命中 `285`，`copy_from_user` 实际返回 192，用户态得到 EFAULT。
- 禁用探针后再运行负载，hits/returns 保持不变，未产生错误点事件。
- 每 CPU trace stats 的 overrun、commit overrun、dropped events 均为 0。
- 普通、inline_data 数据盘的负载前后稳定快照全部有效，namespace 和 relations 有效，mismatch 为 0。
- guest 无 BUG、KASAN、Oops、WARNING 或循环锁诊断；关机后两块数据盘 `e2fsck -fn` 均返回 0。

实际捕获的嵌套事件（省略任务与时间戳前缀）：

```text
site_id=175 invocation=4622 phase=enter result=0 kind=pending
site_id=176 invocation=4623 phase=enter result=0 kind=pending
site_id=176 invocation=4623 phase=return result=1 kind=pointer
site_id=175 invocation=4622 phase=return result=1 kind=pointer
```

上述原始证据、负载源码、guest 脚本、镜像、配置和构建绑定已随历史结果目录清理；
本验证记录保留该轮结论，迁移到其他机器时需要重新运行。

本轮内核镜像 SHA256：
`fdde4a4ae1ec3c02715516e75a7d932b20e3b4ee95bcc7037f0f1ec8cc9052da`。

## 观测层当时的边界

这次验证证明静态记录与实际执行事件可以关联，且检查链能够一起运行。
该历史运行没有替换函数返回值；故障注入及反馈由后面的 `0002` 层和程序级执行器增加。
宏包装范围、设备归属和调用栈采集的具体约定见 [追踪说明](ERROR-SITE-TRACING.md)。

## 程序级故障注入：2026-11-25 至 26 日

实现入口为 [run-campaign.py](scripts/run-campaign.py)。265 条记录都有显式策略，
229 条支持合成接口失败，36 条状态相关记录只观测（`buffer_uptodate` 由
`clear_buffer_uptodate()` 做对象状态注入后纳入可注入集合）。新增的 13 项测试通过，覆盖：
公式先后顺序、跨 syscall 保留与程序重置、其他设备/observer 排除、计划与实际计数核对、
动态新点、三点全部八种组合、组合调度、参数单次求值与 cleanup、syscall 前紧邻 arm，
以及实际生成的 usercopy 包装在整段失败时的返回值、目标清零和大小检查失败时不写的契约。
原有 7 项观测测试、补丁确定性重放及原 checker 开发树源码审计也通过。

以下均为历史真实 QEMU TCG 运行，数据盘在各自 campaign 内持续挂载。每项结果的状态为 complete，
所有轮次原生 syscall、会话/trace 核对、checker、稳定快照及最终 `e2fsck -fn` 通过。
每轮 trace 丢失统计为 0，无新增 BUG/KASAN/Oops/WARNING/循环锁诊断。

| 场景 | 程序 | 轮次（含基线） | 实际注入次数 | blocked | not_reached |
| --- | ---: | ---: | ---: | ---: | ---: |
| 普通 ext4，确定性门控/组合回归（`campaign-ff8vg64a`） | 2 | 9 | 9 | 1 | 1 |
| 普通 ext4，syzkaller 生成及变异（`campaign-sp3_lmf0`） | 2 | 10 | 2 | 6 | 2 |
| inline_data，syzkaller 生成及变异（`campaign-56rexfo2`） | 2 | 10 | 0 | 6 | 2 |
| inline_data，确定性门控/组合回归（`campaign-nezvh5ly`） | 2 | 9 | 9 | 1 | 1 |
| 保存的 syzkaller 输入重新探索（`campaign-ss2xku5z`） | 1 | 3 | 1 | 2 | 0 |
| 补齐 Kconfig 依赖后的普通回归（`campaign-uajaquk5`） | 2 | 9 | 9 | 1 | 1 |
| 补齐 Kconfig 依赖后的 inline 保存输入（`campaign-eu0n70yu`） | 1 | 3 | 1 | 2 | 0 |
| 修正 usercopy 失败契约后的最终普通回归（`campaign-0ag3crq7`） | 2 | 9 | 9 | 1 | 1 |

合计 62 轮、40 次实际注入。每个 campaign 内 boot ID 和 counter epoch 均保持唯一，
程序前的稳定状态出现不同取值，确认并未在变体之间恢复成同一文件系统状态。
此表不表示穷尽故障空间；inline_data 的 syzkaller 短测没有实际注入，不能把其计划数算为成功。
实际注入覆盖的不同 site_id 为 `5/46/69/147/154/514`，远少于静态支持的 215 条。

syzkaller 场景不是 C fixture：首个输入的生成方法是 ResourceSeedMutate，后续输入是
ResourceMutate，调用来源、真实类型变异及未回退的元数据见各 `program.json`。
普通 ext4 使用 seed 1/2、16 calls、每程序最多 4 次重放；inline_data 使用 seed 23/24、
20 calls、file-state 模板及同样的重放预算。保存输入场景重新导出普通场景的 program-0000，
重新采集当前启动的上下文，没有复用旧启动的栈指纹。

上述确定性回归的 `regression_passed=true`。正常程序先打开公式门控，后续无公式的
open/close 程序仍然 `formulas=0`、候选为 0、实际注入为 0；故意提供的真实早期点记为 blocked，
额外出现次数的计划记为 not_reached。多个变体实际同时命中两个故障。

inline_data 历史联合轨迹提供一个与目标一致的实测例子（call 从 0 开始；原始 CSV 已清理）：

```text
order=9   call=1  formula INODE.ALLOC.BITMAP / R-IDELTA
order=27  call=1  site=5   posix_acl_create  eligible=1 injected=1 -> -ENOMEM
order=57  call=3  site=147 new_inode         eligible=1 injected=1 -> NULL
```

同程序中第 2 个 syscall 打开门控，第 4 个 syscall 在自己的首个公式之前仍可注入；
下一程序重新清零。完整计划及内核实际状态见同轮 `plan.json`、`session.txt`，
缺陷判断见 `checker/report.json`，不能只看 errno。

## 最终构建与回归绑定

最后一轮修正了 11 条 `copy_from_user` 策略：整段失败返回请求长度，在
`check_copy_size` 通过后清零目标缓冲区，检查不通过时不写。生成补丁的源码变化仅在
`fs/ext4/ioctl.c`。C 语义测试使用实际策略生成的包装表达式，核对原复制调用被跳过、
有效缓冲区清零、大小检查失败不写、无注入时正常复制和零长度不注入。
这些 usercopy 注入分支通过 C 测试及内核编译验证；上述 QEMU campaign 的实际注入点
仍为前述 6 个 site_id，没有将编译或计划计入动态注入覆盖。

最终历史普通回归（`campaign-0ag3crq7`）为
`status=complete`、`regression_passed=true`，9 轮 checker、稳定快照和原生边界核对
全部通过；18 份每 CPU trace stats 的丢失计数全部为 0，无新增内核诊断，离线检查 `e2fsck_exit=0`。
其历史联合轨迹也包含上例的公式 → site 5 → 跨 syscall 的 site 147 双故障顺序；原始报告与 CSV 已清理。

该历史运行的构建绑定如下；恢复官方源码基线后的构建与运行记录见本文末尾，
不能用这组历史哈希代表当前内核：

| 产物 | SHA256 |
| --- | --- |
| bzImage | `be353ebd4d6695dd6e3d1bde42f0c125040880d30c2b2030490d375cb0e6f844` |
| injection.json | `825606b96ed347655cce9d9b6bf479bdecad3d2f8e5e08771c99678dd25ffdd6` |
| 0002 注入补丁 | `05e3c4d9a7098ab94e6fe2d0c0a32484a1f41044455f5d5901774ed9188248fb` |
| .config | `12b023e1f31c207c854e912785cadf70a5dc87121339379ce6329983a1260270` |

构建日志位于 `build/efi-usercopy-build.log`，最终运行日志位于
`build/efi-usercopy-final-regression.log`。原始 CSV 的 SHA256 未变化。

## 联合内核的旧观测接口兼容性

历史运行 `error-sites-L0rbCBNS` 使用带故障层的内核，
再次运行原有观测 smoke 并通过。普通 ext4 工作负载覆盖 59 个 site、1,145 对事件，
inline_data 覆盖 62 个 site、1,112 对事件；嵌套 175/176、自然 usercopy 失败、
关闭探针计数不变、稳定快照及两块数据盘的离线 fsck 均通过。

历史运行的原始事件、输入导出、checker 报告和镜像已清理；构建/配置/策略绑定的校验值仍记录于上文。
修复种子文件被改名后的重放失败后，使用旧 `program-0010` 导出做了额外 QEMU 回归：
在 guest 中先将备用文件改名，使原路径缺失，再连续执行 3 次；每轮退出码均为 0、
EFI session 和 checker 均完整通过，前后 `dev:ino` 一致，最终 `e2fsck -fn` 返回 0。
修复实现由 holder 在程序的全部变体期间持有文件 inode，并通过 `/proc/<pid>/fd/9` 引用，
不依赖可被 syscall 改名或删除的原路径。收尾时 `progress.json` 也更新为最终状态。
上述新回归的临时输出已一并清理。当前单 worker、栈指纹和未启用路径的边界见
[故障注入说明](FAULT-INJECTION.md)。

## 长时间 campaign 的 guest 临时空间回收

2026-11-26 的六小时运行在 2,721 秒时停止。前 173 轮均完成；第 174 轮 workload 本身退出码为 0，
但 guest 在保存 trace/CPU 统计时报告 `No space left on device`，造成本轮证据不完整，随后分析阶段因缺少文件
抛出 `FileNotFoundError`。根因是每轮结果复制到 host 后，guest `/tmp/efi/r*` 目录没有删除，
trace 持续占满了 guest 根盘；不是 ext4 checker 报告的文件系统缺陷。

执行器现在每轮复制完 host 结果后删除对应 guest 临时目录；若清理失败会中止并报告，避免继续累积。
失败路径会尽力清理，campaign 另保存异常 traceback；guest round 返回非零时，在分析前以明确错误终止。
以该运行保存的 program-0012 重新执行 3 轮进行 QEMU 回归：3 轮均完成分析、checker 通过，
共 1 次实际注入；report 和最终 progress 状态均为 `complete`。单 worker、栈指纹和未启用路径的边界见
[故障注入说明](FAULT-INJECTION.md)。

## 恢复官方 Linux 6.12.1 基线：2026-11-26

本轮审阅三层补丁叠加后的最终源码，撤销基础 checker 层夹带的 extent-grow 回滚修复、
unlink 提前预留 inode 写权限、inline-data 长度修复、orphan-file 新增拒绝条件、xattr
新增拒绝条件及错误分支变化、`AGGRESSIVE_CHECK` 启用等改动。namespace 的重复字段写入
和成功路径多余调用也已移除；保留公式、checker、错误点观测及故障注入所需差异。
逐项说明和官方来源见 [源码基线](SOURCE-BASELINE.md)。

已完成的验证：

- 观测层生成器确定性检查和原始 CSV 身份校验通过，仍为 265 条记录、263 个源码位置。
- 故障层确定性生成、三层补丁重放及最终源码哈希校验通过。
- **21 项故障注入/源码测试、7 项观测测试通过**。源码测试检查恢复后的错误路径、
  unlink 操作顺序、namespace 写入次数，并确认篡改补丁清单外文件也会被拒绝。
- 完整开发树检查通过：86,630 个普通文件、62 个符号链接、5,757 个目录。
  86,581 个普通文件与官方内容相同，37 个上游文件有允许的插桩差异，12 个为新增插桩文件；
  无额外、缺失或内容不匹配的项。
- 使用 GCC 14、4 个编译任务构建 `bzImage` 成功。构建日志为
  [upstream-source-build-final.log](build/upstream-source-build-final.log)。构建中仍有
  `cpuset_write_resmask()` 的 objtool 提示；本实验没有修改该上游函数。
- [source-audit.json](build/linux-error-site-trace/source-audit.json) 与内核、配置及补丁
  一起绑定；启动 campaign 会重新核对整个开发树，并将该报告复制到结果目录。

本轮最终构建的 SHA256：

| 产物 | SHA256 |
| --- | --- |
| bzImage | `a1c5c754e3415a583ecec9e0260d3bc245d5545b8c5c27a7a61c7353606972bf` |
| injection.json | `4d565e8bea9aad2013cbef455b7ac124bafbda4e52ba040b6cc14fbbaef6e897` |
| 0002 注入补丁 | `a87d72abe08c30e979fa9e0952ee91225c624d04747d9cce6229d11b5f0fb62e` |
| source-audit.json | `cbec6c353d20a927dea15d3387af978faff74636a56081c0cfb63eb7e096f8a8` |

使用上述内核完成 QEMU 确定性回归，命令在本实验目录执行：

```sh
python3 -u scripts/run-campaign.py \
  --regression --replays-per-program 6 \
  --duration-seconds 900 --port 11892
```

本次结果见 [campaign-sqb1tzw4/report.json](results/campaign-sqb1tzw4/report.json)，
终端日志见 [upstream-source-regression.log](build/upstream-source-regression.log)。
运行耗时 151.74 秒，`status=complete`、`regression_passed=true`，2 个程序共执行 9 轮；
6 个注入轮次实际注入 9 次，覆盖 6 个不同错误点：

| site_id | 实际注入次数 |
| --- | ---: |
| 5 | 4 |
| 46 | 1 |
| 69 | 1 |
| 147 | 1 |
| 154 | 1 |
| 514 | 1 |

第一个程序的 7 轮实际注入次数依次为 `0,1,2,1,2,1,2`，验证单故障和双故障组合。
随后无公式的程序执行 2 轮，均为 `formula_events=0`、候选为空、实际注入为 0；
故意安排的计划分别计为 `blocked=1` 和 `not_reached=1`，验证门控不会沿用上一程序的状态。
9 轮原生执行及事件核对完整，checker 和稳定状态检查全部通过，无稳定状态不匹配、
无 checker finding、无新增内核诊断。18 份每 CPU trace 统计的 `overrun`、
`commit overrun`、`dropped events` 全为 0；卸载后的
[offline.json](results/campaign-sqb1tzw4/offline.json) 记录 `e2fsck_exit=0`。

这是确定性 fixture 的短程回归，不是 syzkaller 随机生成的长时间 campaign。
本次没有动态触发 extent-grow 的 site 67，不能据此声称复现了该缺陷或覆盖全部静态错误点。
