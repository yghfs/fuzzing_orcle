# Fuzzing 反馈、覆盖度与调度策略调研

调研日期：2026-10-21，Asia/Shanghai。

范围：核对本项目实际代码与保留的运行记录，对照官方源码、官方文档和论文作者的说明，给出可以验证的改进顺序。本次新增研究文档、历史记录审计脚本和结果，不修改运行时调度，也没有启动新的 VM 或长跑。下文区分现有证据、外部方法和待验证方案。

具体实施顺序、每轮闭环及各批验收见 [执行流程](WORKFLOW.md)。

实验分组、对照臂、共同评价口径和工具启发映射见 [实验矩阵](EXPERIMENT-MATRIX.md)。

资源前缀的额外源码核查及前缀/尾部贡献抽查见 [资源前缀复核](PREFIX-REASSESSMENT.md)：
现有前缀包含固定目标操作，整体转换密度提升尚不能证明随机尾部的探索能力改善。

## 1. 判断

“覆盖引导 + 目标状态反馈”的方向有成熟先例，可以继续用；但当前实现不能声称已经验证了联合反馈和加权调度的有效性。

最紧迫的问题是一个已经由历史记录确认的调度缺陷：`campaign-0mwk51uk` 中有 225 个 corpus 文件，1,847 次变异却全部使用同一个父程序。应先修复选择机制、反馈归因和覆盖指标，再比较稀有特征或自适应算法。增加 VM 数量不会自动解决这些问题。

推荐组合是：syzkaller 的稳定信号筛选和语料维护，AFLFast/libFuzzer 的稀有特征与能量分配，StateFuzz 的状态探索思路。SyzVegas 的任务/种子自适应和 Janus 的镜像/操作联合探索适合作为后续独立实验。[S1–S6]

## 2. 先区分闭环中的五个问题

| 层次 | 必须回答的问题 | 本项目对应内容 |
| --- | --- | --- |
| 反馈表示 | 什么算新的行为？ | PC、边、公式点、状态、恢复路径 |
| 样本准入 | 什么证据足够可信，值得保留？ | KCOV 完整性、native 核验、重复执行、来源归因 |
| 种子选择 | 下一次变异谁？ | corpus parent 选择 |
| 能量/任务调度 | 给它多少次变异、选择哪类任务？ | Generate、资源前缀、Mutate、场景、重放预算 |
| 漏洞判定 | 什么才是错误？ | 操作出口、稳定态、持久化/恢复关系及复现 |

可信的新覆盖不是已确认漏洞；成功 syscall 多也不等于行为覆盖多。反过来，失败调用的回滚路径也可能有价值，不应把 errno 非零的输入一律丢弃。

## 3. 其他工具怎么做，哪些能用

