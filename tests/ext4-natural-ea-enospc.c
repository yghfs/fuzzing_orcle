// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/xattr.h>
#include <sys/stat.h>
#include <unistd.h>

/* Seed a shared EA inode before filling the data device, then exercise only
 * reference updates while the filesystem is naturally out of space. */
int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/tmp/ea-enospc";
	char a[256], b[256], filler[256];
	char value[5000], buf[1 << 20];
	int fa, fb, ff, saved, err = 0;
	unsigned long long off = 0;

	if (mkdir(dir, 0700) && errno != EEXIST) return 1;
	snprintf(a, sizeof(a), "%s/a", dir);
	snprintf(b, sizeof(b), "%s/b", dir);
	snprintf(filler, sizeof(filler), "%s/filler", dir);
	fa = open(a, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	fb = open(b, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	if (fa < 0 || fb < 0) return 2;
	memset(value, 'E', sizeof(value));
	if (fsetxattr(fa, "user.shared", value, sizeof(value), 0) ||
	    fsetxattr(fb, "user.shared", value, sizeof(value), 0)) return 3;
	if (fsync(fa) || fsync(fb)) return 4;
	ff = open(filler, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	if (ff < 0) return 5;
	memset(buf, 0x5a, sizeof(buf));
	for (;;) {
		if (pwrite(ff, buf, sizeof(buf), (off_t)off) != (ssize_t)sizeof(buf)) {
			err = errno ?: EIO;
			break;
		}
		off += sizeof(buf);
	}
	printf("fill_bytes=%llu fill_errno=%d (%s)\n", off, err, strerror(err));
	/* These are pure EA-inode ref decrements; no new EA value is allocated. */
	saved = fremovexattr(fa, "user.shared");
	printf("remove_a=%d errno=%d (%s)\n", saved, saved ? errno : 0,
	       saved ? strerror(errno) : "ok");
	saved = fremovexattr(fb, "user.shared");
	printf("remove_b=%d errno=%d (%s)\n", saved, saved ? errno : 0,
	       saved ? strerror(errno) : "ok");
	fsync(fa); fsync(fb); sync();
	close(ff); close(fa); close(fb);
	return 0;
}
