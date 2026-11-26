/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM ext4_efi
#if !defined(_TRACE_EXT4_EFI_H) || defined(TRACE_HEADER_MULTI_READ)
#define _TRACE_EXT4_EFI_H
#include <linux/tracepoint.h>
TRACE_EVENT(efi_site,
	TP_PROTO(u64 run, u64 order, u32 call, u32 site, u32 occurrence,
		 u64 context, bool eligible, bool allowed, bool injected,
		 bool returned, long result, bool pointer),
	TP_ARGS(run, order, call, site, occurrence, context, eligible, allowed,
		injected, returned, result, pointer),
	TP_STRUCT__entry(
		__field(u64, run) __field(u64, order) __field(u32, call)
		__field(u32, site) __field(u32, occurrence) __field(u64, context)
		__field(bool, eligible) __field(bool, allowed) __field(bool, injected)
		__field(bool, returned) __field(long, result) __field(bool, pointer)
	),
	TP_fast_assign(
		__entry->run = run; __entry->order = order; __entry->call = call;
		__entry->site = site; __entry->occurrence = occurrence;
		__entry->context = context; __entry->eligible = eligible;
		__entry->allowed = allowed; __entry->injected = injected;
		__entry->returned = returned; __entry->result = result;
		__entry->pointer = pointer;
	),
	TP_printk("run=%llu order=%llu call=%u site_id=%u occurrence=%u context=%llu eligible=%u allowed=%u injected=%u phase=%s result=%ld pointer=%u",
		__entry->run, __entry->order, __entry->call, __entry->site,
		__entry->occurrence, __entry->context, __entry->eligible,
		__entry->allowed, __entry->injected,
		__entry->returned ? "return" : "enter", __entry->result, __entry->pointer)
);
TRACE_EVENT(efi_formula,
	TP_PROTO(u64 run, u64 order, u32 call, dev_t dev, const char *point, const char *formula),
	TP_ARGS(run, order, call, dev, point, formula),
	TP_STRUCT__entry(
		__field(u64, run) __field(u64, order) __field(u32, call)
		__field(dev_t, dev) __string(point, point) __string(formula, formula)
	),
	TP_fast_assign(
		__entry->run = run; __entry->order = order; __entry->call = call;
		__entry->dev = dev; __assign_str(point); __assign_str(formula);
	),
	TP_printk("run=%llu order=%llu call=%u dev=%u,%u point=%s formula=%s",
		__entry->run, __entry->order, __entry->call,
		MAJOR(__entry->dev), MINOR(__entry->dev), __get_str(point), __get_str(formula))
);
#endif
#undef TRACE_INCLUDE_FILE
#define TRACE_INCLUDE_FILE ext4_efi
#include <trace/define_trace.h>