| 工具/工作 | 实际反馈与选择方法 | 对本项目的用途 | 接入边界 |
| --- | --- | --- | --- |
| syzkaller | 区分原始 PC cover 与用于探索的 signal；本地 executor 支持由相邻 PC 构造边信号。新输入先 triage/deflake，再最小化和入 corpus；信号带执行质量优先级；父程序按权重随机抽取，支持 focus areas。[S1] | 首先借用稳定信号、程序去重、最小化和目标区域管理；保留错误返回路径 | 当前只调用 typed Generate/Mutate，并没有运行这套完整 fuzzer/corpus 管线；不是改一个生成函数就能得到 manager 闭环 |
| AFL++ / AFLFast | AFL++ 使用修改过的边覆盖，引导入队、裁剪及语料维护；AFLFast 的核心是按路径出现频率与选择次数分配能量，`rare` 则强调罕见边。[S2] | 维护“多少次执行命中特征”，给稀有的 ext4 边/语义特征更多探索机会 | `rare`、`fast` 不是可脱离特征定义直接套用的万能权重；不把累计 raw trace hits 当作路径频率 |
| libFuzzer / Entropic | 维护全局稀有特征、每个输入附近的特征频率、已执行变异数和能量；可配置按执行时间缩放；能量随观测更新。[S3] | 为种子记录后续变异收益和成本；避免仅按入库时发现数永久评分 | 借鉴统计与更新方式，不能直接把字节流变异引擎用于 syscall 资源图 |
| StateFuzz，USENIX Security 2022 | 用状态变量值和状态转移补充代码覆盖，优先探索新状态；其目标是 Linux/Android 驱动。[S4] | 我们已有计数、差值、对象生命周期和恢复阶段，可以构造 ext4 语义特征 | “我们的公式状态”是借鉴后的设计，不是原论文已有的 ext4 实现；原始 inode/block 数字不能直接成为新状态 |
| SyzVegas，USENIX Security 2021 | 用多臂老虎机动态调整任务选择和种子选择，目标是提高单位时间的覆盖收益。[S5] | 后续比较 Generate、Mutate、资源前缀、压力/长期资源场景的预算分配 | 必须先有可归因的奖励、成本和探索记录；直接移植 bandit 会学习当前污染的反馈 |
| Janus，IEEE S&P 2019 | 联合探索文件系统镜像与 syscall 两维输入；作者实现是 AFL 变体，基于 LKL。[S6] | 把镜像特性、初始状态和程序共同视作测试输入，建立可重放的场景种子 | 旧 LKL/工具链不能直接替代当前 Linux 6.12.1 VM；镜像变异实验与自然合法输入实验应分别标注 |

这里的“状态转移”有两种含义：AFL 文档中的插桩控制流转换，和 StateFuzz 的程序语义状态转换，不能混为同一个覆盖指标。

外部覆盖提升只能作为方法动机：StateFuzz 摘要报告其当时驱动实验相对 syzkaller 代码覆盖提高 19%、状态覆盖提高 32%；SyzVegas 摘要报告其配置下覆盖最高增加 38.7%。这些是作者实验结果，目标、版本、预算和分母不同，不能预测本项目也能提高相同比例。[S4][S5] 本次未自行重跑这些工具。

### 3.1 syzkaller 已有的可直接参考实现

本仓库实际依赖 `tools/src/syzkaller-master`，其实现可以直接检查：

- `executor/executor.cc:1231`：`write_signal()`；`use_cover_edges` 开启时，从相邻 PC 构造边信号。
- `pkg/fuzzer/fuzzer.go:272`：`signalPrio()`；成功返回、没有 Any 参数会影响同一信号的优先级，不是简单“成功数越多越好”。
- `pkg/fuzzer/job.go:111`：普通新输入的 deflake 采用 3/5 目标；已有 corpus 和 snapshot 路径有不同参数，应按本地版本区分。
- `pkg/fuzzer/job.go:171`：无稳定新信号则退出，否则最小化并保存稳定信号。
- `pkg/corpus/corpus.go:187`、`pkg/corpus/prio.go:19`：以信号规模建立权重，使用随机数从累计权重中选种子。
- `pkg/mgrconfig/config.go:255`：`experimental.focus_areas` 支持过滤区域与权重；配置未覆盖区域是否忽略需要明确处理。

syzkaller 本地版本也有固定参数和启发式。可借鉴的是它的完整证据链与抽样机制，不能把“用了 syzkaller”当作每个参数都最优的证明。[S1]

## 4. 我们现在的问题，有哪些已被证据确认

### 4.1 P0：父种子选择发生实际饥饿

位置：`scripts/kcov_feedback.py:151–163`；调用位置 `scripts/campaign_runner.py:585–599`。

当前实现用：

```text
ticket = segment_ordinal % sum(parent_scores)
按 corpus 入库顺序遍历累计权重，选中 ticket 所在区间
```

这不是每次重新随机抽样，也不是保证短期公平的加权轮转。第一条记录的新 PC 为 7,110，目标前缀 PC 为 2,410，按当前代码得分：

