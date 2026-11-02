/* Injected into syzkaller's sequential C export. Only generated calls use ct_call. */
#include <sys/resource.h>
#include <sys/stat.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <linux/kcov.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <unistd.h>

#define CT_KCOV_MAX_ENTRIES 16777216UL
#define CT_KCOV_FLAG_SATURATED 1ULL
#define CT_KCOV_FLAG_CALL_RANGES 4ULL
#define CT_KCOV_FLAG_PER_CALL_FULL 8ULL
#define CT_KCOV_FLAG_TOTAL_LIMIT 16ULL
#define CT_KCOV_FLAG_IO_FAILURE 64ULL
#define CT_KCOV_FLAG_COMPACT 128ULL
#define CT_KCOV_FLAG_FEATURE_SET 256ULL
#define CT_KCOV_FLAG_FEATURE_TABLE_LIMIT 512ULL
#define CT_KCOV_MAX_TOTAL_PCS 16777216UL
#define CT_KCOV_FEATURE_TABLE_SIZE 1048576UL
#define CT_FALLOCATE_DEFAULT_MAX_BYTES (64ULL * 1024ULL * 1024ULL)


static void ct_phase(const char *edge, const char *source)
{
  int fd = open("/sys/kernel/tracing/trace_marker", O_WRONLY | O_CLOEXEC);
  if (fd < 0) { perror("phase marker"); exit(90); }
  char line[256];
  int n = snprintf(line, sizeof(line),
                   "CT_PHASE phase=%s source=%s target_pid=%ld id=%s-%ld\n",
                   edge, source, (long)getpid(), source, (long)getpid());
  if (n < 0 || n >= (int)sizeof(line) || write(fd, line, n) != n) exit(90);
  close(fd);
}

static int ct_kcov_fd = -1;
static uint64_t *ct_kcov_area;
static size_t ct_kcov_entries;
static int ct_kcov_done;
static int ct_kcov_active;
static int ct_kcov_saturated;
static uint64_t ct_kcov_saturation_reasons;
static FILE *ct_kcov_spool;
static int ct_kcov_spool_failed;
static size_t ct_kcov_count;
static uint64_t ct_kcov_raw_count;
static int ct_kcov_feature_set;
struct ct_kcov_feature { uint64_t from, to; uint32_t generation; };
static struct ct_kcov_feature *ct_kcov_feature_table;
static size_t ct_kcov_feature_used;
static uint32_t ct_kcov_feature_generation;
static size_t ct_kcov_max_call_pcs;
static unsigned ct_kcov_max_call_index;
#define CT_KCOV_MAX_CALLS 256
struct ct_kcov_call_range { uint64_t start, count; };
static struct ct_kcov_call_range ct_kcov_calls[CT_KCOV_MAX_CALLS];
static size_t ct_kcov_call_count;
static size_t ct_kcov_current_call = CT_KCOV_MAX_CALLS;
static int ct_stable_fd = -1;
static int ct_stable_fd_backup = -1;
static unsigned long ct_fd_repairs;
static unsigned long ct_data_calls;
static unsigned long ct_fallocate_adaptations;
static unsigned long long ct_fallocate_max_bytes = CT_FALLOCATE_DEFAULT_MAX_BYTES;
static int ct_fault_stop_after_hit;

/*
 * Fault injection is deliberately an opt-in lane.  The generated process
 * arms one operation for one generated call and records the controller state
 * before disabling it at process exit.  Keeping this state in the workload
 * gives the host an exact PID/program/call boundary even when several
 * workers share a mounted filesystem.
 */
#define CT_FAULT_PATH_MAX 512
#define CT_FAULT_VALUE_MAX 96
#define CT_FAULT_CALL_ANY UINT_MAX
static int ct_fault_enabled;
static int ct_fault_armed;
static unsigned ct_fault_call_index;
static unsigned ct_fault_count = 1;
static unsigned ct_fault_generation = 1;
static char ct_fault_path[CT_FAULT_PATH_MAX];
static char ct_fault_status_path[CT_FAULT_PATH_MAX];
static char ct_fault_operation[CT_FAULT_VALUE_MAX];
static char ct_fault_errno[CT_FAULT_VALUE_MAX] = "-EIO";
static char ct_fault_program[CT_FAULT_VALUE_MAX];
static char ct_fault_evidence_dir[CT_FAULT_PATH_MAX];

static int ct_write_all(int fd, const void *buffer, size_t length);

static int ct_fault_write_all(int fd, const char *text)
{
  size_t length = strlen(text);
  return ct_write_all(fd, text, length);
}

