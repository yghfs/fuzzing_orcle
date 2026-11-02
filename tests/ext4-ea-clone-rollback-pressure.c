// SPDX-License-Identifier: GPL-2.0-only
/* Exercise shared xattr block COW rollback under ordinary memory pressure. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

#define EA_COUNT 64
#define VALUE_LEN 5000

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static void drop_caches(void)
{
	int fd = open("/proc/sys/vm/drop_caches", O_WRONLY | O_CLOEXEC);
	const char value[] = "3\n";

	if (fd < 0)
		die("open drop_caches");
	if (write(fd, value, sizeof(value) - 1) != sizeof(value) - 1)
		die("write drop_caches");
	close(fd);
}

static void *pin_memory(size_t mib)
{
	struct rlimit limit = { RLIM_INFINITY, RLIM_INFINITY };
	long page_size = sysconf(_SC_PAGESIZE);
	size_t bytes = mib * 1024 * 1024;
	volatile unsigned char *memory;

	if (setrlimit(RLIMIT_MEMLOCK, &limit))
		die("setrlimit memlock");
	memory = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		      MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
	if (memory == MAP_FAILED)
		die("mmap pressure");
	if (mlock((void *)memory, bytes))
		die("mlock pressure");
	for (size_t off = 0; off < bytes; off += (size_t)page_size)
		memory[off] = (unsigned char)(off >> 12);
	return (void *)memory;
}

static void report_checker_snapshot(const char *label)
{
	FILE *checker = fopen("/sys/kernel/debug/ext4/vdb/invariant_check", "r");
	char *line = NULL;
	size_t capacity = 0;
	ssize_t length;

	if (!checker) {
		perror("open invariant_check");
		return;
	}
	printf("checker_snapshot=%s\n", label);
	while ((length = getline(&line, &capacity, checker)) >= 0) {
		if (strstr(line, "model=ea_inode_references") ||
		    strstr(line, "model_ea_references_") ||
		    strstr(line, "model_xattr_block_refs_") ||
		    strstr(line, "snapshot OK") ||
		    strstr(line, "violations_total"))
			fwrite(line, 1, (size_t)length, stdout);
	}
	free(line);
	fclose(checker);
	fflush(stdout);
}

static void seed_pair(const char *dir, unsigned int pair, int *a_out,
		      int *b_out)
{
	char a_path[256], b_path[256], name[64];
	unsigned char value[VALUE_LEN];
	int a, b;

	snprintf(a_path, sizeof(a_path), "%s/a-%u", dir, pair);
	snprintf(b_path, sizeof(b_path), "%s/b-%u", dir, pair);
	a = open(a_path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (a < 0)
		die("open file a");
	b = open(b_path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (b < 0)
		die("open file b");
	for (int which = 0; which < 2; which++) {
		int fd = which ? b : a;

		for (unsigned int i = 0; i < EA_COUNT; i++) {
			snprintf(name, sizeof(name), "user.ea%03u", i);
			for (size_t j = 0; j < sizeof(value); j++)
				value[j] = (unsigned char)(j * 31 + pair * 7 + i * 3);
			value[0] = (unsigned char)pair;
			value[1] = (unsigned char)i;
			if (fsetxattr(fd, name, value, sizeof(value), 0))
				die("fsetxattr seed");
		}
	}
	if (fsync(a) || fsync(b))
		die("fsync seed");
	*a_out = a;
	*b_out = b;
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/mnt/ext4-ea-clone/workload";
	unsigned int pairs = argc > 2 ? strtoul(argv[2], NULL, 10) : 8;
	size_t pressure_mib = argc > 3 ? strtoul(argv[3], NULL, 10) : 800;
	long min_free_kib = argc > 4 ? strtol(argv[4], NULL, 10) : 131072;
	int *files, dirfd;
	void *pressure;
	unsigned int succeeded = 0, failed = 0;

	if (!pairs || pairs > 128 || pressure_mib > 8192)
		return 2;
	if (mkdir(dir, 0700) && errno != EEXIST)
		die("mkdir workload directory");
	dirfd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (dirfd < 0)
		die("open workload directory");
	files = calloc((size_t)pairs * 2, sizeof(*files));
	if (!files)
		die("calloc file descriptors");
	for (unsigned int pair = 0; pair < pairs; pair++) {
		seed_pair(dir, pair, &files[pair * 2], &files[pair * 2 + 1]);
		if (!(pair % 8)) {
			printf("seeded_pairs=%u ea_per_file=%u\n", pair + 1,
			       EA_COUNT);
			fflush(stdout);
		}
	}
	if (syncfs(dirfd))
		die("syncfs seeded pairs");
	drop_caches();
	report_checker_snapshot("before_pressure");
	drop_caches();
	pressure = pin_memory(pressure_mib);
	if (min_free_kib > 0) {
		FILE *vm = fopen("/proc/sys/vm/min_free_kbytes", "we");
		if (!vm || fprintf(vm, "%ld\n", min_free_kib) < 0 || fclose(vm))
			die("set min_free_kbytes");
	}
	printf("clone_cow_start pairs=%u ea_per_file=%u locked_mib=%zu\n",
	       pairs, EA_COUNT, pressure_mib);
	fflush(stdout);
	for (unsigned int pair = 0; pair < pairs; pair++) {
		char name[] = "user.touch";
		unsigned int marker = pair;

		if (!fsetxattr(files[pair * 2], name, &marker, sizeof(marker), 0)) {
			succeeded++;
		} else {
			failed++;
			printf("cow_pair=%u errno=%d (%s)\n", pair, errno,
			       strerror(errno));
		}
		if (!(pair % 8)) {
			printf("cow_progress=%u succeeded=%u failed=%u\n",
			       pair + 1, succeeded, failed);
			fflush(stdout);
		}
	}
	munlock(pressure, pressure_mib * 1024 * 1024);
	munmap(pressure, pressure_mib * 1024 * 1024);
	report_checker_snapshot("after_cow");
	for (unsigned int pair = 0; pair < pairs; pair++) {
		if (fsync(files[pair * 2]) || fsync(files[pair * 2 + 1]))
			die("fsync after COW");
		close(files[pair * 2]);
		close(files[pair * 2 + 1]);
	}
	if (syncfs(dirfd))
		die("syncfs after COW");
	close(dirfd);
	free(files);
	printf("clone_cow_complete pairs=%u succeeded=%u failed=%u\n",
	       pairs, succeeded, failed);
	return 0;
}