```text
7,110 + 4 × 2,410 = 16,750
```

这轮总共 4,619 段，传入 parent 的 ordinal 最大为 4,618，始终没有走出第一个种子的权重区间；资源前缀和 fresh generate 还会覆盖其中部分父种子选择。

直接遍历历史 `program.json` 得到：

| 指标 | campaign-0mwk51uk |
| --- | ---: |
| 保存的程序记录/完成核验的段 | 4,619 / 4,619 |
| ResourceSeedAndGeneratedTail | 2,310 |
| 普通 Generate | 462 |
| Mutate | 1,847 |
| Mutate 使用过的不同父程序路径 | **1** |
| 最常用父程序占全部 Mutate | **100%** |
| corpus 文件 / 不同程序 SHA256 | 225 / 215 |

因此“有 225 个 corpus，所以已经在探索 225 个父程序”不成立。这不证明其他父程序一定更好，但已经证明它们没有在这轮得到变异机会。审计所用当前 feedback 源码和该轮冻结源码 SHA256 完全一致：

```text
d18d0273c2d428dce94adf6fc46cab110c09cc06008cfebc0e6de62ad708a2d9
```

证据：`campaign-0mwk51uk-audit.json`；第一段 `coverage.json`、`feedback.json`；全部 `syz-programs/*/program.json`。

### 4.2 P1：公式奖励混入基础设施事件，归因过粗

位置：`scripts/trace_lifecycle.py:906–927` 统计全部 timeline；`scripts/kcov_feedback.py:272–315` 使用段级统计给该段所有合法程序联合奖励。

第一段公式点/公式组合为 103 个，其中 13 个 key 是 `SNAPSHOT.MEMORY.STABLE`。第一段 2,217 个事件按来源为：

| 来源 | 事件 |
| --- | ---: |
| generated_syscall | 234 |
| seed_setup | 414 |
| stable_snapshot | 342 |
| cleanup | 622 |
| background_unattributed | 605 |

这些事件对于 checker 验证都有意义，但不能全部归功于生成程序，也不能据此判定这个父种子有高变异潜力。尤其多 batch/worker 时，一个程序的命中会变成该段所有程序的奖励。

本轮整体报告明确写着 `Source attribution complete: False`。native syscall 配对完整，只说明调用被核验，不意味着所有公式事件都可归因于它。`augment()` 的入口参数只检查 native 核验，没有把完整 trace 质量、来源可信度和重复执行稳定性作为同等门槛。

应把“checker 观察覆盖”与“程序可归因覆盖”分别输出。快照可以继续作为判定证据；由稳定快照发现的异常若要奖励某输入，应通过受控重放确认因果归属，不能把所有快照点统一视作输入新颖性。

### 4.3 P1：未核验签名有正奖励，尚无重放证据

位置：`scripts/kcov_feedback.py:281–315`。

当前 `unsupported_or_unverified`、`concurrent_or_ambiguous` 和部分 unverified reason 会进入正奖励；本轮累计为 13 个 unverified 签名、0 个 mismatch 签名。函数名/字段中的 `repro` 并不意味着已经重新执行复现。

现有 `_signature()` 同时存在两类风险：使用 inode、block、slot 等实例字段时会把同类现象拆散；episode 实际常用 `onset_point` 等字段，而签名读取的是 `point` 等字段，可能丢失需要区分的位置。应按实际 episode schema 规范化签名，剥离实例 ID，并把“探索候选”和“确认错误”分别管理。

### 4.4 P1：权重、稀有度和能量没有得到独立验证

当前入库权重：`new_PC + 4 * new_ext4_PC`。其中 `new_PC` 已包含 ext4 PC，因此每个目标 PC 的总贡献实际为 5，不是与普通 PC 分离的“4 倍”。ext4 集合本来就是普通 PC 集合的子集，单独的 ext4-new 准入也没有扩充可接纳的 PC 行为类型。

