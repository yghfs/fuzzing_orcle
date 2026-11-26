/* Keep one input's data inode referenced throughout all of its replays. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv)
{
  if (argc != 3) {
    fprintf(stderr, "usage: %s SEED_FILE READY_JSON\n", argv[0]);
    return 2;
  }
  sigset_t signals;
  sigemptyset(&signals);
  sigaddset(&signals, SIGTERM);
  sigaddset(&signals, SIGINT);
  if (sigprocmask(SIG_BLOCK, &signals, NULL)) { perror("signal mask"); return 1; }

  int source = open(argv[1], O_RDWR | O_CLOEXEC);
  if (source < 0) { perror("seed reference open"); return 1; }
  if (dup2(source, 9) != 9) { perror("seed reference duplicate"); return 1; }
  if (source != 9) close(source);
  struct stat st;
  if (fstat(9, &st) || !S_ISREG(st.st_mode)) {
    fprintf(stderr, "seed reference must be a regular file\n");
    return 1;
  }

  char temporary[PATH_MAX];
  int length = snprintf(temporary, sizeof(temporary), "%s.tmp", argv[2]);
  if (length < 0 || length >= (int)sizeof(temporary)) return 1;
  FILE *ready = fopen(temporary, "wx");
  if (!ready) { perror("seed ready file"); return 1; }
  int written = fprintf(ready,
      "{\"pid\":%ld,\"fd\":9,\"dev\":%ju,\"ino\":%ju,"
      "\"reference\":\"/proc/%ld/fd/9\"}\n",
      (long)getpid(), (uintmax_t)st.st_dev, (uintmax_t)st.st_ino, (long)getpid());
  int closed = fclose(ready);
  if (written < 0 || closed || rename(temporary, argv[2])) {
    perror("seed ready publish");
    unlink(temporary);
    return 1;
  }

  int received;
  int error = sigwait(&signals, &received);
  close(9);
  unlink(argv[2]);
  if (error) { errno = error; perror("seed reference wait"); return 1; }
  return 0;
}
