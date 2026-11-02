/* Supervise separate syzkaller programs; release workers after native filters. */
#define _GNU_SOURCE
#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static void die(const char *what) { perror(what); exit(90); }


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

static int remove_tree(const char *path)
{
  struct stat st;
  if (lstat(path, &st) < 0) {
    return errno == ENOENT ? 0 : -1;
  }
  /* Generated symlink paths may point at directories. Never follow them
   * while removing the private worker root. */
  if (!S_ISDIR(st.st_mode)) {
    return (unlink(path) < 0 && errno != ENOENT) ? -1 : 0;
  }
  DIR *dir = opendir(path);
  if (!dir) {
    return errno == ENOENT ? 0 : -1;
  }
  int failed = 0;
  struct dirent *entry;
  while ((entry = readdir(dir)) != NULL) {
    if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
    char child[PATH_MAX];
    if (snprintf(child, sizeof(child), "%s/%s", path, entry->d_name) >= (int)sizeof(child)) {
      /* Keep cleanup's phase balanced even for a malformed/overlong entry. */
      closedir(dir);
      return -1;
    }
    if (remove_tree(child) < 0)
      failed = 1;
  }
  closedir(dir);
  if (rmdir(path) < 0 && errno != ENOENT)
    failed = 1;
  return failed ? -1 : 0;
}

static void make_dir(const char *path)
{
  if (mkdir(path, 0700) < 0 && errno != EEXIST) die("mkdir workload tree");
}

static void make_file(const char *path)
{
  int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  if (fd < 0) die("seed workload file");
  static const char seed[] = "counter-trace-seed\n";
  if (write(fd, seed, sizeof(seed) - 1) != (ssize_t)(sizeof(seed) - 1)) die("seed workload write");
  if (close(fd) < 0) die("seed workload close");
}

static void prepare_root(const char *out)
{
  make_dir(out);
  /* Keep file0 as a directory because syzkaller commonly uses it as the
   * first openat$dir target. Its nested file0 is a writable regular file for
   * later plain openat/data calls. */
  for (int n = 0; n < 4; n++) {
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/file%d", out, n) >= (int)sizeof(path)) exit(90);
    make_dir(path);
    if (n >= 2) continue; /* file2/file3 are deletable empty directories. */
    char data[PATH_MAX];
    if (snprintf(data, sizeof(data), "%s/%s", path, n == 0 ? "file0" : "data") >= (int)sizeof(data)) exit(90);
    make_file(data);
  }
  char dir[PATH_MAX];
  if (snprintf(dir, sizeof(dir), "%s/dir0", out) >= (int)sizeof(dir)) exit(90);
  make_dir(dir);
  char nested[PATH_MAX];
  if (snprintf(nested, sizeof(nested), "%s/file0", dir) >= (int)sizeof(nested)) exit(90);
  make_file(nested);
  const char *dirs[] = {"empty0", "srcdir", "dstdir"};
  for (unsigned i = 0; i < sizeof(dirs)/sizeof(dirs[0]); i++) {
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/%s", out, dirs[i]) >= (int)sizeof(path)) exit(90);
    make_dir(path);
  }
  const char *files[] = {"source", "replacement", "exchange0", "exchange1"};
  for (unsigned i = 0; i < sizeof(files)/sizeof(files[0]); i++) {
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/%s", out, files[i]) >= (int)sizeof(path)) exit(90);
    make_file(path);
  }
}

static void prepare_worker_root(const char *root, int worker, pid_t pid, char *out)
{
  if (snprintf(out, PATH_MAX, "%s/w%d-%ld", root, worker, (long)pid) >= PATH_MAX) exit(90);
  prepare_root(out);
}

static void write_bytes(int fd, const char *bytes, size_t length)
{
  while (length) {
    ssize_t written = write(fd, bytes, length);
    if (written < 0 && errno == EINTR) continue;
    if (written <= 0) die("write worker gate");
    bytes += written;
    length -= (size_t)written;
  }
}

