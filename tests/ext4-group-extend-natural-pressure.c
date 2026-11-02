// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/ext4.h>
#include <stdint.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/statfs.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <unistd.h>

static void protect_from_oom(void)
{
	int fd = open("/proc/self/oom_score_adj", O_WRONLY | O_CLOEXEC);
	const char value[] = "-1000\n";
	ssize_t written;

	if (fd < 0)
		return;
	written = write(fd, value, sizeof(value) - 1);
	if (written != sizeof(value) - 1) {
		close(fd);
		return;
	}
	close(fd);
}

static void make_oom_victim(void)
{
	int fd = open("/proc/self/oom_score_adj", O_WRONLY | O_CLOEXEC);
	const char value[] = "1000\n";
	ssize_t written;

	if (fd < 0)
		return;
	written = write(fd, value, sizeof(value) - 1);
	if (written != sizeof(value) - 1) {
		close(fd);
		return;
	}
	close(fd);
}

static pid_t start_memory_pressure(size_t pressure_len)
{
	int ready[2];
	pid_t pid;

	if (pipe(ready))
		return -1;
	protect_from_oom();
	pid = fork();
	if (pid < 0) {
		close(ready[0]);
		close(ready[1]);
		return -1;
	}
	if (!pid) {
		struct rlimit limit = { RLIM_INFINITY, RLIM_INFINITY };
		long page = sysconf(_SC_PAGESIZE);
		void *pressure;
		char ready_byte = 0;
		bool ready_sent = false;
		size_t pinned = 0;
		size_t chunk_size = 16 * 1024 * 1024;

		close(ready[0]);
		make_oom_victim();
		if (setrlimit(RLIMIT_MEMLOCK, &limit))
			goto notify;
		pressure = mmap(NULL, pressure_len, PROT_READ | PROT_WRITE,
				MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (pressure == MAP_FAILED)
			goto notify;
		for (size_t start = 0; start < pressure_len; start += chunk_size) {
			size_t chunk = pressure_len - start;

			if (chunk > chunk_size)
				chunk = chunk_size;
			for (size_t off = start; off < start + chunk; off += page)
				*((volatile unsigned char *)pressure + off) = 0x5a;
			if (mlock((char *)pressure + start, chunk))
				break;
			pinned += chunk;
			if (!ready_sent && (pinned >= pressure_len * 4 / 5 ||
					    pinned == pressure_len)) {
				ready_byte = 1;
				if (write(ready[1], &ready_byte, sizeof(ready_byte)) !=
				    sizeof(ready_byte))
					_exit(2);
				close(ready[1]);
				ready_sent = true;
			}
		}
	notify:
		if (!ready_sent) {
			if (write(ready[1], &ready_byte, sizeof(ready_byte)) !=
			    sizeof(ready_byte))
				_exit(2);
			close(ready[1]);
		}
		if (!ready_byte)
			_exit(1);
		for (;;)
			pause();
	}
	close(ready[1]);
	{
		char ready_byte = 0;
		ssize_t n = read(ready[0], &ready_byte, sizeof(ready_byte));
		close(ready[0]);
		if (n != sizeof(ready_byte) || !ready_byte) {
			waitpid(pid, NULL, 0);
			errno = ENOMEM;
			return -1;
		}
	}
	return pid;
}

static void show_statfs(int fd, const char *label)
{
	struct statfs st;

	if (fstatfs(fd, &st) < 0) {
		perror("fstatfs");
		return;
	}
	printf("%s_blocks=%" PRIu64 " %s_bfree=%" PRIu64 "\n",
	       label, (uint64_t)st.f_blocks, label, (uint64_t)st.f_bfree);
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/mnt/ext4-group-extend";
	uint32_t target_blocks = argc > 2 ? strtoul(argv[2], NULL, 0) : 50000;
	size_t pressure_len = argc > 3 ?
		(size_t)strtoul(argv[3], NULL, 0) * 1024 * 1024 : 0;
	pid_t pressure_pid = -1, pressure_pid2 = -1;
	int fd, rc, saved_errno;

	fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0) {
		perror("open mountpoint");
		return 1;
	}
	show_statfs(fd, "before");
	if (pressure_len) {
		pressure_pid = start_memory_pressure(pressure_len);
		pressure_pid2 = start_memory_pressure(pressure_len);
		if (pressure_pid < 0 || pressure_pid2 < 0) {
			perror("start locked memory pressure");
			if (pressure_pid > 0)
				kill(pressure_pid, SIGTERM);
			if (pressure_pid > 0)
				waitpid(pressure_pid, NULL, 0);
			if (pressure_pid2 > 0)
				kill(pressure_pid2, SIGTERM);
			if (pressure_pid2 > 0)
				waitpid(pressure_pid2, NULL, 0);
			close(fd);
			return 3;
		}
		printf("pressure_locked_bytes=%zu workers=2\n", pressure_len * 2);
	}

	errno = 0;
	rc = ioctl(fd, EXT4_IOC_GROUP_EXTEND, &target_blocks);
	saved_errno = errno;
	printf("group_extend_target=%" PRIu32 " rc=%d errno=%d (%s)\n",
	       target_blocks, rc, saved_errno, strerror(saved_errno));
	show_statfs(fd, "after");
	if (pressure_pid > 0) {
		kill(pressure_pid, SIGTERM);
		waitpid(pressure_pid, NULL, 0);
	}
	if (pressure_pid2 > 0) {
		kill(pressure_pid2, SIGTERM);
		waitpid(pressure_pid2, NULL, 0);
	}
	close(fd);
	return rc < 0 ? 4 : 0;
}
