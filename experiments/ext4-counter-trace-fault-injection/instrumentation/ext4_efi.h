/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_EXT4_EFI_H
#define _LINUX_EXT4_EFI_H
#include <linux/ext4_error_site.h>
struct super_block;
struct ext4_efi_decision {
	u64 run, order, context;
	u32 call, occurrence;
	bool eligible, allowed, inject, tracked;
};
#ifdef CONFIG_EXT4_ERROR_SITE_TRACE
struct ext4_efi_decision ext4_efi_before(u32 site, bool allowed);
void ext4_efi_after(u32 site, struct ext4_efi_decision *d, long result, bool pointer);
void ext4_efi_formula(struct super_block *sb, const char *point, const char *formula);
#else
static inline void ext4_efi_formula(struct super_block *sb, const char *p, const char *f) {}
#endif

#if defined(CONFIG_EXT4_ERROR_SITE_TRACE) && EXT4_ERROR_SITE_TU
/* Arguments are evaluated by the generated outer wrapper before this macro.
 * A skipped call never acquires a resource. Per-API cleanup models consuming
 * input references (d_make_root) even on failure. */
#define EXT4_EFI_CALL(id, allowed, failure, cleanup, expression) ({ \
	const u32 __efi_id = (id); \
	u64 __efi_inv = 0; \
	struct ext4_efi_decision __efi_d = {0}; \
	if (__efi_id) { \
		if (READ_ONCE(ext4_error_site_enabled)) \
			__efi_inv = ext4_error_site_enter(__efi_id); \
		__efi_d = ext4_efi_before(__efi_id, (allowed)); \
	} \
	__typeof__(expression) __efi_result; \
	if (__efi_d.inject) { \
		cleanup; \
		__efi_result = (__typeof__(expression))(failure); \
	} else { \
		__efi_result = (expression); \
	} \
	if (__efi_inv) \
		ext4_error_site_return(__efi_id, __efi_inv, (long)__efi_result, \
			__builtin_classify_type(__efi_result) == 5); \
	if (__efi_d.tracked) \
		ext4_efi_after(__efi_id, &__efi_d, (long)__efi_result, \
			__builtin_classify_type(__efi_result) == 5); \
	__efi_result; \
})
#else
#define EXT4_EFI_CALL(id, allowed, failure, cleanup, expression) (expression)
#endif
#endif
