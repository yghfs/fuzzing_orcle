// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

#define FILL_CHUNK (64 * 1024)
#define VALUE_SIZE 1200

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static int fill_to_enospc(int fd)
{
	off_t offset = 0;

	for (;;) {
		if (!fallocate(fd, 0, offset, FILL_CHUNK)) {
			offset += FILL_CHUNK;
			continue;
		}
		if (errno == ENOSPC)
			return 0;
		return -1;
	}
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/xattr-cow";
	char a[512], b[512], filler_path[512];
	char shared[VALUE_SIZE], replacement[VALUE_SIZE];
	int fa = -1, fb = -1, filler = -1, failed_updates = 0, succeeded_updates = 0;
	int rc = 1;

	memset(shared, 'S', sizeof(shared));
	memset(replacement, 'R', sizeof(replacement));
	if (mkdir(dir, 0700) && errno != EEXIST)
		die("mkdir workload directory");
	if (snprintf(a, sizeof(a), "%s/a", dir) >= (int)sizeof(a) ||
	    snprintf(b, sizeof(b), "%s/b", dir) >= (int)sizeof(b) ||
	    snprintf(filler_path, sizeof(filler_path), "%s/filler", dir) >=
	    (int)sizeof(filler_path)) {
		errno = ENAMETOOLONG;
		die("format workload paths");
	}
	fa = open(a, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	fb = open(b, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fa < 0 || fb < 0)
		die("create xattr owners");
	if (fsetxattr(fa, "user.shared.a", shared, sizeof(shared), XATTR_CREATE) ||
	    fsetxattr(fa, "user.shared.b", shared, sizeof(shared), XATTR_CREATE) ||
	    fsetxattr(fb, "user.shared.a", shared, sizeof(shared), XATTR_CREATE) ||
	    fsetxattr(fb, "user.shared.b", shared, sizeof(shared), XATTR_CREATE) ||
	    fsync(fa) || fsync(fb))
		die("seed shared xattrs");

	filler = open(filler_path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (filler < 0 || fill_to_enospc(filler))
		die("fill filesystem");
	if (fsync(filler) && errno != ENOSPC)
		die("fsync full filesystem");

	for (int i = 0; i < 32; i++) {
		int set_rc;

		errno = 0;
		set_rc = fsetxattr(fa, "user.shared.a", replacement,
				   sizeof(replacement), XATTR_REPLACE);
		if (!set_rc) {
			succeeded_updates++;
			break;
		}
		if (errno != ENOSPC)
			die("set COW xattr");
		failed_updates++;
		if (fgetxattr(fa, "user.shared.a", replacement,
			      sizeof(replacement)) != sizeof(shared) ||
		    memcmp(replacement, shared, sizeof(shared))) {
			errno = EIO;
			die("verify failed COW preserved old xattr");
		}
	}

	if (!failed_updates) {
		fprintf(stderr, "no natural ENOSPC on shared-xattr COW\n");
		goto out;
	}
	if (close(filler))
		die("close filler");
	filler = -1;
	if (unlink(filler_path) || fsync(fa) || fsync(fb))
		die("release full-space filler");
	if (fsetxattr(fa, "user.shared.a", replacement,
		      sizeof(replacement), XATTR_REPLACE) || fsync(fa))
		die("retry COW after freeing space");
	if (fremovexattr(fa, "user.shared.a") ||
	    fremovexattr(fa, "user.shared.b") ||
	    fremovexattr(fb, "user.shared.a") ||
	    fremovexattr(fb, "user.shared.b") || fsync(fa) || fsync(fb))
		die("remove shared xattrs");
	if (close(fa) || close(fb))
		die("close xattr owners");
	fa = fb = -1;
	if (unlink(a) || unlink(b) || rmdir(dir))
		die("remove workload files");
	rc = 0;
	printf("xattr_cow_natural complete enospc=%d success=%d\n",
	       failed_updates, succeeded_updates);
out:
	if (filler >= 0)
		close(filler);
	if (fa >= 0)
		close(fa);
	if (fb >= 0)
		close(fb);
	return rc;
}