static int ct_fault_write_path(const char *path, const char *text)
{
  int fd = open(path, O_WRONLY | O_CLOEXEC);
  int result;
  if (fd < 0) return -1;
  result = ct_fault_write_all(fd, text);
  close(fd);
  return result;
}

static void ct_fault_marker(const char *kind, const char *phase,
                            unsigned call_index, const char *status)
{
  int fd = open("/sys/kernel/tracing/trace_marker", O_WRONLY | O_CLOEXEC);
  char line[768];
  int length;
  if (fd < 0) return;
  length = snprintf(line, sizeof(line),
                    "CT_%s phase=%s target_pid=%ld program=%s call_index=%u "
                    "operation=%s errno=%s generation=%u status=%s\n",
                    kind, phase, (long)getpid(),
                    ct_fault_program[0] ? ct_fault_program : "unknown",
                    call_index, ct_fault_operation[0] ? ct_fault_operation : "off",
                    ct_fault_errno, ct_fault_generation,
                    status ? status : "unknown");
  if (length > 0 && length < (int)sizeof(line))
    (void)write(fd, line, (size_t)length);
  close(fd);
}

static int ct_fault_parse_unsigned(const char *name, unsigned *value,
                                   unsigned fallback, unsigned maximum)
{
  const char *text = getenv(name);
  char *end = NULL;
  unsigned long parsed;
  if (!text || !*text) {
    *value = fallback;
    return 0;
  }
  parsed = strtoul(text, &end, 10);
  if (!end || *end || parsed > maximum) return -1;
  *value = (unsigned)parsed;
  return 0;
}

static void ct_fault_status_evidence(unsigned call_index, const char *phase)
{
  int fd;
  char status[2048];
  ssize_t length;
  char path[CT_FAULT_PATH_MAX];
  if (!ct_fault_enabled || !ct_fault_status_path[0]) return;
  fd = open(ct_fault_status_path, O_RDONLY | O_CLOEXEC);
  if (fd < 0) return;
  length = read(fd, status, sizeof(status) - 1);
  close(fd);
  if (length <= 0) return;
  status[length] = '\0';
  /* Keep trace_marker one-line and retain a bounded copy for host evidence. */
  for (ssize_t i = 0; i < length; i++)
    if (isspace((unsigned char)status[i])) status[i] = '_';
  ct_fault_marker("FAULT_STATUS", phase, call_index, status);
  if (!ct_fault_evidence_dir[0] || !ct_fault_program[0]) return;
  if (snprintf(path, sizeof(path), "%s/%s-call-%u-%s.status",
               ct_fault_evidence_dir, ct_fault_program, call_index, phase) >=
      (int)sizeof(path)) return;
  fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  if (fd >= 0) {
    (void)ct_fault_write_all(fd, status);
    (void)ct_fault_write_all(fd, "\n");
    close(fd);
  }
}

static void ct_fault_disable(void)
{
  if (!ct_fault_enabled || !ct_fault_path[0]) return;
  (void)ct_fault_write_path(ct_fault_path, "off\n");
  ct_fault_marker("FAULT_ARM", "END", ct_fault_call_index, "off");
  ct_fault_armed = 0;
}

static void ct_fault_init(void)
{
  const char *path = getenv("CT_FAULT_SYSFS");
  const char *operation = getenv("CT_FAULT_OPERATION");
  const char *fault_errno = getenv("CT_FAULT_ERRNO");
  const char *program = getenv("CT_PROGRAM_ID");
  const char *status = getenv("CT_FAULT_STATUS");
  const char *evidence = getenv("CT_FAULT_EVIDENCE_DIR");
  size_t path_length;
  if (!path || !*path || !operation || !*operation) return;
  if (snprintf(ct_fault_path, sizeof(ct_fault_path), "%s", path) >=
      (int)sizeof(ct_fault_path)) exit(91);
  if (status && *status) {
    if (snprintf(ct_fault_status_path, sizeof(ct_fault_status_path), "%s", status) >=
        (int)sizeof(ct_fault_status_path)) exit(91);
  } else {
    path_length = strlen(path);
    if (path_length < strlen("fault_inject") ||
        strcmp(path + path_length - strlen("fault_inject"), "fault_inject"))
      exit(91);
    if (snprintf(ct_fault_status_path, sizeof(ct_fault_status_path), "%.*s%s",
                 (int)(path_length - strlen("fault_inject")), path,
                 "fault_inject_status") >= (int)sizeof(ct_fault_status_path))
      exit(91);
  }
  if (snprintf(ct_fault_operation, sizeof(ct_fault_operation), "%s", operation) >=
      (int)sizeof(ct_fault_operation)) exit(91);
  if (fault_errno && *fault_errno &&
      snprintf(ct_fault_errno, sizeof(ct_fault_errno), "%s", fault_errno) >=
      (int)sizeof(ct_fault_errno)) exit(91);
  if (program && *program &&
      snprintf(ct_fault_program, sizeof(ct_fault_program), "%s", program) >=
      (int)sizeof(ct_fault_program)) exit(91);
  if (evidence && *evidence &&
      snprintf(ct_fault_evidence_dir, sizeof(ct_fault_evidence_dir), "%s", evidence) >=
      (int)sizeof(ct_fault_evidence_dir)) exit(91);
  if (ct_fault_parse_unsigned("CT_FAULT_CALL_INDEX", &ct_fault_call_index, 0,
                              CT_KCOV_MAX_CALLS - 1) < 0 ||
      ct_fault_parse_unsigned("CT_FAULT_COUNT", &ct_fault_count, 1, 1000000) < 0 ||
      ct_fault_parse_unsigned("CT_FAULT_GENERATION", &ct_fault_generation, 1,
                              0x7fffffffU) < 0 || !ct_fault_count)
    exit(91);
  ct_fault_enabled = 1;
  ct_fault_marker("FAULT_ARM", "READY", ct_fault_call_index, "configured");
}