static void write_text_file(const char *path, const char *text)
{
  int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  if (fd < 0) die("open fault evidence");
  size_t length = strlen(text);
  while (length) {
    ssize_t written = write(fd, text, length);
    if (written < 0 && errno == EINTR) continue;
    if (written <= 0) die("write fault evidence");
    text += written;
    length -= (size_t)written;
  }
  if (close(fd) < 0) die("close fault evidence");
}

static void capture_fault_status(const char *source, const char *destination)
{
  int input = open(source, O_RDONLY | O_CLOEXEC);
  if (input < 0) die("open fault status");
  int output = open(destination, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  if (output < 0) die("create fault status evidence");
  char buffer[1024];
  ssize_t count;
  while ((count = read(input, buffer, sizeof(buffer))) != 0) {
    if (count < 0) {
      if (errno == EINTR) continue;
      die("read fault status");
    }
    write_bytes(output, buffer, (size_t)count);
  }
  if (close(input) < 0 || close(output) < 0) die("close fault status evidence");
}

static void arm_runtime_fault(int shared)
{
  const char *config = getenv("CT_FAULT_CONFIG");
  if (!config || !*config) return;
  const char *inject_path = getenv("CT_FAULT_PATH");
  const char *status_path = getenv("CT_FAULT_STATUS_PATH");
  const char *arm_path = getenv("CT_FAULT_ARM_EVIDENCE");
  const char *policy_path = getenv("CT_FAULT_POLICY_EVIDENCE");
  if (!inject_path || !status_path || !arm_path || !policy_path)
    die("incomplete fault injection configuration");

  long page_size = sysconf(_SC_PAGESIZE);
  /* A private worker arms after seed setup, targeting only its own PID.
   * Shared workers need one parent arm targeting their inherited process
   * group, so concurrent children cannot race the mount-level controller. */
  char target[80];
  if (shared)
    snprintf(target, sizeof(target), "pid=0 pgid=%ld", (long)getpgrp());
  else
    snprintf(target, sizeof(target), "pid=%ld", (long)getpid());
  int length = snprintf(NULL, 0, "%s %s\n", config, target);
  if (page_size <= 0 || length < 0 || (long)length >= page_size)
    die("fault injection configuration too long");
  char *actual_config = malloc((size_t)length + 1);
  if (!actual_config) die("allocate fault injection configuration");
  if (snprintf(actual_config, (size_t)length + 1, "%s %s\n",
               config, target) != length) {
    free(actual_config);
    die("format fault injection configuration");
  }
  int fd = open(inject_path, O_WRONLY | O_CLOEXEC);
  if (fd < 0) {
    free(actual_config);
    die("open fault injection control");
  }
  if (write(fd, actual_config, (size_t)length) != length) {
    free(actual_config);
    die("arm fault injection");
  }
  if (close(fd) < 0) {
    free(actual_config);
    die("close fault injection control");
  }
  write_text_file(policy_path, actual_config);
  capture_fault_status(status_path, arm_path);
  free(actual_config);
}

static void read_bytes(int fd, char *bytes, size_t length)
{
  while (length) {
    ssize_t count = read(fd, bytes, length);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) die("read worker gate");
    bytes += count;
    length -= (size_t)count;
  }
}

