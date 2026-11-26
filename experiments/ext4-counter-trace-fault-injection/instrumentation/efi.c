// SPDX-License-Identifier: GPL-2.0
/* One sequential program owns the session. Only native syscall windows count. */
#include <linux/debugfs.h>
#include <linux/ext4_efi.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/interrupt.h>
#include <linux/mutex.h>
#include <linux/rcupdate.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/stacktrace.h>
#include <linux/uaccess.h>
#include <trace/events/syscalls.h>
#define CREATE_TRACE_POINTS
#include <trace/events/ext4_efi.h>

#define EFI_SITES 588
#define EFI_PLANS 64
struct efi_plan { u32 call, site, occurrence; u64 context; bool hit, blocked; };
struct efi_session {
	struct task_struct *owner;
	dev_t dev;
	u64 run, order, formulas, sites, injected;
	u32 call, call_count, nplans;
	long nr;
	bool running, prepared, native_active, target, formula_seen;
	u32 occurrences[EFI_SITES];
	struct efi_plan plans[EFI_PLANS];
};
static DEFINE_MUTEX(efi_mutex);
static struct efi_session __rcu *efi_current;

static struct efi_session *efi_owner(void)
{
	struct efi_session *s = rcu_dereference(efi_current);
	return s && s->owner == current && !in_interrupt() ? s : NULL;
}

static void efi_sys_enter(void *data, struct pt_regs *regs, long id)
{
	struct efi_session *s;
	rcu_read_lock();
	s = efi_owner();
	if (s && s->running && s->prepared && id == s->nr) {
		s->prepared = false;
		s->native_active = true;
	}
	rcu_read_unlock();
}
static void efi_sys_exit(void *data, struct pt_regs *regs, long ret)
{
	struct efi_session *s;
	rcu_read_lock();
	s = efi_owner();
	if (s)
		s->native_active = false;
	rcu_read_unlock();
}

void ext4_efi_formula(struct super_block *sb, const char *point, const char *formula)
{
	struct efi_session *s;
	/* Scope boundaries and observer scans are not formula gates. */
	if (!strcmp(formula, "BEGIN") || !strcmp(formula, "END") ||
	    !strcmp(formula, "ALL") || !strncmp(point, "SNAPSHOT.", 9) ||
	    !strncmp(point, "MOUNT.", 6) || !strncmp(point, "UNMOUNT.", 8))
		return;
	rcu_read_lock();
	s = efi_owner();
	if (s && s->running && s->native_active && s->target && s->dev == sb->s_dev) {
		s->formula_seen = true;
		s->formulas++;
		trace_efi_formula(s->run, ++s->order, s->call, sb->s_dev, point, formula);
	}
	rcu_read_unlock();
}

struct ext4_efi_decision ext4_efi_before(u32 site, bool allowed)
{
	struct ext4_efi_decision d = {0};
	struct efi_session *s;
	unsigned long frames[24];
	u32 n, i;
	u64 hash = 1469598103934665603ULL;
	rcu_read_lock();
	s = efi_owner();
	if (!s || !s->running || !s->native_active || !s->target || site >= EFI_SITES)
		goto out;
	n = stack_trace_save(frames, ARRAY_SIZE(frames), 1);
	for (i = 0; i < n; i++) {
		hash ^= frames[i];
		hash *= 1099511628211ULL;
	}
	d.run = s->run;
	d.order = ++s->order;
	d.call = s->call;
	d.occurrence = ++s->occurrences[site];
	d.context = hash;
	d.eligible = s->formula_seen;
	d.allowed = allowed;
	d.tracked = true;
	s->sites++;
	for (i = 0; i < s->nplans; i++) {
		struct efi_plan *p = &s->plans[i];
		if (p->call != d.call || p->site != site || p->occurrence != d.occurrence ||
		    p->context != hash || p->hit)
			continue;
		if (allowed && d.eligible) {
			p->hit = true;
			d.inject = true;
			s->injected++;
		} else {
			p->blocked = true;
		}
	}
	trace_efi_site(d.run, d.order, d.call, site, d.occurrence, hash,
		       d.eligible, allowed, d.inject, false, 0, false);
out:
	rcu_read_unlock();
	return d;
}

void ext4_efi_after(u32 site, struct ext4_efi_decision *d, long result, bool pointer)
{
	if (pointer && result && !IS_ERR_VALUE((unsigned long)result))
		result = 1;
	trace_efi_site(d->run, d->order, d->call, site, d->occurrence,
		       d->context, d->eligible, d->allowed, d->inject, true, result, pointer);
}

