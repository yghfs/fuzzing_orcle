# KCOV 反馈闭环

这份文档只记录当前 ext4 counter-trace 的反馈实现和数据口径。历史 E2/E3 协议、结果和运行记录
已经删除，新的受控对照必须使用新的结果目录和新的随机 seed。

## 目标覆盖

文件系统目标范围是同一插桩内核中的 ext4 与 JBD2。`feedback/coverage.json` 分别保存
`ext4_pc_coverage`、`jbd2_pc_coverage`、`target_pc_coverage`、`ext4_edge_coverage` 和
`jbd2_edge_coverage`；`total_coverage` 仅作全内核诊断。PC 与 edge 永远分开统计，不能把
PC 数当作 edge 数，也不能把不同内核构建的地址集合直接合并。

目标符号来自 kallsyms 的文本符号范围，覆盖 `ext4_`、`_ext4_`、`__ext4_`、`ext4fs` 以及
`jbd2_`、`_jbd2_`、`__jbd2_` 前缀，并排除 trace、perf、prefix、event 辅助符号和数据符号。

## 在线闭环

```text
Generate(seed) 或 Mutate(corpus parent)
        |
保存 program、parent、模板、随机 seed 和调用元数据
        |
guest 执行并按 syscall 保存 KCOV PC/edge 范围
        |
host 将 native syscall、目标设备、返回值、修复/改写与调用范围配对
        |
分别更新 raw coverage、effect coverage 和 corpus
        |
只有当前 feedback mode 启用的新颖信号才允许输入进入 corpus
```

KCOV v5 每个 syscall 保存唯一 PC/edge feature 和调用范围。`(pc, 0)` 表示 PC，非零目标表示同一
syscall 内的 edge；调用边界清晰，因此不会跨 syscall 合成 edge。重复执行只保留一次 feature，
同时保留原始执行计数用于诊断。初始化失败、截断、非法范围、overflow、超时或 native syscall
未核验的样本不更新覆盖，也不进入 corpus。

## Effect 归因

生命周期证据按 `(program, call_index)` 与 KCOV 范围配对，调用分类为：

- `target_success_unmodified`
- `target_failure_unmodified`
- `target_success_adapted`
- `target_failure_adapted`
- `non_target`
- `unverified`

raw 覆盖保留所有完整样本。effect 覆盖只使用目标设备匹配、且没有参数修复或改写的目标调用；
失败调用仍可贡献错误路径覆盖。`--require-target-success` 可以要求一个输入至少包含一个成功且
未改写的目标调用。

## 生成与调度

resource-valid 输入使用 syzkaller typed `Prog.Mutate` 保留并演化资源关系；程序元数据保存调用
来源、parent hash、模板、连续保留前缀和实际变异预算。普通 `Generate`、resource seed mutation
和 resource parent mutation 的来源必须分开记录。均匀轮转是可审计基线，`rarity_fair` 是独立
策略；动态预算也必须单独开实验，不能和反馈信号同时改变。

2026-11-12 修正：typed 父程序按 `resource_template` 分池，不再由父程序覆盖计划模板。
combined 模式默认每个模板的第 0、4、8……个实际 typed 槽位从 seed 刷新；
`--resource-seed-refresh-every N` 可调整，0 关闭周期刷新。模板没有可用父程序或全部冷却时
也从该模板重新播种。seed_only / parent_only 保持各自实验含义，后者仅在无活跃父程序时
产生明确标记的 bootstrap。每次选择记录计划模板、实际模板、模板槽位和选择原因。

`target_scope=ext4_jbd2` 同时约束 PC 和 edge 的准入、edge rarity/频率与子代 edge 收益；
全内核 raw/effect edge 仍保留为诊断字段。新结果含 `feedback_edge_scope`，不能将旧结果的
全内核 edge 奖励与新结果当作同一种处理条件。

## 每次运行必须保留的字段

- ext4、JBD2 的 raw/effect PC 和 edge 累计集合及分段时间曲线；
- 每个输入的 native 核验结果、目标操作成功/失败、修复/改写分类；
- 唯一输入内容 hash、输入来源、parent hash、准入与抑制原因；
- KCOV 完整性、饱和/丢失原因、generated-syscall attribution；
- 内核、rootfs、起始镜像、syscall allowlist、反馈模式、调度策略和随机 seed 的 hash。

单个实验的异常、崩溃或 checker 事件可以作为后续安全验证材料保存，但不改变本反馈采集器的
覆盖统计口径。

## 运行入口

单个 campaign 使用：

```sh
bash experiments/ext4-counter-trace/scripts/run-campaign.sh \
  --mode single --duration-seconds 10800 --segments 10000 \
  --target-scope ext4_jbd2 --workers 1
```

当前三臂对照入口为 `scripts/run-target-coverage-comparison.sh`，并行运行 native
`syz-manager`、custom PC-only 和 custom full-feedback；旧 E2/E3 矩阵入口已删除。
单独运行各臂时仍可使用 `run-campaign.sh` 和独立结果目录。

当前保留结果见 [覆盖审计](research/2026-11-11-target-coverage/README.md)。一小时实验
full-feedback 的 admitted-corpus 目标覆盖较高，但全部完整样本并集略低；十二小时
单 seed 审计中 full-feedback 的 effect/corpus 和 raw 目标覆盖均低于 PC-only。
不能据此宣称联合反馈稳定提升覆盖，也不能把单次较低结果当作机制无效的因果证明。
KCOV 样本完整、公式生命周期完整和整轮通过是三个不同的字段；十二小时两臂只满足前者。