static int ct_barrier_fd(const char *name)
{
  const char *text = getenv(name);
  char *end = NULL;
  long fd;
  if (!text || !*text) return -1;
  fd = strtol(text, &end, 10);
  if (!end || *end || fd < 3 || fd > 1048576) exit(91);
  return (int)fd;
}

static void ct_wait_worker_barrier(void)
{
  int ready_fd = ct_barrier_fd("CT_READY_FD");
  int run_fd = ct_barrier_fd("CT_RUN_FD");
  char byte = 'r';
  ssize_t result;
  if (ready_fd < 0 && run_fd < 0) return;
  if (ready_fd < 0 || run_fd < 0) exit(91);
  do result = write(ready_fd, &byte, 1); while (result < 0 && errno == EINTR);
  close(ready_fd);
  if (result != 1) exit(91);
  do result = read(run_fd, &byte, 1); while (result < 0 && errno == EINTR);
  close(run_fd);
  if (result != 1) exit(91);
}

static void ct_fault_maybe_arm(unsigned call_index)
{
  char config[768];
  int length;
  if (!ct_fault_enabled || ct_fault_armed || call_index != ct_fault_call_index)
    return;
  length = snprintf(config, sizeof(config),
                    "operation=%s errno=%s count=%u pid=%ld program=%s "
                    "call_index=%u generation=%u\n",
                    ct_fault_operation, ct_fault_errno, ct_fault_count,
                    (long)getpid(), ct_fault_program[0] ? ct_fault_program : "unknown",
                    call_index, ct_fault_generation);
  if (length <= 0 || length >= (int)sizeof(config) ||
      ct_fault_write_path(ct_fault_path, config) < 0) {
    ct_fault_marker("FAULT_ARM", "END", call_index, "write_failed");
    return;
  }
  ct_fault_armed = 1;
  ct_fault_marker("FAULT_ARM", "END", call_index, "armed");
  ct_fault_status_evidence(call_index, "before");
}

static int ct_write_all(int fd, const void *buffer, size_t length)
{
  const unsigned char *cursor = buffer;
  while (length) {
    ssize_t written = write(fd, cursor, length);
    if (written < 0) {
      if (errno == EINTR) continue;
      return -1;
    }
    if (written == 0) return -1;
    cursor += written;
    length -= (size_t)written;
  }
  return 0;
}

static uint64_t ct_kcov_feature_hash(uint64_t from, uint64_t to)
{
  uint64_t value = from ^ (to + 0x9e3779b97f4a7c15ULL + (from << 6) + (from >> 2));
  value ^= value >> 30;
  value *= 0xbf58476d1ce4e5b9ULL;
  value ^= value >> 27;
  value *= 0x94d049bb133111ebULL;
  return value ^ (value >> 31);
}

static int ct_kcov_prepare_feature_table(void)
{
  if (ct_kcov_feature_table) {
    ct_kcov_feature_generation++;
    if (!ct_kcov_feature_generation) {
      memset(ct_kcov_feature_table, 0,
             CT_KCOV_FEATURE_TABLE_SIZE * sizeof(*ct_kcov_feature_table));
      ct_kcov_feature_generation = 1;
    }
    ct_kcov_feature_used = 0;
    return 0;
  }
  ct_kcov_feature_table = calloc(CT_KCOV_FEATURE_TABLE_SIZE,
                                 sizeof(*ct_kcov_feature_table));
  if (!ct_kcov_feature_table) {
    ct_kcov_saturated = 1;
    ct_kcov_saturation_reasons |= CT_KCOV_FLAG_FEATURE_TABLE_LIMIT;
    return -1;
  }
  ct_kcov_feature_used = 0;
  ct_kcov_feature_generation = 1;
  return 0;
}

