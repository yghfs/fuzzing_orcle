// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <err.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <unistd.h>

static void snapshot(const char *stage)
{
	char path[512], buffer[4096];
	const char *source = getenv("CT_SNAPSHOT_PATH");
	const char *prefix = getenv("CT_RESULT_PREFIX");
	int in, out;
	ssize_t n;

	if (!source || !prefix)
		errx(1, "snapshot environment missing");
	snprintf(path, sizeof(path), "%s-%s.json", prefix, stage);
	in = open(source, O_RDONLY);
	if (in < 0 || (n = read(in, buffer, sizeof(buffer) - 1)) <= 0)
		err(1, "read snapshot %s", stage);
	buffer[n] = 0;
	close(in);
	out = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0600);
	if (out < 0 || write(out, buffer, n) != n)
		err(1, "write snapshot %s", stage);
	close(out);
	if (!strstr(buffer, "\"valid\":true") ||
	    !strstr(buffer, "\"namespace_valid\":true") ||
	    !strstr(buffer, "\"relations_valid\":true") ||
	    !strstr(buffer, "\"observed_mismatches\":0"))
		errx(1, "invalid snapshot %s: %s", stage, buffer);
	printf("snapshot=%s %s", stage, buffer);
}

int main(int argc, char **argv)
{
	char value[1024], name[32];
	int a, b, fd;

	if (argc != 2 || chdir(argv[1]))
		err(1, "mountpoint");
	memset(value, 'x', sizeof(value));
	a = open("xattr-a", O_CREAT | O_EXCL | O_RDWR, 0600);
	b = open("xattr-b", O_CREAT | O_EXCL | O_RDWR, 0600);
	if (a < 0 || b < 0 || write(b, value, sizeof(value)) != sizeof(value))
		err(1, "create files");
	if (fsetxattr(a, "user.shared", value, sizeof(value), 0) ||
	    fsetxattr(b, "user.shared", value, sizeof(value), 0))
		err(1, "create shared xattr");
	snapshot("shared");
	if (fremovexattr(a, "user.shared"))
		err(1, "detach first reference");
	snapshot("detached");
	if (unlink("xattr-b"))
		err(1, "unlink while open");
	snapshot("open-unlinked");
	if (close(b))
		err(1, "close final reference");
	snapshot("evicted");
	if (close(a) || unlink("xattr-a"))
		err(1, "remove first file");
	for (int i = 0; i < 8; i++) {
		snprintf(name, sizeof(name), "reuse-%d", i);
		fd = open(name, O_CREAT | O_EXCL | O_RDWR, 0600);
		if (fd < 0 || close(fd) || unlink(name))
			err(1, "inode reuse");
	}
	snapshot("final");
	return 0;
}
