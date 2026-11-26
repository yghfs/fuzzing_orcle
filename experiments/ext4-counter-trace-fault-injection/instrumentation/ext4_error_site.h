/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_EXT4_ERROR_SITE_H
#define _LINUX_EXT4_ERROR_SITE_H

#include <linux/types.h>
#include <linux/compiler.h>

/* Assigned per object by fs/ext4/Makefile; zero is never a reviewed TU. */
#ifndef EXT4_ERROR_SITE_TU
#define EXT4_ERROR_SITE_TU 0
#endif

#ifdef CONFIG_EXT4_ERROR_SITE_TRACE
extern bool ext4_error_site_enabled;
u64 ext4_error_site_enter(unsigned int site_id);
void ext4_error_site_return(unsigned int site_id, u64 invocation,
			    long result, bool is_pointer);
#endif

#if defined(CONFIG_EXT4_ERROR_SITE_TRACE) && EXT4_ERROR_SITE_TU
/*
 * Preserve the original expression's type, short-circuit position, argument
 * evaluation and return value. The expression is evaluated exactly once.
 * id=0 removes a shared-header probe outside its reviewed translation units.
 * No allocation, printk, filesystem IO or failure injection takes place here.
 */
#define EXT4_ES_CALL(id, expression) ({ \
	const unsigned int __ext4_es_id = (id); \
	u64 __ext4_es_invocation = 0; \
	if (__ext4_es_id && READ_ONCE(ext4_error_site_enabled)) \
		__ext4_es_invocation = ext4_error_site_enter(__ext4_es_id); \
	__auto_type __ext4_es_value = (expression); \
	if (__ext4_es_invocation) \
		ext4_error_site_return(__ext4_es_id, __ext4_es_invocation, \
			(long)__ext4_es_value, \
			__builtin_classify_type(__ext4_es_value) == 5); \
	__ext4_es_value; \
})
#else
#define EXT4_ES_CALL(id, expression) (expression)
#endif

#endif /* _LINUX_EXT4_ERROR_SITE_H */
