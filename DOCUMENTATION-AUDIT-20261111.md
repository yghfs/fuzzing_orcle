# 代码修复与文档审计（2026-11-11）

审计对象是 `/home/ikun/fuzzing-orcle` 的当前工作树，包含用户既有的 staged 修改。
本轮修复离线 BIGALLOC 特性识别，核对 32 份现存 tracked Markdown 的本地链接及主要状态描述，
并同步机器可读状态。没有重新运行长 campaign、构建内核，也没有重新核实远端上游最新状态。
已有实验摘要与本轮新验证在下文分别说明。

## 已修复代码与新验证

[`check-disk.py`](experiments/ext4-counter-trace/scripts/check-disk.py) 原来从 superblock
偏移 96 的 `s_feature_incompat` 读取 `0x0200`，但该位代表 FLEX_BG；
BIGALLOC 应读取偏移 100 的 `s_feature_ro_compat` 中的 `0x0200`。
原实现会将启用 flex_bg 的普通 ext4 当作 bigalloc，并漏认关闭 flex_bg 的 bigalloc。
这会污染布局分支及 bitmap/cluster 计数解释，历史检查结果不能自动视作使用修复后解析器重验。
修复仅改变 host 离线解析器，不修改 kernel 插桩或已绑定构建产物。

新增回归在 [`test-lazy-disk.py`](experiments/ext4-counter-trace/tests/test-lazy-disk.py)：
用真实 `mke2fs` 镜像覆盖 regular/bigalloc 与 flex_bg 开关的四种组合，检查特性识别、
cluster ratio、group 有效 cluster 数及完整离线判定。

| 本轮验证 | 结果 |
| --- | --- |
| `python3 -B experiments/ext4-counter-trace/tests/test-lazy-disk.py -v` | 5 项通过，含四种新组合和原有 lazy-layout 集成测试 |
| `python3 -B experiments/ext4-counter-trace/tests/test-disk-layouts.py -v` | 7 项通过 |
| 新增四项测试运行于临时恢复的旧解析逻辑 | 恰有 2 项失败：regular+flex_bg、bigalloc 无 flex_bg，证明能检出本次回归 |
| `test-disk.py <fresh-image>`，四种组合各一次 | 全部通过；篡改计数、链接、inode 生命周期及 itable highwater 后均拒绝有效结论 |
| `scripts/source_audit.py --require-build`（在 counter-trace 目录） | `passed=true, issues=[]`；这是源码/产物绑定检查，不是重新编译 |

## 当前实现与文档修正

- 自定义单 VM 反馈链已连接 Generate/Mutate、运行、KCOV、生命周期分析、有效操作覆盖准入及父程序调度。
  多 VM 自定义 corpus 合并、通用跨线程因果和跨重启自动恢复仍未实现。
- 默认随机 admission hooks 是 `mballoc_mark`、`unlink_inode_dirty`、`inode_evict_final_dirty`；
  默认概率为 1000 ppm，默认 errno 策略为 `native`。旧文档中的“两类 hook / 默认 EIO”已更正。
- 离线 `BLOCK_UNINIT` 已按受支持的 metadata/flex/短末组布局重算；`META_BG` descriptor placement
  明确拒绝，不能将其报告为已支持。准确的同线程原生 syscall 返回关联已实现。
- 并发多 worker 使用共享对象及同步启动，single 使用私有工作目录；修正旧的全部 worker 私有路径描述。
- `R-IEVICT` 的正常释放保证以最终 inode 更新成功等前提为限。final-dirty 失败后保留 bitmap
  是 ext4 保守恢复行为，不能仅凭在线 mismatch 确认独立缺陷。观测输出和 checker 代码没有因此被改写。
- 已将错误指向 upstream v6.12.1 的项目自有 `checker.c`、`fault-inject.c` 链接改为本地源码，
  并把“当前 HEAD”改为历史核对版本；本次未联网验证这些漏洞在上游的最新状态。

入口及实现描述已同步至根 README、`COUNTER-TRACE-IMPLEMENTATION.md`、`公式.md`、`汇报.md`、
`BUG-INVENTORY.md`，以及 counter-trace 的 README、IMPLEMENTATION-STATUS、FIRST/SECOND-CHAIN、
KCOV-FEEDBACK、PROBE-API、公式映射、source audit 和 `status.json`。
MVP 原始开发文档已明确标为历史设计，保留当时方案与数字。

## 证据目录与可支持的结论

| 材料 | 本机状态与适用边界 |
| --- | --- |
| `baselines/20261009-130905+0800/main-project-snapshot.tar.gz` | 存在；是基线归档，不是当前工作树的全量快照 |
| counter-trace `results/target-comparison-long-20261109` | 一小时三臂结果存在；测量完成，但实验臂可比性有限 |
| counter-trace `results/campaign-9chz7m3j`、`campaign-fyubc_lv` | 十二小时 PC-only/full-feedback 存在；KCOV 完整，整轮验证失败 |
| `work/extent-grow-errors-v2-20261110` 及自然压力 extent 结果 | 修复前后材料存在；本次未重跑，结论限定于已有 Linux 6.12.1 运行 |
| counter-trace `campaign-lhze43mu`、`campaign-k3gxmeex`、其 postreport、`campaign-fmxs5w3e`、`campaign-0_6qrnow` | 原始目录不存在，剩余文字为历史摘要 |
| 旧 natural-rename 三组结果及旧 research 中 lhze/yk/dzq 原始目录 | 不存在；失效链接改为明确标注的历史路径 |

覆盖数字、来源和比较限制集中在
[覆盖证据审计](experiments/ext4-counter-trace/research/2026-11-11-target-coverage/README.md)。
一小时 admitted-corpus 目标 PC 为 PC-only 1854、full-feedback 2068；全部完整样本并集则为
2637、2630。十二小时 effect/corpus 为 3636、3256，raw 为 3819、3668。
这些是不同样本口径和不同实验，不支持“联合反馈稳定提升覆盖”的断言，也不构成其无效的因果证明。

十二小时 PC-only 的 7 段、full-feedback 的 3 段生命周期分析失败均紧随 fault/reset，
记录涉及旧 carry epoch 与新 mount epoch 的连续性，以及 device identity/sequence 校验。
这是待验证的分析链问题；不能将 KCOV 完整等同于生命周期完整或宣称 ext4 缺陷。
`status.json` 保留历史字段并明确其范围，新增两臂当前失败状态，避免覆写原始实验报告。

## 尚未解决与审计边界

1. reset/carry epoch 连续性需要单独定位、修复并做定向回归；本轮不将观察到的失败冒充已修复。
2. 反馈收益需要在可比工作负载、相同覆盖口径和多个 seed 下重跑；现有单 seed 两臂不能提供因果判断。
3. 旧 extent v2 已有三类错误出口的 Linux 6.12.1 对照，主线应用/编译检查不能替代主线运行验证。
4. 本地链接检查只确认目标存在；未全量验证外部 URL、所有行号/Markdown 锚点或重新推导每条内核 finding。
   旧原始日志被清理后，无法仅凭文字摘要重新证明对应历史数字。

最终结构检查：32 份原有 Markdown 加 2 份新报告，共 283 个本地链接目标均存在；
`status.json` 可解析，`git diff --check` 通过。文档上传前后 staged diff 的 SHA256
保持一致（`ce059365ceeadfd0c62156c7a609970759b9c056f8098454e574cfd4771e082e`）。
本轮修改保留既有暂存内容，不进行 commit 或实验结果清理。
