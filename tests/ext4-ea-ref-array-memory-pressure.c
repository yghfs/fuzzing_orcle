// SPDX-License-Identifier: GPL-2.0-only
/* Trigger EA inode reference release under ordinary memory pressure. */
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

#ifndef EA_COUNT
#define EA_COUNT 64
#endif
#define VALUE_LEN 5000

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static void set_oom_score_adj(int score)
{
	char value[32];
	int fd = open("/proc/self/oom_score_adj", O_WRONLY | O_CLOEXEC);
	int len;
	ssize_t written;

	if (fd < 0)
		return;
	len = snprintf(value, sizeof(value), "%d\n", score);
	if (len > 0) {
		written = write(fd, value, (size_t)len);
		(void)written;
	}
	close(fd);
}

static pid_t pin_memory(size_t mib)
{
	int ready[2];
	pid_t pid;
	int max_attempt = 10;

	while (mib > 0) {
		long page = sysconf(_SC_PAGESIZE);
		size_t bytes = mib * 1024 * 1024;
		void *probe = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
				   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);

		if (probe != MAP_FAILED) {
			munmap(probe, bytes);
			break;
		}
		if (--max_attempt == 0)
			die("mmap pressure (reduce locked MiB)");
		mib = mib > (size_t)page * 256 ? mib - page * 256 / (1024 * 1024) : 0;
	}
	if (!mib)
		die("no memory available for pressure workload");

	if (pipe(ready))
		die("pipe pressure");
	pid = fork();
	if (pid < 0)
		die("fork pressure");
	if (!pid) {
		struct rlimit limit = { RLIM_INFINITY, RLIM_INFINITY };
		long page = sysconf(_SC_PAGESIZE);
		size_t bytes = mib * 1024 * 1024;
		void *memory;
		char ready_byte = 'R';

		close(ready[0]);
		(void)setpriority(PRIO_PROCESS, 0, 19);
		set_oom_score_adj(-500);
		(void)setrlimit(RLIMIT_MEMLOCK, &limit);
		memory = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (memory == MAP_FAILED)
			_exit(20);
		for (size_t off = 0; off < bytes; off += (size_t)page)
			*((volatile unsigned char *)memory + off) = (unsigned char)(off >> 12);
		if (mlock(memory, bytes))
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
			waitpid(pid, NULL, 0);
			errno = ENOMEM;
			die("mlock pressure (reduce locked MiB)");
		}
	}
	close(ready[0]);
	return pid;
}

