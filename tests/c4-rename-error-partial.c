#define _GNU_SOURCE
/* candidate 4: ext4_rename() error exit after new.inode nlink was dropped.
 * Overwrite-rename a file over another while the fs is at ENOSPC so
 * ext4_mark_inode_dirty(handle, old.dir) fails after ext4_dec_count(new.inode)
 * already ran (namei.c:4166/4174 area). We check: does target still exist,
 * what is nlink of surviving inode. */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/c4";
	char a[512], b[512], f[512];
	snprintf(a, sizeof(a), "%s/old", dir);
	snprintf(b, sizeof(b), "%s/new", dir);
	snprintf(f, sizeof(f), "%s/filler", dir);

	/* fill until ENOSPC */
	int fd = open(f, O_CREAT | O_WRONLY | O_TRUNC, 0600);
	char buf[65536];
	memset(buf, 0x42, sizeof(buf));
	while (write(fd, buf, sizeof(buf)) > 0)
		;
	close(fd);

	creat(a, 0600);
	creat(b, 0600);
	/* stat before */
	struct stat sa, sb;
	stat(b, &sb);
	unsigned before = sb.st_nlink;
	int rc = rename(a, b);
	int e = errno;
	int b_exists = access(b, F_OK) == 0;
	int a_exists = access(a, F_OK) == 0;
	stat(b, &sb);
	printf("rename_rc=%d errno=%d a_exists=%d b_exists=%d b_nlink_before=%u after=%u\n",
	       rc, e, a_exists, b_exists, before, sb.st_nlink);
	return 0;
}
