#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

static void die(const char *s) { perror(s); exit(1); }

static pid_t pressure(size_t mib)
{
	int p[2]; pid_t pid;
	if (pipe(p) || (pid = fork()) < 0) die("pressure fork");
	if (!pid) {
		struct rlimit r = { RLIM_INFINITY, RLIM_INFINITY };
		long page = sysconf(_SC_PAGESIZE);
		size_t bytes = mib * 1024 * 1024;
		unsigned char *m;
		close(p[0]);
		setrlimit(RLIMIT_MEMLOCK, &r);
		m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (m == MAP_FAILED || mlock(m, bytes)) _exit(2);
		for (size_t i = 0; i < bytes; i += page) m[i] = (unsigned char)i;
		if (write(p[1], "R", 1) != 1) _exit(3);
		for (;;) pause();
	}
	close(p[1]);
	if (read(p[0], &(char){0}, 1) != 1) die("pressure ready");
	close(p[0]);
	return pid;
}

int main(int argc, char **argv)
{
	const char *root = argc > 1 ? argv[1] : "/mnt/test";
	unsigned files = argc > 2 ? strtoul(argv[2], NULL, 0) : 200;
	size_t pressure_mib = argc > 3 ? strtoul(argv[3], NULL, 0) : 384;
	char path[512], name[64];
	unsigned char *value = malloc(60000);
	unsigned char *fill = malloc(1024 * 1024);
	pid_t p;
	unsigned created = 0, removed = 0, set_errors = 0, remove_errors = 0;
	if (!value || !fill) die("value");
	memset(value, 'X', 60000);
	memset(fill, 'Y', 1024 * 1024);
	if (mkdir(root, 0700) && errno != EEXIST) die("mkdir root");
	for (unsigned i = 0; i < files; i++) {
		snprintf(name, sizeof(name), "f-%u", i);
		snprintf(path, sizeof(path), "%s/%s", root, name);
		int fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) die("create");
		close(fd);
		if (setxattr(path, "user.large", value, 60000, XATTR_CREATE)) {
			set_errors++; unlink(path); continue;
		}
		created++;
	}
	/* Consume ordinary data blocks until the normal write path returns ENOSPC. */
	{
		char filler[512];
		int fd;
		snprintf(filler, sizeof(filler), "%s/.filler", root);
		fd = open(filler, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) die("filler");
		for (;;) {
			ssize_t n = write(fd, fill, 1024 * 1024);
			if (n < 0) { printf("fill_errno=%d (%s)\n", errno, strerror(errno)); break; }
			if (n != 1024 * 1024) { printf("fill_short=%zd\n", n); break; }
		}
		fsync(fd);
		close(fd);
	}
	p = pressure(pressure_mib);
	for (unsigned i = 0; i < files; i++) {
		snprintf(name, sizeof(name), "f-%u", i);
		snprintf(path, sizeof(path), "%s/%s", root, name);
		/* Keep the EA container intact until inode eviction. */
		if (unlink(path)) remove_errors++; else removed++;
	}
	kill(p, SIGTERM); waitpid(p, NULL, 0);
	sync();
	printf("ea_pressure files=%u created=%u removed=%u set_errors=%u remove_errors=%u pressure_mib=%zu\n",
	       files, created, removed, set_errors, remove_errors, pressure_mib);
	return 0;
}
