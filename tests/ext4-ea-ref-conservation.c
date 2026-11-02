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
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

#define VALUE_LEN 5000
#define EA_COUNT 64

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static void set_value(int fd, const char *name, unsigned int generation)
{
	unsigned char value[VALUE_LEN];
	for (size_t i = 0; i < sizeof(value); i++)
		value[i] = (unsigned char)(i * 31U + generation * 17U);
	if (fsetxattr(fd, name, value, sizeof(value), 0) < 0)
		die("fsetxattr");
}

static pid_t memory_pressure(size_t mib)
{
	int ready[2];
	pid_t pid;

	if (!mib)
		return -1;
	if (pipe(ready) < 0)
		die("pipe");
	pid = fork();
	if (pid < 0)
		die("fork");
	if (!pid) {
		long page = sysconf(_SC_PAGESIZE);
		size_t bytes = mib * 1024U * 1024U;
		volatile unsigned char *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		char ready_byte = 'R';
		close(ready[0]);
		if (p == MAP_FAILED)
			_exit(20);
		for (size_t off = 0; off < bytes; off += (size_t)page)
			p[off] = (unsigned char)(off >> 12);
		if (mlock((void *)p, bytes) < 0)
			_exit(21);
		if (write(ready[1], &ready_byte, 1) != 1)
			_exit(22);
		close(ready[1]);
		for (;;)
			pause();
	}
	close(ready[1]);
	{
		char ready_byte;
		if (read(ready[0], &ready_byte, 1) != 1) {
			close(ready[0]);
			die("memory pressure ready");
		}
	}
	close(ready[0]);
	fprintf(stderr, "memory_pressure_ready_mib=%zu\n", mib);
	return pid;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-ea-ref/work";
	unsigned int rounds = argc > 2 ? strtoul(argv[2], NULL, 0) : 64;
	size_t pressure_mib = argc > 3 ? strtoull(argv[3], NULL, 0) : 256;
	char path_a[256], path_b[256];
	int a, b, dirfd;
	pid_t pressure;

	if (mkdir(dir, 0700) < 0 && errno != EEXIST)
		die("mkdir");
	snprintf(path_a, sizeof(path_a), "%s/a", dir);
	snprintf(path_b, sizeof(path_b), "%s/b", dir);
	a = open(path_a, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	b = open(path_b, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
	if (a < 0 || b < 0)
		die("open owners");
	for (unsigned int i = 0; i < EA_COUNT; i++) {
		char name[32];
		snprintf(name, sizeof(name), "user.large_%02u", i);
		set_value(a, name, 1);
		set_value(b, name, 1);
	}
	if (fsync(a) < 0 || fsync(b) < 0)
		die("fsync seed");
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0 || syncfs(dirfd) < 0)
		die("syncfs seed");
	pressure = memory_pressure(pressure_mib);
	fprintf(stderr, "phase=shared_owner_ready rounds=%u\n", rounds);
	for (unsigned int round = 0; round < rounds; round++) {
		/* Replacing the same large value forces xattr block COW while the
		 * old block remains shared by b. */
	{
		char name[32];
		snprintf(name, sizeof(name), "user.large_%02u", round % EA_COUNT);
		set_value(a, name, round + 2);
	}
		if (fsync(a) < 0)
			die("fsync cow");
		if (!(round % 4))
			fprintf(stderr, "cow_round=%u\n", round);
	}
	if (pressure > 0) {
		kill(pressure, SIGTERM);
		waitpid(pressure, NULL, 0);
	}
	if (syncfs(dirfd) < 0)
		die("syncfs final");
	close(a);
	close(b);
	close(dirfd);
	fprintf(stderr, "workload_complete rounds=%u\n", rounds);
	return 0;
}
