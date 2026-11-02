// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *mnt = argc > 1 ? argv[1] : "/mnt/ext4-xattr-shutdown";
	char a[256], b[256];
	const char value[] = "shared-xattr-value-0123456789";
	unsigned int mode = EXT4_GOING_FLAGS_DEFAULT;
	int fda, fdb, mfd, rc, saved;

	if (mkdir(mnt, 0700) && errno != EEXIST)
		return 1;
	snprintf(a, sizeof(a), "%s/a", mnt);
	snprintf(b, sizeof(b), "%s/b", mnt);
	fda = open(a, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	fdb = open(b, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	if (fda < 0 || fdb < 0)
		return 1;
	if (fsetxattr(fda, "user.shared", value, sizeof(value), 0) ||
	    fsetxattr(fdb, "user.shared", value, sizeof(value), 0) ||
	    fsync(fda) || fsync(fdb))
		return 1;
	if (unlink(a))
		return 1;
	if (syncfs(fda))
		return 1;
	mfd = open(mnt, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (mfd < 0)
		return 1;
	rc = ioctl(mfd, EXT4_IOC_SHUTDOWN, &mode);
	saved = errno;
	printf("shutdown_rc=%d shutdown_errno=%d (%s)\n", rc, rc ? saved : 0,
	       rc ? strerror(saved) : "ok");
	close(mfd);
	close(fda);
	rc = close(fdb);
	saved = errno;
	printf("close_rc=%d close_errno=%d (%s)\n", rc, rc ? saved : 0,
	       rc ? strerror(saved) : "ok");
	return 0;
}