联合公式分数是 `4 * new_points + 2 * rare_points + 8 * new_mismatch + 3 * new_unverified`。未找到独立消融证明这些系数优于均匀抽样或其他系数；`old < 3` 数的是累计原始事件命中，不是命中该特征的执行次数，不能直接等同 AFLFast/Entropic 的稀有度。

分数主要在入库时设定，没有记录父种子后续被变异多少次、其子代增加多少稳定特征、消耗多少总时间。现有机制更接近“按首次发现量加权的启发式”，尚不是基于观测收益更新的自适应调度。

### 4.5 已有 A/B 证明的范围需要收窄

已清理的 A/B 短测汇总显示，两臂各核验 640 个 syscall：

| 输入策略 | 成功调用 | 公式转换调用 | 转换密度 |
| --- | ---: | ---: | ---: |
| ordinary | 194 | 23 | 3.59% |
| resource-valid | 427 | 183 | 28.59% |

这是资源前缀能提高该短测中公式转换密度的证据。没有把父种子算法、联合反馈和系数分别作为实验变量，也没有多随机种子长期覆盖比较，因此不能据此得出“联合反馈调度已经提高覆盖率/发现漏洞能力”。

`scripts/ab_report.py` 当前主要汇总调用、成功、转换密度和完整性，没有提供稳定边覆盖、语义状态覆盖和父种子选择分布的对照。

## 5. 覆盖率到底应该怎么看

### 5.1 现有数字是覆盖规模，不是百分比覆盖率

该 12 小时记录最终有 12,378 个唯一内核 PC，4,213 个“ext4 PC”，113 个公式点/公式组合；没有对应的可插桩总数，不能写成“覆盖率 X%”。

现有 `parse_symbol_ranges()` 用 `ext4_`、`__ext4_`、`jbd2_` 符号前缀归类，既含 jbd2，也不是 `fs/ext4/` 所有静态函数的完整集合。需要单独展示 ext4、jbd2 和 checker 自身的范围；正式代码覆盖建议按固定构建的 DWARF/源码路径映射，不用函数名前缀代替源码范围。

| 段编号，起始为 0 | 累计内核 PC | 目标前缀 PC | 公式点/公式组合 |
| ---: | ---: | ---: | ---: |
| 0 | 7,110 | 2,410 | 103 |
| 100 | 9,787 | 3,268 | 109 |
| 500 | 11,401 | 3,857 | 113 |
| 1,000 | 11,607 | 3,962 | 113 |
| 2,000 | 12,026 | 4,118 | 113 |
| 4,000 | 12,309 | 4,210 | 113 |
| 4,618 | 12,378 | 4,213 | 113 |

最后一个新公式点在第 425 段。之后仍有新 PC，最后一次 PC 增益在第 4,615 段，所以不能声称“代码覆盖完全停滞”，也不能由 PC 增长推导公式状态覆盖继续扩大。

### 5.2 建议并列报告的指标

| 指标 | 定义与用途 |
| --- | --- |
| 稳定代码覆盖 | 同构建下可重放的唯一 PC/边；PC 与边分别统计，不互相充当分母 |
| 源码覆盖率 | 固定构建、固定源码范围中已覆盖插桩点或行 / 对应可插桩点或行总数；标明未编译和不可观测代码 |
| 目标公式点覆盖 | 按 feature/mount/scenario 建立点清单，已命中点 / 当前适用点，并列出未命中及不适用原因 |
| 公式状态/转移覆盖 | 唯一规范化状态元组和转移数量；未知总空间时只报数量，不编造百分比 |
| 调用转换密度 | 触发独立转换的调用 / 已核验调用；保留为输入质量诊断 |
| 调度公平性 | 被选择的不同种子数、从未选择数、最大连续等待、父种子选择熵/分布 |
| 单位成本收益 | 稳定新目标特征 / 全流程 wall time；同时报告每千次已核验调用的收益 |
| 错误发现 | 经重放确认的独立异常、首次发现时间、恢复/出口/稳定态分类；与临时差值、模糊事件分开 |
| 反馈质量 | KCOV 完整率、trace loss、有效归因比例、信号重放稳定率 |