/* Add one unique per-syscall PC or edge and spool it as a pair. */
static int ct_kcov_append_feature(uint64_t from, uint64_t to)
{
  size_t mask = CT_KCOV_FEATURE_TABLE_SIZE - 1;
  size_t slot;
  if (ct_kcov_spool_failed) return -1;
  if (!ct_kcov_feature_table && ct_kcov_prepare_feature_table() < 0) return -1;
  slot = (size_t)ct_kcov_feature_hash(from, to) & mask;
  for (size_t probe = 0; probe < CT_KCOV_FEATURE_TABLE_SIZE; probe++) {
    struct ct_kcov_feature *entry = &ct_kcov_feature_table[slot];
    if (entry->generation != ct_kcov_feature_generation) {
      if (ct_kcov_feature_used >= CT_KCOV_FEATURE_TABLE_SIZE - 1) {
        ct_kcov_saturated = 1;
        ct_kcov_saturation_reasons |= CT_KCOV_FLAG_FEATURE_TABLE_LIMIT;
        return -1;
      }
      entry->from = from;
      entry->to = to;
      entry->generation = ct_kcov_feature_generation;
      ct_kcov_feature_used++;
      if (ct_kcov_count >= CT_KCOV_MAX_TOTAL_PCS) {
        ct_kcov_saturated = 1;
        ct_kcov_saturation_reasons |= CT_KCOV_FLAG_TOTAL_LIMIT;
        return -1;
      }
      uint64_t feature[2] = {from, to};
      if (ct_write_all(fileno(ct_kcov_spool), feature, sizeof(feature)) < 0) {
        ct_kcov_saturated = 1;
        ct_kcov_spool_failed = 1;
        ct_kcov_saturation_reasons |= CT_KCOV_FLAG_IO_FAILURE;
        return -1;
      }
      ct_kcov_count++;
      return 1;
    }
    if (entry->from == from && entry->to == to) return 0;
    slot = (slot + 1) & mask;
  }
  ct_kcov_saturated = 1;
  ct_kcov_saturation_reasons |= CT_KCOV_FLAG_FEATURE_TABLE_LIMIT;
  return -1;
}

/*
 * Store one run of identical PCs.  The host expands each run to one PC (or
 * two when the run length is greater than one), which preserves every
 * distinct transition and self edge while keeping the compact sample small.
 */
static int ct_kcov_append_run(uint64_t pc, size_t run)
{
  uint64_t token[2];
  if (!run || ct_kcov_spool_failed) return 0;
  if (ct_kcov_count >= CT_KCOV_MAX_TOTAL_PCS) {
    ct_kcov_saturated = 1;
    ct_kcov_saturation_reasons |= CT_KCOV_FLAG_TOTAL_LIMIT;
    return 0;
  }
  token[0] = pc;
  token[1] = (uint64_t)run;
  if (ct_write_all(fileno(ct_kcov_spool), token, sizeof(token)) < 0) {
    ct_kcov_saturated = 1;
    ct_kcov_spool_failed = 1;
    ct_kcov_saturation_reasons |= CT_KCOV_FLAG_IO_FAILURE;
    return 0;
  }
  ct_kcov_count++;
  ct_kcov_raw_count += run;
  return 1;
}

static int ct_data_fd_call(const char *name)
{
  return !strcmp(name, "write") || !strcmp(name, "pwrite64") ||
         !strcmp(name, "fallocate") || !strcmp(name, "ftruncate") ||
         !strcmp(name, "fsync") || !strcmp(name, "fdatasync") ||
         !strcmp(name, "fchmod") || !strcmp(name, "fsetxattr");
}

static int ct_primary_data_call(const char *name)
{
  return !strcmp(name, "write") || !strcmp(name, "pwrite64") ||
         !strcmp(name, "fallocate") || !strcmp(name, "ftruncate") ||
         !strcmp(name, "fsync") || !strcmp(name, "fdatasync");
}

static int ct_inode_fd_call(const char *name)
{
  return ct_data_fd_call(name) || !strcmp(name, "fremovexattr") ||
         !strcmp(name, "fgetxattr") || !strcmp(name, "flistxattr") ||
         !strcmp(name, "fstat");
}

static void ct_inode_identity(const char *name, long fd,
                              unsigned long long *ino,
                              unsigned long long *dev, int *is_dir)
{
  struct stat st;
  *ino = 0;
  *dev = 0;
  *is_dir = 0;
  if (!ct_inode_fd_call(name) || fd < 0 || fd > INT_MAX) return;
  /* Capture identity before the syscall marker. The extra fstat is outside
   * the generated call window and cannot be mistaken for the target syscall. */
  if (fstat((int)fd, &st) == 0) {
    *ino = (unsigned long long)st.st_ino;
    *dev = (unsigned long long)st.st_dev;
    *is_dir = S_ISDIR(st.st_mode);
  }
}

