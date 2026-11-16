# unlink 补丁：原线程跟进

状态：**用户已执行正式发送，QQ SMTP 返回 `Result: 250`，邮件已由发送服务器接受**。
依据为用户提供的终端投递输出；邮件列表归档、最终收件状态及人工回复尚待确认。
实际收件人与下方核对结果一致，包括 Theodore Ts'o、ext4 reviewers、原 Fixes 作者及
列表，主题与原线程关联正确，无 Bcc。邮件解析、正文行宽及所引用日志已核对。

正式邮件为 [unlink-followup-20261116.eml](submissions/unlink-followup-20261116.eml)。
它保留原主题，以原邮件的实际 Message-ID 设置 `In-Reply-To` 和 `References`，
向 Theodore Ts'o 跟进，并补齐 ext4 reviewers、原 Fixes 作者及相关列表。

- [原邮件](https://lore.kernel.org/linux-ext4/tencent_D60E766CE07D3A1847B81DE9BC37B4466908@qq.com/)，2026-10-27 18:53:51 +0800。
- [原邮件镜像](https://www.spinics.net/lists/linux-ext4/msg113195.html)。
- [Sashiko AI 回复](https://www.spinics.net/lists/linux-ext4/msg113196.html)，2026-10-27 19:05:02 +0800；未发现问题，不等于维护者接收。

## 跟进投递记录

| 项目 | 用户提供的正式投递输出 |
|---|---|
| 邮件 Date | 2026-11-16 15:35:19 +0800 |
| SMTP 服务器 | smtp.qq.com |
| 发送结果 | `OK. Log says:`、`Result: 250` |
| 本地生成的 Message-ID | `<20261116073519.21804-1-3164729207@qq.com>` |
| In-Reply-To / References | `<tencent_D60E766CE07D3A1847B81DE9BC37B4466908@qq.com>` |
| 收件人数 | To 1 人、Cc 10 个地址，与下方清单一致 |
| 归档中的实际 Message-ID | 待确认；QQ 可能改写本地生成的 ID |

前一次演练的 `<20261116073455.21657-1-3164729207@qq.com>` 属于 dry-run，
不是本次正式发送的 Message-ID。当前状态不代表维护者已阅读、认可或合入补丁。

## 收件人核对

2026-11-16 通过 GitHub API 读取 torvalds/linux master 的
[`MAINTAINERS`](https://github.com/torvalds/linux/blob/master/MAINTAINERS)：
Ted 是 ext4 maintainer；Jan Kara、Andreas Dilger 等是明确列出的 reviewers。
本地主线 `get_maintainer.pl --no-git --no-git-fallback` 对 unlink 补丁的结果与此一致，
并从 `Fixes: 4209ae12b122` 找到原作者 Harshad Shirwadkar。

| 字段 | 收件人 |
|---|---|
| To | Theodore Ts'o `<tytso@mit.edu>` |
| Cc：ext4 reviewers | Andreas Dilger、Baokun Li、Jan Kara、Ojaswin Mujoo、Ritesh Harjani、Zhang Yi |
| Cc：Fixes 原作者 | Harshad Shirwadkar |
| Cc：列表 | linux-ext4、linux-kernel、stable |

extent 首版和 v2 的 To 同样是 Ted，但抄送了上述 ext4 reviewers。
本轮前面已取得的邮件镜像索引确认，Jan Kara 回复了
[首版](https://www.spinics.net/lists/linux-ext4/msg114422.html) 和
[v2](https://www.spinics.net/lists/linux-ext4/msg114506.html)。
unlink 原投稿没有直接抄送这些 reviewers，因此本次跟进补齐收件人。
这是提高审阅覆盖的调整，不能据此断言抄送差异就是回复速度不同的原因。

## 邮件中的证据

本次重新核对现存日志，没有重新运行 QEMU。运行时结果来自 Linux 6.12.1；
主线验证仍限于原先的 `fs/ext4/namei.o` 编译，不表述为完整主线回归通过。
实验方法、QMP 和完整日志索引见 [块层 EIO 报告](PHYSICAL-IO-CORE-20261102.md)。

- 未修复内核实际 READ EIO 与 unlink 返回值：[serial](results/physical-io-20261102/unfixed-serial.log)。
- 卸载后名字缺失而 raw inode 保留 Links: 1：[parent](results/physical-io-20261102/unfixed-disk0-parent-p.txt)、[inode](results/physical-io-20261102/unfixed-disk0-inode-2064.txt)、[e2fsck](results/physical-io-20261102/unfixed-disk0-e2fsck.log)。
- 独立复现的 JBD2 abort 探针、handle 状态与 TID 7 提交：[trace](results/unlink-jbd2-state-20261102/unlink-jbd2-serial.log)、[e2fsck](results/unlink-jbd2-state-20261102/unlink-jbd2-disk2-e2fsck.log)。
- 修复对照的离线名字、inode 与 fsck：[parent](results/physical-io-20261102/fixed-disk0-parent-p.txt)、[inode](results/physical-io-20261102/fixed-disk0-inode-2064.txt)、[e2fsck](results/physical-io-20261102/fixed-disk0-e2fsck.log)。

## 已执行的投递命令（留档）

以下命令已由用户执行，保留用于核对本次投稿配置。首次为演练，第二次为实际投递；
`git send-email` 的密码提示使用 QQ SMTP 授权码，输入内容不会显示。

```bash
cd /home/ikun/fuzzing-orcle

reply_mail="$PWD/experiments/ext4-source-fault-study/submissions/unlink-followup-20261116.eml"
reply_args=(
  --from='Alan <3164729207@qq.com>'
  --no-to --no-cc --no-bcc
  --to=tytso@mit.edu
  --cc=adilger.kernel@dilger.ca
  --cc=libaokun@linux.alibaba.com
  --cc=jack@suse.cz
  --cc=ojaswin@linux.ibm.com
  --cc=ritesh.list@gmail.com
  --cc=yi.zhang@huawei.com
  --cc=harshadshirwadkar@gmail.com
  --cc=linux-ext4@vger.kernel.org
  --cc=linux-kernel@vger.kernel.org
  --cc=stable@vger.kernel.org
  --suppress-cc=all
  --in-reply-to='<tencent_D60E766CE07D3A1847B81DE9BC37B4466908@qq.com>'
  --smtp-server=smtp.qq.com
  --smtp-server-port=465
  --smtp-encryption=ssl
  --smtp-user=3164729207@qq.com
  --confirm=never
)

git send-email --dry-run "${reply_args[@]}" "$reply_mail"

git send-email "${reply_args[@]}" "$reply_mail"
```

发送输出中的最终 `Result: 250` 表示 SMTP 服务器接受邮件，不等于收件人已经
阅读。保留发送输出，并以 QQ/lore 中实际邮件的 Message-ID 为准，QQ 可能改写
本地生成的 Message-ID。后续检查原线程归档和退信，取得归档中的实际 ID 后补充记录。