KCOV 以 task 为单位，并不自动记录所有后台线程、软中断和异步 work。remote coverage 需要相应内核标注和 handle 关联；不能只启用一个配置就声称覆盖所有 jbd2/回写路径。[S7] 对异步状态可以先保留独立观察通道，未知来源不能直接给当前程序加分。

## 6. 能落地的方案

以下是候选设计，尚未宣称能提高本项目覆盖率。先补最小可比较基线，再逐项加入。

### 6.1 第一阶段：修复选择与收益归因

1. 替换 ordinal 取模累计权重；先用可复现 PRNG 的均匀抽样或真正按轮次公平的轮转作为基线，再比较有界加权随机。PRNG 的 seed、抽样序号和选择结果全部保存。
2. corpus 以程序内容哈希去重，同一程序的 PC/公式贡献合并；由于文件系统有状态，重放上下文保留不同版本，不能在去重时丢掉必要状态。
3. 新公式特征按 `program + call_index + same-task evidence` 归因；setup、快照、cleanup 与不明后台事件单独统计。跨线程异常通过专门重放确认。
4. `unverified` 保留为诊断，退出正常探索正奖励；mismatch 先进入候选队列，重放确认后再作为错误证据。普通 transient mismatch 不作为漏洞奖励。
5. 输出每个种子的选择次数、变异次数、子代新增特征、总成本和最后收益时间。把启动即有的大量通用覆盖作为基线统计，避免首次种子因此长期垄断。

验收：对已有的“首种子权重 16,750、总段数 4,619”条件构造回归；均匀轮转基线必须遍历全部可选种子。随机/加权策略则验证概率、重放确定性及长尾等待，不能仅以“偶尔选到第二个种子”为通过。

### 6.2 第二阶段：让反馈表达我们真正关心的状态

普通代码覆盖保留，用于发现新控制流；新增一个与之独立的语义特征集合：

```text
semantic_feature = (
  formula_id,
  point_id,
  image_feature_profile,
  object_kind,
  lifecycle_phase,
  gap_bucket,
  outcome_class,
  recovery_class
)
```

示例：同一个 inode 释放点经过“正常释放”“失败后回滚”“退出仍有差值”“稳定态持续差值”属于不同语义行为，而重复一万次正常释放不能当作一万个新状态。

`gap_bucket` 可从 `0`、`+1`、`-1`、`>1`、`<-1` 起步，再依据实际边界关系细分；这只是待验证的有限状态划分。对象状态需要区分有界阈值、引用共享/独占、资源存在/不存在等真实语义，不对原始 inode、地址、时间戳和不断增长的计数直接做唯一性奖励。

相邻状态对只在同一个对象生命周期、完整且可信的事件序列内构造；按 call/thread 边界处理控制流边，不能把两个 syscall 的拼接边或丢事件后的状态跳跃当作真实新转移。

program 级 KCOV 文件当前拼接多个 syscall 的 PC 序列，没有保存 syscall 分界。增加边信号应在采集端每次 `ct_kcov_stop()` 时分别计算，或给样本增加版本化的 call-offset 元数据，不能直接在旧文件的整个数组上连边。

### 6.3 第三阶段：在干净信号上比较稀有特征与能量

第一版稀有度建议按“执行是否命中特征”更新，每次执行对同一特征最多记一次。保留 `feature -> seed` 映射，考察较少执行能到达的状态，而不是奖励循环内高频重复 hits。[S2][S3]

记录原始收益向量：`new_target_edges`、`new_semantic_features`、`confirmed_candidates`、`wall_time`，先分别比较，避免再次把未经校准的异质指标揉成固定分数。

