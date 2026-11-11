# extent 增深修复 v2：提交命令

v2 已按 Jan Kara 的要求修复发布新根之前的三个错误出口，并完成对照验证。
它是相对主线基线的完整补丁，不需要先应用首版。
首版及其投递记录保留；本次应发送文件名包含 `error-v2.patch` 的新版。

## 可选：先只发给自己

已准备单独的测试副本，邮件头不含维护者或列表收件地址，且 dry-run
确认 SMTP 收件人只有你的邮箱。命令如下：

```bash
cd /home/ikun/fuzzing-orcle/work/linux-mainline-extent-grow-submit

git send-email \
  --from='Alan <3164729207@qq.com>' \
  --no-to --no-cc --no-bcc \
  --suppress-cc=all \
  --to=3164729207@qq.com \
  --smtp-server=smtp.qq.com \
  --smtp-server-port=465 \
  --smtp-encryption=ssl \
  --smtp-user=3164729207@qq.com \
  --confirm=always \
  /home/ikun/fuzzing-orcle/work/extent-grow-selftest/extent-grow-v2-selftest.patch
```

这个命令会实际向你自己发邮件。确认收件人只有你的邮箱再输入 `y`。
需要密码时仍使用 QQ SMTP 授权码。

## 正式提交 v2

补丁包含 `[PATCH v2]` 标题和完整 To/Cc，保留与首版相同的
`Fixes: 8a83ac54940d` 标签。按用户要求移除长版变更清单和首版链接。
下面显式给出 To 和 In-Reply-To，避免首版投递时的交互提问，并将新版
关联到已归档的首版讨论线程。Jan Kara 已在 Cc 中。

先做演练：

```bash
git send-email --dry-run --confirm=never \
  --from='Alan <3164729207@qq.com>' \
  --to=tytso@mit.edu \
  --in-reply-to='<tencent_9EB8EBE910378E46C80E006C6A7DA918BA08@qq.com>' \
  --smtp-server=smtp.qq.com \
  --smtp-server-port=465 \
  --smtp-encryption=ssl \
  --smtp-user=3164729207@qq.com \
  /home/ikun/fuzzing-orcle/experiments/ext4-invariant-checker/patches/0002-ext4-free-unreachable-extent-block-on-error-v2.patch
```

确认输出中的主题为 `[PATCH v2]`，To 是 `tytso@mit.edu`，Cc 包含
`jack@suse.cz` 和 `linux-ext4@vger.kernel.org`，且 In-Reply-To 与上面一致。

再执行实际投递命令：

```bash
git send-email \
  --from='Alan <3164729207@qq.com>' \
  --to=tytso@mit.edu \
  --in-reply-to='<tencent_9EB8EBE910378E46C80E006C6A7DA918BA08@qq.com>' \
  --smtp-server=smtp.qq.com \
  --smtp-server-port=465 \
  --smtp-encryption=ssl \
  --smtp-user=3164729207@qq.com \
  --confirm=always \
  /home/ikun/fuzzing-orcle/experiments/ext4-invariant-checker/patches/0002-ext4-free-unreachable-extent-block-on-error-v2.patch
```

出现 `Send this email?` 时核对后输入 `y`。
正文已经回应 Jan 的意见并解释 v2 改动，不需要额外发送相同补丁的附件。
`Result: 250` 表示 SMTP 服务器接受邮件；留意抄送、归档和退信。

首版被 QQ 改写后的实际 Message-ID 是：

```text
<tencent_9EB8EBE910378E46C80E006C6A7DA918BA08@qq.com>
```

后续讨论或 v3 应以邮件列表归档中的真实 Message-ID 为准，QQ 也可能改写
v2 的本地 Message-ID。收到审阅回复使用“回复全部”。

验证细节见 [v2 报告](EXTENT-GROW-ERRORS-V2-20261110.md)。
