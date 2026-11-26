/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM ext4_error

#if !defined(_TRACE_EXT4_ERROR_SITE_H) || defined(TRACE_HEADER_MULTI_READ)
#define _TRACE_EXT4_ERROR_SITE_H
#include <linux/tracepoint.h>

TRACE_EVENT(error_site,
	TP_PROTO(unsigned int site_id, u64 invocation, bool returned,
		 long result, bool is_pointer),
	TP_ARGS(site_id, invocation, returned, result, is_pointer),
	TP_STRUCT__entry(
		__field(unsigned int, site_id)
		__field(u64, invocation)
		__field(bool, returned)
		__field(long, result)
		__field(bool, is_pointer)
	),
	TP_fast_assign(
		__entry->site_id = site_id;
		__entry->invocation = invocation;
		__entry->returned = returned;
		__entry->result = result;
		__entry->is_pointer = is_pointer;
	),
	TP_printk("schema=1 site_id=%u invocation=%llu phase=%s result=%ld kind=%s",
		__entry->site_id, __entry->invocation,
		__entry->returned ? "return" : "enter", __entry->result,
		!__entry->returned ? "pending" :
		__entry->is_pointer ? "pointer" : "integer")
);
#endif

#undef TRACE_INCLUDE_FILE
#define TRACE_INCLUDE_FILE ext4_error_site
#include <trace/define_trace.h>