若实验支持，再比较有上限的稀有度加权或 Entropic 式能量更新；参数在调参集选择，在未参与调参的随机种子上复核。慢路径的纯时间归一化可能压低恢复、压力和 remount 场景，因此这类场景保留独立预算，并报告成本。

### 6.4 第四阶段：场景和任务选择

有了上述计量，再参考 SyzVegas，把 fresh、变异、资源前缀和不同场景视作可评估任务；决定是否用 MAB，应由与固定比例基线的实验决定。[S5]

参考 Janus，把“初始镜像/挂载特性 + 状态构建前缀 + 程序”保存成重放单元。[S6] 当前 corpus 只有 `.syz` 并不足以描述跨 segment 状态；至少记录：

```text
kernel/config/build hash
image hash + feature/mount profile
state-building prefix or checkpoint
program hash + parent chain
runtime fd-repair policy/version
worker/thread mode + scenario
```

当前 runtime 会修复部分 fd，直接把同一 `.syz` 交给原生 syzkaller executor 不保证执行相同行为。接入 manager 时必须先完成等价重放；自定义语义信号也需要明确的传输、命名空间与版本，不能无隔离地伪装成 PC signal。

## 7. 怎样证明策略有效

保持内核、编译器、checker、镜像起点、资源前缀和 syscall allowlist 一致，把同一套测量放在所有实验臂中。不同场景分层比较；必要时在实验块间重置到相同 checkpoint，避免历史状态和执行顺序混淆奖励。

| 实验臂 | 相对上一臂只改变什么 | 要回答的问题 |
| --- | --- | --- |
| A0 | 当前策略，保留原始行为 | 复核历史基线，观察首种子垄断 |
| A1 | 同一反馈、去重和记录；父种子改均匀抽样 | 单独修复选择后是否改善覆盖与多样性 |
| A2 | 在 A1 上加入可归因且重放稳定的语义状态准入 | 相同代码路径下的状态探索是否增加 |
| A3 | 在 A2 上加入稀有特征/动态能量 | 是否优于均匀选择，而非只优于已知有缺陷的 A0 |
| A4 | 在 A3 上比较任务自适应 | 是否优于固定任务比例 |

上表中去重/测量先统一应用于对照双方；若要单独量化去重收益，另加实验，不把多个改动混成一次 scheduler 胜出。

实施建议：先对短预算 pilot 检查信号和方差，再按 pilot 决定重复次数及长跑预算。可以从至少 5 个独立随机种子的 pilot 起步，但这不是论文统一标准，也不能保证统计功效。正式结论报告中位数、离散程度/置信区间及覆盖随时间的曲线/AUC，不能只挑最好一次。

同时做两种预算：等全流程 wall time 评价实际效率；等已核验 syscall/执行预算诊断输入质量。triage、重放、生成、编译、上传、取回、分析和快照都计入实际 wall time，不能只算 guest 执行时间。

关键验收：

- A1 不再出现历史记录中的单父程序垄断。
- A2/A3 在未参与调参的重复实验中，稳定目标覆盖/语义覆盖相对 A1 有一致收益，且反馈完整性没有恶化。
- 无确认错误时明确报告 0，不能把普通中间差值、模糊签名数或 corpus 文件数当作漏洞发现数。
- PC 覆盖、语义覆盖、调用转换密度和漏洞发现分别报告；没有统一分母时不跨工具比较百分比。

## 8. 审计复现与交付

审计只读取保留摘要；原始 `campaign-0mwk51uk` 目录已清理，不能再从该目录重跑审计。
当前保留的 `campaign-0mwk51uk-audit.json` 只作为历史调度结论摘要，不作为活动运行证据。

输出 `programs` 统计保存的 `program.json`，可能包含未执行的导出程序；本次长跑的 4,619 段均有 native 核验证据。另保存了 `campaign-xf_1gmhz-audit.json` 用于验证脚本对短测及缺少末段反馈的处理，不能将其不同场景的覆盖值与长跑直接比较。