static void progress(const char *phase, const char *program, pid_t pid, int code, int signal_number)
{
  const char *fd_text=getenv("CT_PROGRESS_FD");
  if (!fd_text) return;
  struct timespec now;
  if (clock_gettime(CLOCK_REALTIME,&now)) die("progress clock");
  const char *segment=getenv("CT_CALL_PREFIX");
  char line[512];
  int n=snprintf(line,sizeof(line),
      "CT_PROGRESS phase=WORKLOAD_%s segment_batch=%s program=%s pid=%ld wall_ns=%lld",
      phase,segment?segment:"unknown",program,(long)pid,
      (long long)now.tv_sec*1000000000LL+now.tv_nsec);
  if (n<0 || n>=(int)sizeof(line)) exit(90);
  if (!strcmp(phase,"END")) {
    int extra=snprintf(line+n,sizeof(line)-(size_t)n," exit=%d signal=%d",code,signal_number);
    if (extra<0 || extra>=(int)(sizeof(line)-(size_t)n)) exit(90);
    n+=extra;
  }
  if (n+1>=(int)sizeof(line)) exit(90);
  line[n++]='\n';
  if (n<0 || n>=(int)sizeof(line) || write(atoi(fd_text),line,n)!=n) die("progress write");
}
static void filter(pid_t *pids, int workers)
{
  char value[512]; int n=0;
  for (int i=0; i<workers; i++)
    n += snprintf(value+n,sizeof(value)-(size_t)n,"%scommon_pid == %ld",i?" || ":"",(long)pids[i]);
  if (n < 0 || n >= (int)sizeof(value)) exit(90);
  const char *files[] = {"/sys/kernel/tracing/events/raw_syscalls/sys_exit/filter",
                         "/sys/kernel/tracing/events/raw_syscalls/sys_enter/filter"};
  for (int i=0; i<2; i++) {
    int fd=open(files[i],O_WRONLY|O_CLOEXEC);
    if (fd<0 || write(fd,value,n)!=n) die("native filter");
    close(fd);
  }
}
int main(int argc, char **argv)
{
  if (argc==3 && !strcmp(argv[1], "--cleanup")) {
    char shared_root[PATH_MAX];
    if (snprintf(shared_root, sizeof(shared_root), "%s/syz-work/shared", argv[2]) >=
        (int)sizeof(shared_root)) return 2;
    ct_phase("BEGIN", "cleanup");
    if (remove_tree(shared_root) < 0) {
      ct_phase("END", "cleanup");
      return 90;
    }
    ct_phase("END", "cleanup");
    return 0;
  }
  if (argc!=7) return 2;
  const char *root=argv[1], *programs=argv[2], *batch=argv[3];
  int workers=atoi(argv[4]), memory=atoi(argv[5]);
  int shared=!strcmp(argv[6], "shared");
  if (workers<1 || workers>8 || memory<0 || memory>256 ||
      (!shared && strcmp(argv[6], "private")) || (shared && workers<2)) return 2;
  char work[PATH_MAX];
  if (snprintf(work,sizeof(work),"%s/syz-work",root)>=(int)sizeof(work)) return 2;
  if (mkdir(work,0700) && errno!=EEXIST) die("work directory");
  if (setenv("CT_ROOT",work,1)) die("CT_ROOT");
  void *pressure=malloc((size_t)memory*1024*1024);
  if (memory && !pressure) die("memory pressure");
  for (size_t off=0; off<(size_t)memory*1024*1024; off+=4096)
    ((volatile unsigned char *)pressure)[off]=0x3c;
  char shared_root[PATH_MAX];
  if (shared) {
    if (snprintf(shared_root, sizeof(shared_root), "%s/shared", work) >= (int)sizeof(shared_root)) return 2;
    ct_phase("BEGIN", "cleanup");
    if (remove_tree(shared_root) < 0) {
      ct_phase("END", "cleanup");
      return 90;
    }
    ct_phase("END", "cleanup");
    ct_phase("BEGIN", "seed_setup");
    prepare_root(shared_root);
    ct_phase("END", "seed_setup");
  }
  int start_gate[2], ready_gate[2], run_gate[2];
  if (pipe(start_gate)) die("start gate pipe");
  if (shared && (pipe(ready_gate) || pipe(run_gate))) die("worker barrier pipe");
  pid_t pids[8];
  for (int i=0; i<workers; i++) {
    pids[i]=fork(); if (pids[i]<0) die("fork");
    if (!pids[i]) {
      close(start_gate[1]);
      if (shared) {
        close(ready_gate[0]);
        close(run_gate[1]);
      }
      char ready;
      if (read(start_gate[0],&ready,1)!=1) _exit(90);
      close(start_gate[0]);
      int null=open("/dev/null",O_RDONLY);
      if (null<0 || dup2(null,0)<0) _exit(90);
      if (null) close(null);
      char worker_root[PATH_MAX];
      if (shared) {
        if (snprintf(worker_root, sizeof(worker_root), "%s", shared_root) >= (int)sizeof(worker_root)) _exit(90);
        char ready_fd[32], run_fd[32];
        snprintf(ready_fd, sizeof(ready_fd), "%d", ready_gate[1]);
        snprintf(run_fd, sizeof(run_fd), "%d", run_gate[0]);
        if (setenv("CT_READY_FD", ready_fd, 1) || setenv("CT_RUN_FD", run_fd, 1)) _exit(90);
      } else {
        ct_phase("BEGIN", "seed_setup");
        prepare_worker_root(root, i, getpid(), worker_root);
        ct_phase("END", "seed_setup");
      }
      if (setenv("CT_ROOT", worker_root, 1)) _exit(90);
      char binary[PATH_MAX], ident[80];
      if (snprintf(ident,sizeof(ident),"b%s-w%d",batch,i)>=(int)sizeof(ident) ||
          snprintf(binary,sizeof(binary),"%s/%s/workload",programs,ident)>=(int)sizeof(binary)) _exit(90);
      if (setenv("CT_PROGRAM_ID",ident,1)) _exit(90);
      if (!shared) arm_runtime_fault(0);
      progress("START",ident,getpid(),-1,0);
      /* The generated program should inherit its usual descriptor layout. */
      const char *progress_fd=getenv("CT_PROGRESS_FD");
      if (progress_fd && atoi(progress_fd)>=3) close(atoi(progress_fd));
      execl(binary,binary,(char *)NULL); perror("exec program"); _exit(90);
    }
  }
  close(start_gate[0]);
  if (shared) {
    close(ready_gate[1]);
    close(run_gate[0]);
  }
  filter(pids,workers);
  if (shared) {
    /* Shared workers are held at start_gate. Arm the mount only once; private
     * workers instead arm their own PID after preparing their seed tree. */
    pid_t worker_pgid = getpgrp();
    for (int i=0; i<workers; i++)
      if (getpgid(pids[i]) != worker_pgid) {
        errno = ESRCH;
        die("worker process group");
      }
    arm_runtime_fault(1);
  }
  for (int i=0; i<workers; i++) write_bytes(start_gate[1], "x", 1);
  close(start_gate[1]);
  if (shared) {
    char ready[8], release[8];
    read_bytes(ready_gate[0], ready, (size_t)workers);
    close(ready_gate[0]);
    memset(release, 'x', (size_t)workers);
    write_bytes(run_gate[1], release, (size_t)workers);
    close(run_gate[1]);
    printf("CT_CONTENTION mode=shared workers=%d initialized_before_release=1 objects=shared-root,shared-source,shared-directory\n",
           workers);
    fflush(stdout);
  }
  int failed=0;
  for (int done=0; done<workers; done++) {
    int status;
    pid_t pid=waitpid(-1,&status,0);
    if (pid<0) die("wait worker");
    int worker=0;
    while (worker<workers && pids[worker]!=pid) worker++;
    char ident[80];
    if (worker==workers || snprintf(ident,sizeof(ident),"b%s-w%d",batch,worker)>=(int)sizeof(ident)) exit(90);
    int code=WIFEXITED(status)?WEXITSTATUS(status):-1;
    int signal_number=WIFSIGNALED(status)?WTERMSIG(status):0;
    progress("END",ident,pid,code,signal_number);
    if (!shared) {
      char worker_root[PATH_MAX];
      if (snprintf(worker_root, sizeof(worker_root), "%s/w%d-%ld", root, worker, (long)pid) >= (int)sizeof(worker_root)) exit(90);
      ct_phase("BEGIN", "cleanup");
      if (remove_tree(worker_root) < 0)
        failed=1;
      ct_phase("END", "cleanup");
    }
    if (code!=0 || signal_number) failed=1;
  }
  free(pressure);
  return failed ? 90 : 0;
}
