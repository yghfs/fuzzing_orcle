/* Program session glue inserted into upstream syzkaller's sequential C export. */
#include <sys/sysmacros.h>
static int efi_fd = -1;
static unsigned long long efi_run;

static unsigned long long efi_seed_number(const char *name)
{
  const char *text = getenv(name);
  char *end = NULL;
  errno = 0;
  unsigned long long value = text ? strtoull(text, &end, 10) : 0;
  if (!text || !*text || *text == '-' || !end || *end || errno) {
    fprintf(stderr, "invalid %s\n", name); exit(91);
  }
  return value;
}

static int efi_open_stable_file(void)
{
  const char *reference = getenv("EFI_SEED_REFERENCE");
  if (!reference || !*reference) {
    fprintf(stderr, "missing EFI_SEED_REFERENCE\n"); exit(91);
  }
  unsigned long long device = efi_seed_number("EFI_SEED_DEVICE");
  unsigned long long inode = efi_seed_number("EFI_SEED_INODE");
  int fd = open(reference, O_RDWR | O_CLOEXEC);
  if (fd < 0) { perror("stable inode reference"); exit(91); }
  struct stat data, root;
  if (fstat(fd, &data) || stat(".", &root) || !S_ISREG(data.st_mode) ||
      data.st_dev != root.st_dev || (unsigned long long)data.st_dev != device ||
      (unsigned long long)data.st_ino != inode) {
    fprintf(stderr, "stable inode identity mismatch\n"); close(fd); exit(91);
  }
  return fd;
}

static void efi_command(const char *command)
{
  size_t length = strlen(command);
  if (write(efi_fd, command, length) != (ssize_t)length) {
    perror(command); exit(92);
  }
}

static void efi_finish(void)
{
  if (efi_fd < 0) return;
  efi_command("end\n");
  const char *path = getenv("EFI_RESULT");
  FILE *out = path ? fopen(path, "w") : NULL;
  if (!out) { perror("EFI_RESULT"); _exit(92); }
  char buffer[2048];
  ssize_t n;
  while ((n = read(efi_fd, buffer, sizeof(buffer))) > 0) {
    if (fwrite(buffer, 1, (size_t)n, out) != (size_t)n) _exit(92);
  }
  if (n < 0 || fclose(out)) _exit(92);
  close(efi_fd); efi_fd = -1;
}

static void efi_init(void)
{
  const char *run = getenv("EFI_RUN_ID");
  char *end = NULL;
  struct stat st;
  if (!run || !(efi_run = strtoull(run, &end, 10)) || *end || stat(".", &st)) exit(92);
  int fd = open("/sys/kernel/debug/ext4_efi/session", O_RDWR | O_CLOEXEC);
  if (fd < 0) { perror("EFI session"); exit(92); }
  efi_fd = fcntl(fd, F_DUPFD_CLOEXEC, 248);
  close(fd);
  if (efi_fd != 248) { perror("EFI reserved descriptor"); exit(92); }
  const char *events[] = {"sys_enter", "sys_exit"};
  for (unsigned i = 0; i < 2; i++) {
    char filter_path[180], filter[80];
    snprintf(filter_path, sizeof(filter_path), "/sys/kernel/tracing/events/raw_syscalls/%s/filter", events[i]);
    int filter_fd = open(filter_path, O_WRONLY | O_CLOEXEC);
    int count = snprintf(filter, sizeof(filter), "common_pid == %ld", (long)getpid());
    if (filter_fd < 0 || write(filter_fd, filter, count) != count || close(filter_fd)) exit(92);
  }
  const char *path = getenv("EFI_PLAN");
  if (path && *path) {
    FILE *plan = fopen(path, "r");
    if (!plan) { perror("EFI_PLAN"); exit(92); }
    char line[160];
    while (fgets(line, sizeof(line), plan)) efi_command(line);
    if (ferror(plan) || fclose(plan)) exit(92);
  }
  char command[160];
  snprintf(command, sizeof(command), "begin %u %u %llu\n", major(st.st_dev), minor(st.st_dev), efi_run);
  efi_command(command);
  if (atexit(efi_finish)) exit(92);
}

static void efi_call_begin(unsigned index, long nr, unsigned long long device)
{
  char command[160];
  snprintf(command, sizeof(command), "call %u %u %u %ld\n", index,
           major((dev_t)device), minor((dev_t)device), nr);
  efi_command(command);
}
static void efi_call_end(void) { efi_command("leave\n"); }