当前能得出的结论是：资源前缀已有混合负载转换量证据，随机部分的探索增益尚未隔离；父种子选择存在实际饥饿，联合反馈的归因和实验验证不足。下一次开发应首先完成 A1 的选择修复和计量，单独验证前缀，再做 A2/A3；暂时没有证据支持直接宣布某套新权重或 RL 调度更好。

## 9. 一手来源与阅读范围

访问/核对日期均为 2026-10-21。本次联网读取官方文档、GitHub 作者仓库/API 和 USENIX 论文页面；部分 USENIX 请求完成正文后以网络错误退出，因此仅依据实际取得的标题、作者、摘要与会议信息，不把它当作已精读全部 PDF。没有获取全部最新 fuzzing 论文的穷尽清单。AFL++ 网站和文档中个别默认值描述不一致，本报告不据此断言当前版本的默认 power schedule。

**[S1] syzkaller 官方源码，本地实际依赖版本。** 核对 `tools/src/syzkaller-master/{executor/executor.cc,pkg/fuzzer/fuzzer.go,pkg/fuzzer/job.go,pkg/corpus/corpus.go,pkg/corpus/prio.go,pkg/mgrconfig/config.go,pkg/signal/signal.go}`，具体函数与行号见 3.1。官方仓库定位：

```text
https://github.com/google/syzkaller
```

**[S2] AFL++ 官方 power schedules 文档、官方 stable 分支方法说明及 bitmap 实现。** 方法说明通过 GitHub API 读取，文件 blob SHA 为 `aaaa641b59e00dc831c50da64d56c4edca3ad226`；频率调度以官方 power schedules 页面为依据。

```text
https://aflplus.plus/docs/power_schedules/
https://github.com/AFLplusplus/AFLplusplus/blob/stable/docs/afl-fuzz_approach.md
https://github.com/AFLplusplus/AFLplusplus/blob/stable/src/afl-fuzz-bitmap.c
```

**[S3] LLVM 官方 libFuzzer 文档与 FuzzerCorpus.h。** 通过官方 GitHub API 读取 `InputInfo::UpdateEnergy()`、`FeatureFreqs`、`NumExecutedMutations`、`EntropicOptions` 和语料添加/替换逻辑，确认更新所用统计及可配置的时间缩放。

```text
https://llvm.org/docs/LibFuzzer.html
https://github.com/llvm/llvm-project/blob/main/compiler-rt/lib/fuzzer/FuzzerCorpus.h
```

**[S4] StateFuzz: System Call-Based State-Aware Linux Driver Fuzzing。** Bodong Zhao 等，USENIX Security 2022。读取作者/会议页与摘要；本报告没有复述其未核对的详细调度公式。

```text
https://www.usenix.org/conference/usenixsecurity22/presentation/zhao-bodong
```

**[S5] SyzVegas: Beating Kernel Fuzzing Odds with Reinforcement Learning。** Daimeng Wang 等，USENIX Security 2021。读取作者/会议页与摘要；本报告只借鉴任务与种子选择的 MAB 思路，不声称已复现论文算法。

```text
https://www.usenix.org/conference/usenixsecurity21/presentation/wang-daimeng
```

**[S6] Janus: Fuzzing File Systems via Two-Dimensional Input Space Exploration。** IEEE S&P 2019，读取作者官方仓库 README，确认 image/syscall 两维输入、LKL 环境和重放产物。

```text
https://github.com/sslab-gatech/janus
https://gts3.org/assets/papers/2019/xu:janus.pdf
```

第二个 URL 为作者仓库提供的论文定位，本次未读取该 PDF。

**[S7] Linux 官方 KCOV 文档。** 已联网读取文档，并核对项目官方 Linux 6.12.1 源码中的 `Documentation/dev-tools/kcov.rst`；task 级采集、PC/CMP 模式以及 remote 标注约束以该基线文档为准。

```text
https://docs.kernel.org/dev-tools/kcov.html
```