static int ct_regular_fd(int fd)
{
  struct stat st;
  return fd >= 0 && fstat(fd, &st) == 0 && S_ISREG(st.st_mode);
}

static int ct_stable_data_fd(void)
{
  if (ct_regular_fd(ct_stable_fd)) return ct_stable_fd;
  if (ct_regular_fd(ct_stable_fd_backup)) {
    ct_stable_fd = ct_stable_fd_backup;
    return ct_stable_fd;
  }
  if (ct_stable_fd >= 0) close(ct_stable_fd);
  ct_stable_fd = open("./file0/file0", O_RDWR | O_CLOEXEC);
  if (ct_stable_fd < 0) ct_stable_fd = open("./file0/data", O_RDWR | O_CLOEXEC);
  if (ct_stable_fd < 0) _exit(91);
  return ct_stable_fd;
}

static int ct_reserved_fd(int fd)
{
  return fd >= 0 && (fd == ct_stable_fd || fd == ct_stable_fd_backup);
}

static long ct_repair_fd(const char *name, long fd, int *repaired)
{
  if (!ct_data_fd_call(name)) return fd;
  if (ct_primary_data_call(name)) ct_data_calls++;
  /* A directory fd is valid for fsync/fdatasync, but data and inode
   * operations require a regular file. Keep valid regular fds untouched. */
  if (ct_regular_fd((int)fd) ||
      ((!strcmp(name, "fsync") || !strcmp(name, "fdatasync")) &&
       fd >= 0 && fcntl((int)fd, F_GETFD) >= 0)) return fd;
  *repaired = 1;
  ct_fd_repairs++;
  return ct_stable_data_fd();
}

static long ct_bound_fallocate_length(long length, int *adapted)
{
  if (adapted) *adapted = 0;
  if (length > 0 && (unsigned long long)length > ct_fallocate_max_bytes) {
    if (adapted) *adapted = 1;
    return (long)ct_fallocate_max_bytes;
  }
  return length;
}

static void ct_kcov_stop(void)
{
  uint64_t count;
  if (!ct_kcov_active) return;
  if (ioctl(ct_kcov_fd, KCOV_DISABLE, 0) < 0) _exit(91);
  ct_kcov_active = 0;
  count = ct_kcov_area[0];
  if (count > ct_kcov_max_call_pcs) {
    ct_kcov_max_call_pcs = (size_t)count;
    ct_kcov_max_call_index = (unsigned)ct_kcov_current_call;
  }
  /* KCOV cannot report whether a full buffer lost its next PC, so a full
   * buffer is conservatively incomplete even when the kernel did not expose
   * an explicit overflow bit. */
  if (count >= ct_kcov_entries) {
    ct_kcov_saturated = 1;
    ct_kcov_saturation_reasons |= CT_KCOV_FLAG_PER_CALL_FULL;
  }
  if (count > ct_kcov_entries) count = ct_kcov_entries;
  if (count && !ct_kcov_spool_failed && ct_kcov_feature_set) {
    ct_kcov_raw_count += count;
    if (ct_kcov_prepare_feature_table() < 0) count = 0;
    uint64_t previous = 0;
    int have_previous = 0;
    for (size_t index = 0; index < (size_t)count; index++) {
      uint64_t pc = ct_kcov_area[1 + index];
      if (ct_kcov_append_feature(pc, 0) < 0) break;
      if (have_previous && ct_kcov_append_feature(previous, pc) < 0) break;
      previous = pc;
      have_previous = 1;
    }
  } else if (count && !ct_kcov_spool_failed) {
    size_t accepted = 0;
    while (accepted < (size_t)count) {
      size_t end = accepted + 1;
      while (end < (size_t)count &&
             ct_kcov_area[1 + end] == ct_kcov_area[1 + accepted]) end++;
      if (!ct_kcov_append_run(ct_kcov_area[1 + accepted], end - accepted))
        break;
      accepted = end;
    }
  }
  if (ct_kcov_current_call < CT_KCOV_MAX_CALLS) {
    struct ct_kcov_call_range *range = &ct_kcov_calls[ct_kcov_current_call];
    range->count = ct_kcov_count - range->start;
    ct_kcov_current_call = CT_KCOV_MAX_CALLS;
  }
}

