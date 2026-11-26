// SPDX-License-Identifier: GPL-2.0
/* Observational probes for the reviewed Linux 6.12.1 ext4 error sites. */
#include <linux/atomic.h>
#include <linux/debugfs.h>
#include <linux/err.h>
#include <linux/ext4_error_site.h>
#include <linux/init.h>
#include <linux/moduleparam.h>
#include <linux/seq_file.h>

#define CREATE_TRACE_POINTS
#include <trace/events/ext4_error_site.h>

struct ext4_error_site_desc {
	const char *translation_unit;
	const char *source;
	unsigned int line, column;
	const char *caller;
	const char *callee;
};

#include "error_site_table.h"

bool ext4_error_site_enabled;
module_param_named(trace_error_sites, ext4_error_site_enabled, bool, 0644);
MODULE_PARM_DESC(trace_error_sites, "Observe reviewed ext4 error sites (no fault injection)");

static atomic64_t ext4_es_invocation = ATOMIC64_INIT(0);
static atomic64_t ext4_es_hits[ARRAY_SIZE(ext4_error_sites)];
static atomic64_t ext4_es_returns[ARRAY_SIZE(ext4_error_sites)];

u64 ext4_error_site_enter(unsigned int site_id)
{
	u64 invocation;

	if (WARN_ON_ONCE(site_id >= ARRAY_SIZE(ext4_error_sites) ||
			 !ext4_error_sites[site_id].source))
		return 0;
	invocation = atomic64_inc_return(&ext4_es_invocation);
	atomic64_inc(&ext4_es_hits[site_id]);
	trace_error_site(site_id, invocation, false, 0, false);
	return invocation;
}

void ext4_error_site_return(unsigned int site_id, u64 invocation,
			    long result, bool is_pointer)
{
	/* Keep NULL and ERR_PTR results; expose successful pointers as 1. */
	if (is_pointer && result && !IS_ERR_VALUE((unsigned long)result))
		result = 1;
	atomic64_inc(&ext4_es_returns[site_id]);
	trace_error_site(site_id, invocation, true, result, is_pointer);
}

static int ext4_es_stats_show(struct seq_file *m, void *unused)
{
	unsigned int id;

	seq_puts(m, "site_id\thits\treturns\ttranslation_unit\tsource\tline\tcolumn\tcaller\tcallee\n");
	for (id = 1; id < ARRAY_SIZE(ext4_error_sites); id++) {
		const struct ext4_error_site_desc *site = &ext4_error_sites[id];

		if (!site->source)
			continue;
		seq_printf(m, "%u\t%lld\t%lld\t%s\t%s\t%u\t%u\t%s\t%s\n",
			   id, atomic64_read(&ext4_es_hits[id]),
			   atomic64_read(&ext4_es_returns[id]),
			   site->translation_unit, site->source,
			   site->line, site->column, site->caller, site->callee);
	}
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(ext4_es_stats);

static int __init ext4_error_site_init(void)
{
	struct dentry *root;

	root = debugfs_create_dir("ext4_error_sites", NULL);
	if (IS_ERR(root))
		return PTR_ERR(root);
	debugfs_create_bool("enabled", 0600, root, &ext4_error_site_enabled);
	debugfs_create_file("stats", 0400, root, NULL, &ext4_es_stats_fops);
	return 0;
}
/* CONFIG_EXT4_ERROR_SITE_TRACE requires built-in ext4. */
late_initcall(ext4_error_site_init);