static int efi_open(struct inode *inode, struct file *file)
{
	struct efi_session *s;
	if (!mutex_trylock(&efi_mutex))
		return -EBUSY;
	s = kzalloc(sizeof(*s), GFP_KERNEL);
	if (!s) { mutex_unlock(&efi_mutex); return -ENOMEM; }
	s->owner = current;
	file->private_data = s;
	rcu_assign_pointer(efi_current, s);
	return 0;
}
static int efi_release(struct inode *inode, struct file *file)
{
	rcu_assign_pointer(efi_current, NULL);
	synchronize_rcu();
	kfree(file->private_data);
	mutex_unlock(&efi_mutex);
	return 0;
}

static ssize_t efi_write(struct file *file, const char __user *buffer, size_t length, loff_t *pos)
{
	struct efi_session *s = file->private_data;
	char command[160], extra;
	unsigned int major, minor, call, site, occurrence;
	unsigned long long value;
	long nr;
	if (current != s->owner || !length || length >= sizeof(command))
		return -EINVAL;
	if (copy_from_user(command, buffer, length))
		return -EFAULT;
	command[length] = 0;
	if (sscanf(command, "begin %u %u %llu %c", &major, &minor, &value, &extra) == 3) {
		if (s->running || major > 0xfff || minor > 0xfffff || !value)
			return -EINVAL;
		s->dev = MKDEV(major, minor); s->run = value;
		s->running = true; s->formula_seen = false;
	} else if (sscanf(command, "plan %u %u %u %llu %c", &call, &site, &occurrence, &value, &extra) == 4) {
		if (s->running || s->nplans >= EFI_PLANS || !site || site >= EFI_SITES || !occurrence || call >= 256)
			return -EINVAL;
		s->plans[s->nplans++] = (struct efi_plan){call, site, occurrence, value, false, false};
	} else if (sscanf(command, "call %u %u %u %ld %c", &call, &major, &minor, &nr, &extra) == 4) {
		if (!s->running || s->prepared || call != s->call_count || call >= 256 || nr < 0)
			return -EINVAL;
		s->call = call; s->call_count++; s->nr = nr;
		s->target = s->dev == MKDEV(major, minor);
		memset(s->occurrences, 0, sizeof(s->occurrences));
		s->prepared = true;
	} else if (!strcmp(command, "leave\n")) {
		if (!s->running || s->prepared || s->native_active)
			return -EINVAL;
	} else if (!strcmp(command, "end\n")) {
		if (!s->running || s->prepared || s->native_active)
			return -EINVAL;
		s->running = false;
	} else {
		return -EINVAL;
	}
	return length;
}

static ssize_t efi_read(struct file *file, char __user *buffer, size_t length, loff_t *pos)
{
	struct efi_session *s = file->private_data;
	char *text;
	ssize_t result;
	size_t used;
	u32 i;
	if (current != s->owner || s->running)
		return -EBUSY;
	text = kzalloc(12288, GFP_KERNEL);
	if (!text)
		return -ENOMEM;
	used = scnprintf(text, 12288, "run=%llu calls=%u formulas=%llu sites=%llu injected=%llu\n",
			 s->run, s->call_count, s->formulas, s->sites, s->injected);
	for (i = 0; i < s->nplans; i++) {
		struct efi_plan *p = &s->plans[i];
		used += scnprintf(text + used, 12288 - used,
			"call=%u site_id=%u occurrence=%u context=%llu status=%s\n",
			p->call, p->site, p->occurrence, p->context,
			p->hit ? "injected" : p->blocked ? "blocked" : "not_reached");
	}
	result = simple_read_from_buffer(buffer, length, pos, text, used);
	kfree(text);
	return result;
}
static const struct file_operations efi_fops = {
	.open = efi_open, .release = efi_release, .write = efi_write,
	.read = efi_read,
};

static int __init ext4_efi_init(void)
{
	struct dentry *dir;
	int result;
	result = register_trace_sys_enter(efi_sys_enter, NULL);
	if (result) return result;
	result = register_trace_sys_exit(efi_sys_exit, NULL);
	if (result) { unregister_trace_sys_enter(efi_sys_enter, NULL); return result; }
	dir = debugfs_create_dir("ext4_efi", NULL);
	debugfs_create_file("session", 0600, dir, NULL, &efi_fops);
	return 0;
}
late_initcall(ext4_efi_init);