static void ct_kcov_start(unsigned call_index)
{
  if (ct_kcov_active) ct_kcov_stop();
  if (call_index >= CT_KCOV_MAX_CALLS) _exit(91);
  ct_kcov_calls[call_index].start = ct_kcov_count;
  ct_kcov_calls[call_index].count = 0;
  if (ct_kcov_call_count <= call_index) ct_kcov_call_count = call_index + 1;
  ct_kcov_current_call = call_index;
  ct_kcov_area[0] = 0;
  if (ioctl(ct_kcov_fd, KCOV_ENABLE, KCOV_TRACE_PC) < 0) _exit(91);
  ct_kcov_area[0] = 0;
  ct_kcov_active = 1;
}

static void ct_kcov_finish(void)
{
  if (ct_kcov_done || ct_kcov_fd < 0) return;
  ct_kcov_done = 1;
  ct_kcov_stop();
  const char *dir = getenv("CT_COVERAGE_DIR");
  const char *program = getenv("CT_PROGRAM_ID");
  if (!dir || !program || !*dir || !*program) _exit(91);
  char path[512];
  int n = snprintf(path, sizeof(path), "%s/%s.bin", dir, program);
  if (n < 0 || n >= (int)sizeof(path)) _exit(91);
  int out = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  if (out < 0) _exit(91);
  uint64_t flags = (uint64_t)(ct_kcov_saturated ? CT_KCOV_FLAG_SATURATED : 0) |
                   CT_KCOV_FLAG_CALL_RANGES |
                   (ct_kcov_feature_set ? CT_KCOV_FLAG_FEATURE_SET : CT_KCOV_FLAG_COMPACT) |
                   ct_kcov_saturation_reasons;
  /* v4 count is compact runs; v5 count is unique PC/edge feature pairs. */
  uint64_t header[4] = {0x43544b434f563031ULL, ct_kcov_feature_set ? 5 : 4,
                        ct_kcov_count, flags};
  uint64_t call_count = ct_kcov_call_count;
  if (ct_write_all(out, header, sizeof(header)) < 0 ||
      ct_write_all(out, &ct_kcov_raw_count, sizeof(ct_kcov_raw_count)) < 0 ||
      lseek(fileno(ct_kcov_spool), 0, SEEK_SET) < 0) {
    close(out); _exit(91);
  }
  unsigned char copy_buffer[65536];
  size_t remaining = ct_kcov_count * 2 * sizeof(uint64_t);
  while (remaining) {
    size_t wanted = remaining < sizeof(copy_buffer) ? remaining : sizeof(copy_buffer);
    ssize_t got = read(fileno(ct_kcov_spool), copy_buffer, wanted);
    if (got < 0 && errno == EINTR) continue;
    if (got <= 0 || ct_write_all(out, copy_buffer, (size_t)got) < 0) {
      close(out); _exit(91);
    }
    remaining -= (size_t)got;
  }
  if (ct_write_all(out, &call_count, sizeof(call_count)) < 0 ||
      (call_count && ct_write_all(out, ct_kcov_calls,
                                  call_count * sizeof(ct_kcov_calls[0])) < 0)) {
    close(out); _exit(91);
  }
  if (fsync(out) < 0) { close(out); _exit(91); }
  close(out);
  dprintf(2, "CT_WORKLOAD_SUMMARY data_calls=%lu fd_repairs=%lu fallocate_adaptations=%lu kcov_count=%zu "
          "kcov_raw_count=%llu kcov_max_call_index=%u kcov_max_call_pcs=%zu "
          "saturation_flags=%llu\n",
          ct_data_calls, ct_fd_repairs, ct_fallocate_adaptations, ct_kcov_count,
          (unsigned long long)ct_kcov_raw_count, ct_kcov_max_call_index,
          ct_kcov_max_call_pcs, (unsigned long long)ct_kcov_saturation_reasons);
  munmap(ct_kcov_area, (ct_kcov_entries + 1) * sizeof(uint64_t));
  fclose(ct_kcov_spool);
  ct_kcov_spool = NULL;
  close(ct_kcov_fd);
  ct_kcov_fd = -1;
  /* A saturated sample still carries every PC recorded before the cap and
   * keeps the conservative bit set for the host. The counter-trace evidence
   * of this program is complete, so do not turn incomplete KCOV feedback
   * into a failed workload; the host records it as a coverage gap instead. */
}

