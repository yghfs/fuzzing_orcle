# ext4 extent 增深 ENOMEM 修复：邮件补丁与提交方法

更新：首版已由用户发送。Jan Kara 要求补齐其他未发布新块的错误出口，
现已完成 v2；本文件保留首版说明，新的提交命令请使用
[v2 提交指南](EXTENT-GROW-V2-SUBMISSION.md)。

已整理好可供上游审阅的单独补丁：
[0002-ext4-free-unreachable-extent-block-on-enomem.patch](patches/0002-ext4-free-unreachable-extent-block-on-enomem.patch)。
作者和签署使用你指定的既有补丁中的 `Alan <3164729207@qq.com>`。
本文件记录首版准备和投递方法；首版现已由用户发送。

## 补丁现在的格式

补丁来自真实提交 `4c2a9240fdff9b59f0c1b908327e9a2d57c1a987`，由
`git format-patch` 生成。提交位于：

```text
worktree: /home/ikun/fuzzing-orcle/work/linux-mainline-extent-grow-submit
branch:   fix/ext4-extent-grow-enomem
base:     d135ab453a7400a34930bdda1ef4bdc8acfac80e
```

邮件包含 `From`、`Date`、`Subject: [PATCH] ext4: ...`、`To/Cc`、问题和修复
说明、测试情况、`Fixes:`、`Cc: stable@vger.kernel.org`、`Signed-off-by`、
diffstat、带 index 的 diff 和 `base-commit:`。
这些是内核常用邮件补丁格式；格式合规不等于已经获得维护者认可。

`Signed-off-by` 是按内核 DCO 对贡献来源及提交权利的声明。
`Fixes: 8a83ac54940d` 指向移除该调用链隐含 `__GFP_NOFAIL` 的提交，
说明为什么这个未回滚的 NULL 返回分支会变得可达。

这是一个独立修复，使用 `[PATCH]`，无需 cover letter。文件名中的 `0002`
只是本项目的编号，不表示必须和 unlink 修复一起提交，也不需要改成
`[PATCH 2/2]`。补丁不含 checker、故障注入或实验脚本。

## 已完成的验证

- `checkpatch.pl --no-tree`：0 errors、0 warnings，包含签署检查。
- 主线基线 `git apply --check` 通过；原版 6.12.1 无 fuzz 应用检查通过。
- 邮件可由 `git mailinfo` 正确解析；`git send-email --dry-run` 成功。
- 新邮件补丁产生的代码与此前实际构建的修复代码一致。
- Linux 6.12.1 故障注入回归：修复前 7 个不可达块，7 个 inode 的
  `st_blocks=40` 而预期为 32；修复后失败仍返回 ENOMEM，`st_blocks=32`，
  正常卸载后的 `e2fsck -fn` 返回 0，重试、截断和正常增深也通过。

测试是在调用方获取 buffer 的边界注入 NULL，未模拟 helper 内部真实内存
耗尽；主线基线上此次只检查了应用和格式。完整证据见
[验证报告](EXTENT-GROW-ENOMEM-FIX-20261110.md)。

## 收件人

已从当前主线 `MAINTAINERS` 用 `scripts/get_maintainer.pl` 核对：

- To：`Theodore Ts'o <tytso@mit.edu>`。
- Cc：`linux-ext4@vger.kernel.org`、`linux-kernel@vger.kernel.org`，
  ext4 reviewers，以及 `stable@vger.kernel.org`。

具体地址已写进补丁头，`git send-email` 会读取它们，正文中的 stable 地址
也会自动去重。先向 ext4 上游提交；stable 标签用于提示后续回移，不需要
先单独向 stable 提交一个不同版本。

若过一段时间才投递，先更新目标树并重新核对维护者和相关修复是否已合入；
当前邮件标明了准备时使用的确切基线。

## 用 QQ 邮箱提交

在 QQ 邮箱设置中开启 SMTP 服务，获取用于第三方客户端的**授权码**。
SMTP 登录名是完整邮箱，密码提示处使用授权码。
使用 `git send-email` 以正文发送补丁，可保留 diff 的空白与换行。

先在终端执行演练：

```bash
cd /home/ikun/fuzzing-orcle/work/linux-mainline-extent-grow-submit

patch_file=/home/ikun/fuzzing-orcle/experiments/ext4-invariant-checker/patches/0002-ext4-free-unreachable-extent-block-on-enomem.patch

git send-email --dry-run --confirm=never \
  --from='Alan <3164729207@qq.com>' \
  --smtp-server=smtp.qq.com \
  --smtp-server-port=465 \
  --smtp-encryption=ssl \
  --smtp-user=3164729207@qq.com \
  "$patch_file"
```

检查输出中的 From、To、Cc 和 Subject。`Dry-OK`/`Result: OK` 表示邮件生成
检查通过；dry-run 不会发送邮件，也不证明 SMTP 登录成功。
自动给自己抄送一份是正常行为。

确认内容后，在同一个终端执行下面的实际发送命令：

```bash
git send-email --confirm=always \
  --from='Alan <3164729207@qq.com>' \
  --smtp-server=smtp.qq.com \
  --smtp-server-port=465 \
  --smtp-encryption=ssl \
  --smtp-user=3164729207@qq.com \
  "$patch_file"
```

命令会显示待发送信息并要求确认，确认后才投递。需要密码时输入 QQ SMTP
授权码，避免把授权码直接写在命令行、补丁或提交记录里。
这里使用的 `--confirm=always` 是供你实际投递时核对收件人的客户端选项。

## 收到审阅意见后

保存实际发送输出里的 Message-ID，并留意 ext4 邮件列表的回复。
讨论时使用“回复全部”，保留邮件线程，针对具体意见逐条答复。
如一两周没有回应，可在原线程简短询问。

需要改代码时，在上述 worktree 内修改，用与修改范围相符的检查验证后：

```bash
git add fs/ext4/extents.c
git -c user.name=Alan -c user.email=3164729207@qq.com commit --amend

git format-patch -v2 -1 \
  --base=d135ab453a7400a34930bdda1ef4bdc8acfac80e \
  -o /home/ikun/fuzzing-orcle/work/extent-grow-v2
```

保留已有 `Signed-off-by`。若重新基于其他提交制作补丁，应把 `--base` 更新
为新的实际基线。这个简写的 v2 导出命令不添加 To/Cc；发送 v2 时需要按上文
收件人清单补齐 `--to`、`--cc`，或在 format-patch 时添加相同选项。
在 v2 邮件正文的 `---` 下方、diffstat 前写简短的“Changes in v2”，说明
本轮调整；它不会成为提交日志的一部分。再次运行 checkpatch 和 dry-run。

投递 v2 可在 `git send-email` 中加
`--in-reply-to='<首版实际发送的 Message-ID>'`，关联原来的讨论线程。
只有得到明确的 Reviewed-by/Tested-by 等确认时才添加相应标签。

参考：[内核补丁提交指南](https://docs.kernel.org/process/submitting-patches.html)、
[stable 规则](https://docs.kernel.org/process/stable-kernel-rules.html)、
[git-send-email 教程](https://git-send-email.io/)。
