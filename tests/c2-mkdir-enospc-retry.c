#define _GNU_SOURCE
/* candidate 2: ext4_mkdir() ext4_inc_count(dir) + ENOSPC retry loop.
 * Fill the fs, then mkdir repeatedly; -ENOSPC from ext4_mark_inode_dirty(dir)
 * after ext4_inc_count() drives the out_clear_inode -> out_retry -> retry
#include <fcntl.h>
 * path at namei.c:3040/3056/3058. */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <sys/syscall.h>
#include <unistd.h>

static int fill(const char *dir, char name[256], unsigned long i)
{
	char path[512];
	snprintf(path, sizeof(path), "%s/f%lu", dir, i);
	int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0600);
	if (fd < 0)
		return -1;
	/* write big chunks until ENOSPC */
	static char buf[1 << 20];
	memset(buf, 0x41, sizeof(buf));
	while (write(fd, buf, sizeof(buf)) > 0)
		;
	close(fd);
	return 0;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/c2";
	char name[256];
	unsigned long i = 0;
	/* keep writing new files until even a small write fails with ENOSPC */
	for (;; i++) {
		snprintf(name, sizeof(name), "f%lu", i);
		char path[512];
		snprintf(path, sizeof(path), "%s/%s", dir, name);
		int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0600);
		if (fd < 0)
			break;
		char b[4096];
		if (write(fd, b, sizeof(b)) < 0 && errno == ENOSPC) {
			close(fd);
			unlink(path);
			break;
		}
		close(fd);
		if (i > 200000)
			break;
	}
	/* reserve a little so mkdir's transaction is credit-feasible but
	 * ext4_mark_inode_dirty still fails at ENOSPC when journal runs dry:
	 * mkdir in a loop; each retry re-executes ext4_inc_count(dir). */
	int enospc = 0, ok = 0;
	for (int r = 0; r < 200; r++) {
		char p[512];
		snprintf(p, sizeof(p), "%s/d%d", dir, r);
		if (mkdir(p, 0700) == 0) {
			ok++;
			rmdir(p);
		} else {
			enospc++;
			if (errno != ENOSPC)
				break;
		}
	}
	printf("fill_files=%lu mkdir_ok=%d mkdir_fail=%d\n", i, ok, enospc);
	return 0;
}