static void ct_init(void)
{
  ct_phase("BEGIN", "runtime_init");
  const char *root = getenv("CT_ROOT");
  if (!root || chdir(root)) { perror("CT_ROOT"); exit(90); }
  /* Keep fallback descriptors valid after generated rename/unlink operations.
   * The directory entry may disappear, but an open regular file remains a
   * valid target for later data syscalls. */
  int seed_fd = open("./file0/file0", O_RDWR | O_CLOEXEC);
  if (seed_fd < 0) seed_fd = open("./file0/data", O_RDWR | O_CLOEXEC);
  if (seed_fd < 0) { perror("stable data file"); exit(91); }
  ct_stable_fd = fcntl(seed_fd, F_DUPFD_CLOEXEC, 240);
  if (ct_stable_fd < 0) { perror("stable data fd"); exit(91); }
  ct_stable_fd_backup = fcntl(ct_stable_fd, F_DUPFD_CLOEXEC, 241);
  if (ct_stable_fd_backup < 0) { perror("stable data fd backup"); exit(91); }
  close(seed_fd);
  const char *entries_text = getenv("CT_KCOV_ENTRIES");
  unsigned long entries = entries_text ? strtoul(entries_text, NULL, 10) : 65536;
  const char *fallocate_max_text = getenv("CT_FALLOCATE_MAX_BYTES");
  if (fallocate_max_text && *fallocate_max_text) {
    char *end = NULL;
    unsigned long long value = strtoull(fallocate_max_text, &end, 10);
    if (!end || *end || value < 4096 || value > (1ULL << 30)) exit(91);
    ct_fallocate_max_bytes = value;
  }
  /* Keep enough room for a full typed 16-call program.  The host still
   * rejects samples above its independent parser limit. */
  if (entries < 1024 || entries > CT_KCOV_MAX_ENTRIES) exit(91);
  /* KCOV's N is the number of unsigned-long words including slot 0, which
   * stores the count.  The remaining N-1 words hold PCs. */
  ct_kcov_entries = entries - 1;
  const char *feature_set = getenv("CT_KCOV_FEATURE_SET");
  ct_kcov_feature_set = feature_set && *feature_set && strcmp(feature_set, "0");
  ct_kcov_spool = tmpfile();
  if (!ct_kcov_spool) {
    dprintf(2, "CT_KCOV_ERROR reason=spool_create_failed errno=%d\n", errno);
    exit(91);
  }
  ct_kcov_fd = open("/sys/kernel/debug/kcov", O_RDWR | O_CLOEXEC);
  if (ct_kcov_fd < 0) { perror("kcov open"); exit(91); }
  if (ioctl(ct_kcov_fd, KCOV_INIT_TRACE, entries) < 0) { perror("kcov init"); exit(91); }
  ct_kcov_area = mmap(NULL, entries * sizeof(uint64_t),
                      PROT_READ | PROT_WRITE, MAP_SHARED, ct_kcov_fd, 0);
  if (ct_kcov_area == MAP_FAILED) { perror("kcov mmap"); exit(91); }
  atexit(ct_kcov_finish);
  ct_wait_worker_barrier();
  ct_phase("END", "runtime_init");
}