static void apply_memory_cgroup(const char *max_value)
{
	char cg[128], path[256], value[64], pid[32], buf[256];
	int fd, n;

	if (!max_value || !*max_value)
		return;
	fd = open("/sys/fs/cgroup/cgroup.subtree_control", O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		die("open cgroup.subtree_control");
	if (write(fd, "+memory\n", 7) != 7 && errno != EBUSY)
		die("enable memory cgroup controller");
	close(fd);
	fd = open("/sys/fs/cgroup/cgroup.controllers", O_RDONLY | O_CLOEXEC);
	if (fd >= 0) {
		n = read(fd, buf, sizeof(buf) - 1);
		if (n > 0) { buf[n] = '\0'; fprintf(stderr, "cgroup.controllers=%s", buf); }
		close(fd);
	}
	snprintf(cg, sizeof(cg), "/sys/fs/cgroup/ext4-ea-array-%d", getpid());
	if (mkdir(cg, 0755) && errno != EEXIST)
		die("mkdir EA memory cgroup");
	snprintf(path, sizeof(path), "%s/memory.max", cg);
	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		die("open EA memory.max");
	n = snprintf(value, sizeof(value), "%s\n", max_value);
	if (write(fd, value, (size_t)n) != n)
		die("write EA memory.max");
	close(fd);
	snprintf(path, sizeof(path), "%s/cgroup.procs", cg);
	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		die("open EA cgroup.procs");
	n = snprintf(pid, sizeof(pid), "%d\n", getpid());
	if (write(fd, pid, (size_t)n) != n)
		die("write EA cgroup.procs");
	close(fd);
	printf("ea_memcg=%s max=%s\n", cg, max_value);
	fflush(stdout);
}

static void fill_cgroup_memory(size_t kib)
{
	long page = sysconf(_SC_PAGESIZE);
	size_t bytes = kib * 1024;
	volatile unsigned char *memory;
	struct rlimit limit = { RLIM_INFINITY, RLIM_INFINITY };

	if (!bytes)
		return;
	if (setrlimit(RLIMIT_MEMLOCK, &limit))
		perror("raise cgroup pressure memlock limit");
	if (setpriority(PRIO_PROCESS, 0, 0) < 0)
		perror("restore workload priority");
	memory = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (memory == MAP_FAILED)
		die("mmap cgroup pressure");
	for (size_t off = 0; off < bytes; off += (size_t)page)
		memory[off] = (unsigned char)(off >> 12);
	{
		char path[256], buf[128];
		int fd, n;
		snprintf(path, sizeof(path), "/sys/fs/cgroup/ext4-ea-array-%d/memory.current", getpid());
		fd = open(path, O_RDONLY | O_CLOEXEC);
		if (fd >= 0) {
			n = read(fd, buf, sizeof(buf) - 1);
			if (n > 0) { buf[n] = '\0'; printf("ea_memcg_current=%s", buf); fflush(stdout); }
			close(fd);
		}
	}
	if (mlock((void *)memory, bytes))
		fprintf(stderr, "mlock cgroup fill failed errno=%d\n", errno);
	/* Keep these pages resident through the unlink loop. */
}

static void drop_caches(void)
{
	int fd;
	/* Evict dentries/inodes so EA inode iget must reconstruct them, while
	 * keeping executable and shared-library page cache available to userspace.
	 */
	const char value[] = "2\n";

	fd = open("/proc/sys/vm/drop_caches", O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		die("open drop_caches");
	if (write(fd, value, sizeof(value) - 1) != sizeof(value) - 1)
		die("write drop_caches");
	close(fd);
}

static void seed_file(const char *dir, unsigned int iteration)
{
	char path[256], name[64];
	unsigned char value[VALUE_LEN];
	int fd;

	snprintf(path, sizeof(path), "%s/many-ea-%u", dir, iteration);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		die("open EA container");
	for (unsigned int i = 0; i < EA_COUNT; i++) {
		snprintf(name, sizeof(name), "user.a%03u", i);
		for (size_t j = 0; j < sizeof(value); j++)
			value[j] = (unsigned char)(j * 31 + iteration * 7 + i * 3);
		if (fsetxattr(fd, name, value, sizeof(value), 0)) {
			fprintf(stderr, "setxattr iteration=%u index=%u errno=%d\n",
				iteration, i, errno);
			die("fsetxattr seed");
		}
	}
	if (fsync(fd))
		die("fsync EA container");
	if (close(fd))
		die("close EA container");
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-ea-array";
	unsigned int files = argc > 2 ? strtoul(argv[2], NULL, 10) : 128;
	size_t pressure_mib = argc > 3 ? strtoul(argv[3], NULL, 10) : 350;
	long min_free_kib = argc > 4 ? strtol(argv[4], NULL, 10) : 0;
	const char *memcg_max = getenv("EXT4_EA_ARRAY_MEMCG_MAX");
	size_t memcg_fill_kib = getenv("EXT4_EA_ARRAY_MEMCG_FILL_KIB") ?
		strtoul(getenv("EXT4_EA_ARRAY_MEMCG_FILL_KIB"), NULL, 10) :
		(getenv("EXT4_EA_ARRAY_MEMCG_FILL_MIB") ?
		 strtoul(getenv("EXT4_EA_ARRAY_MEMCG_FILL_MIB"), NULL, 10) * 1024 : 0);
	char path[256];
	pid_t pressure;
	int dirfd;

	if (!files || files > 4096 ||
	    (!pressure_mib && (!memcg_max || !*memcg_max || !memcg_fill_kib)))
		return 2;
	set_oom_score_adj(-1000);
	if (mkdir(dir, 0700) && errno != EEXIST)
		die("mkdir workload directory");
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		die("open workload directory");
	for (unsigned int i = 0; i < files; i++) {
		seed_file(dir, i);
		if (!(i % 32)) {
			printf("seeded_files=%u ea_per_file=%u\n", i + 1, EA_COUNT);
			fflush(stdout);
		}
	}
	if (syncfs(dirfd))
		die("syncfs seeded containers");
	if (getenv("EXT4_EA_ARRAY_DROP_CACHES") &&
	    strcmp(getenv("EXT4_EA_ARRAY_DROP_CACHES"), "0") != 0) {
		drop_caches();
		printf("dropped_inode_and_dentry_caches=1\n");
		fflush(stdout);
	}
	apply_memory_cgroup(memcg_max);
	fill_cgroup_memory(memcg_fill_kib);
	/* Move the pressure worker into the workload cgroup too, when present. */
	pressure = pressure_mib ? pin_memory(pressure_mib) : -1;
	if (min_free_kib > 0) {
		FILE *vm = fopen("/proc/sys/vm/min_free_kbytes", "we");
		if (!vm || fprintf(vm, "%ld\n", min_free_kib) < 0 || fclose(vm))
			die("set memory reserve pressure");
		printf("min_free_kbytes=%ld\n", min_free_kib);
	}
	printf("ea_array_unlink_start files=%u ea_per_file=%u locked_mib=%zu\n",
	       files, EA_COUNT, pressure_mib);
	fflush(stdout);
	for (unsigned int i = 0; i < files; i++) {
		snprintf(path, sizeof(path), "%s/many-ea-%u", dir, i);
		if (unlink(path))
			die("unlink EA container");
		if (!(i % 32)) {
			printf("unlinked_files=%u\n", i + 1);
			fflush(stdout);
		}
	}
	if (pressure > 0) {
		kill(pressure, SIGTERM);
		waitpid(pressure, NULL, 0);
	}
	if (rmdir(dir))
		die("rmdir workload directory");
	if (syncfs(dirfd))
		die("syncfs released containers");
	close(dirfd);
	printf("ea_ref_array_memory_pressure_complete files=%u ea_references=%u\n",
	       files, files * EA_COUNT);
	return 0;
}
