#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *root = argc > 1 ? argv[1] : "/mnt/ext4-ns";
	const char *mode = argc > 2 ? argv[2] : "34";
	char path[512];
	int ctl, rc, saved;
	char modebuf[16];
	snprintf(modebuf, sizeof(modebuf), "%s\n", mode);
	ctl = open("/sys/module/ext4/parameters/ext4_fault_mode", O_WRONLY);
	if (ctl < 0 || write(ctl, modebuf, strlen(modebuf)) != (ssize_t)strlen(modebuf)) return 2;
	close(ctl);
	snprintf(path, sizeof(path), "%s/child", root);
	errno = 0;
	rc = mkdir(path, 0700);
	saved = errno;
	ctl = open("/sys/module/ext4/parameters/ext4_fault_mode", O_WRONLY);
	if (ctl >= 0) { if (write(ctl, "0\n", 2) != 2) saved = EIO; close(ctl); }
	printf("mkdir_rc=%d errno=%d(%s) child_after=%d\n", rc, saved,
	       strerror(saved), access(path, F_OK));
	sync();
	return 0;
}