static long ct_call(unsigned index, const char *name, long nr,
                    long a, long b, long c, long d, long e, long f)
{
  /* Keep marker setup and coverage-file I/O out of KCOV. Only the generated
   * syscall itself is sampled; its PCs are appended to the program sample. */
  ct_phase("BEGIN", "runtime_adaptation");
  ct_kcov_stop();
  long original_fd = a;
  long original_length = d;
  int length_adapted = 0;
  /* Do not let a generated close invalidate the private fallback descriptors. */
  if (!strcmp(name, "close") && ct_reserved_fd((int)a)) a = -1;
  int fd_repaired = 0;
  a = ct_repair_fd(name, a, &fd_repaired);
  if (!strcmp(name, "fallocate"))
    d = ct_bound_fallocate_length(d, &length_adapted);
  if (length_adapted) ct_fallocate_adaptations++;
  unsigned long long object_ino, object_dev;
  int object_is_dir;
  ct_inode_identity(name, a, &object_ino, &object_dev, &object_is_dir);
  long args[] = {a,b,c,d,e,f};
  int fd = open("/sys/kernel/tracing/trace_marker", O_WRONLY | O_CLOEXEC);
  if (fd < 0) { perror("trace_marker"); exit(90); }
  /* A generated numeric fd must not close/write the marker. This high fd is
   * opened before BEGIN and closed after END, avoiding false native pairs for
   * generated openat/close. It is never a syzkaller resource result. */
  /* Guest limits the process to 256 descriptors. Keep the marker in the
   * reserved upper range, away from normal syzkaller resource results. */
  int reserved = 200, marker;
  for (;;) {
    marker = fcntl(fd, F_DUPFD_CLOEXEC, reserved);
    if (marker < 0) { perror("marker duplicate"); exit(90); }
    int conflict = 0;
    /* Linux fd arguments are truncated to int, including fuzzed high bits. */
    for (unsigned i=0; i<6; i++) if ((int)args[i] == marker) conflict = 1;
    if (!conflict) break;
    reserved = marker+1;
    close(marker);
  }
  close(fd);
  if (marker < 0) { perror("marker duplicate"); exit(90); }
  ct_phase("END", "runtime_adaptation");
  const char *prefix = getenv("CT_CALL_PREFIX");
  const char *program = getenv("CT_PROGRAM_ID");
  if (!prefix || !program) exit(90);
  char line[512];
  int size = snprintf(line, sizeof(line),
    "CT_SYSCALL phase=BEGIN id=%s-%ld-%u nr=%ld name=%s ret=0 errno=0 repaired=%d adapted=%d program=%s call_index=%u original_fd=%ld executed_fd=%ld original_length=%ld executed_length=%ld object_ino=%llu object_dev=%llu object_is_dir=%d repair_version=2\n",
    prefix, (long)getpid(), index+1, nr, name, fd_repaired, length_adapted,
    program, index, original_fd, a, original_length, d, object_ino, object_dev, object_is_dir);
  if (size < 0 || size >= (int)sizeof(line) || write(marker,line,size) != size) exit(90);
  ct_kcov_start(index);
  errno = 0;
  long ret = syscall(nr,a,b,c,d,e,f);
  int error = ret == -1 ? errno : 0;
  ct_kcov_stop();
  size = snprintf(line, sizeof(line),
    "CT_SYSCALL phase=END id=%s-%ld-%u nr=%ld name=%s ret=%ld errno=%d repaired=%d adapted=%d program=%s call_index=%u original_fd=%ld executed_fd=%ld original_length=%ld executed_length=%ld object_ino=%llu object_dev=%llu object_is_dir=%d repair_version=2\n",
    prefix, (long)getpid(), index+1, nr, name, ret, error, fd_repaired, length_adapted,
    program, index, original_fd, a, original_length, d, object_ino, object_dev, object_is_dir);
  if (size < 0 || size >= (int)sizeof(line) || write(marker,line,size) != size) exit(90);
  close(marker);
  errno = error;
  const char *stop_setting = getenv("CT_FAULT_STOP_AFTER_HIT");
  const char *status_path = getenv("CT_FAULT_STATUS_PATH");
  if (stop_setting && !strcmp(stop_setting, "1") && status_path) {
    int status_fd = open(status_path, O_RDONLY | O_CLOEXEC);
    if (status_fd >= 0) {
      char status[2048];
      ssize_t count = read(status_fd, status, sizeof(status) - 1);
      close(status_fd);
      if (count > 0) {
        status[count] = '\0';
        char *injected = strstr(status, "\"injected\":");
        char *target_pid = strstr(status, "\"target_pid\":");
        char *errno_operation = strstr(status, "\"errno_operation\":\"");
        long hit_count = injected ? strtol(injected + strlen("\"injected\":"), NULL, 10) : 0;
        long target = target_pid ? strtol(target_pid + strlen("\"target_pid\":"), NULL, 10) : 0;
        if (hit_count > 0 && target == (long)getpid() && errno_operation) {
          const char *program_id = getenv("CT_PROGRAM_ID");
          const char *stop_marker = getenv("CT_FAULT_STOP_MARKER");
          int trace_fd = open("/sys/kernel/tracing/trace_marker", O_WRONLY | O_CLOEXEC);
          if (trace_fd >= 0) {
            char stop_line[256];
            const char *operation_start = errno_operation + strlen("\"errno_operation\":\"");
            const char *quote = strchr(operation_start, '"');
            if (quote) {
              size_t operation_size = (size_t)(quote -
                  operation_start);
              if (operation_size < sizeof(stop_line)) {
                char operation[96];
                if (operation_size < sizeof(operation)) {
                  memcpy(operation, errno_operation + strlen("\"errno_operation\":\""), operation_size);
                  operation[operation_size] = '\0';
                  int stop_size = snprintf(stop_line, sizeof(stop_line),
                      "CT_FAULT_STOP_AFTER_CALL program=%s call_index=%u operation=%s\n",
                      program_id ? program_id : "unknown", index, operation);
                  if (stop_size > 0 && stop_size < (int)sizeof(stop_line))
                    (void)write(trace_fd, stop_line, (size_t)stop_size);
                  if (stop_marker) {
                    int stop_fd = open(stop_marker, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
                    if (stop_fd >= 0) {
                      (void)dprintf(stop_fd, "program=%s\ncall_index=%u\noperation=%s\n",
                                    program_id ? program_id : "unknown", index, operation);
                      close(stop_fd);
                    }
                  }
                  close(trace_fd);
                  exit(0);
                }
              }
            }
            close(trace_fd);
          }
        }
      }
    }
  }
  return ret;
}
