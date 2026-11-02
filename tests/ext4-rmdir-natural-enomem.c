// SPDX-License-Identifier: GPL-2.0-only
/* Try to reach ext4_rmdir()'s child-inode dirty failure using memory pressure. */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define BASE "/mnt/p"
#define NAME_LEN 32

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static void child_name(char *buf, size_t size, unsigned int n)
{
	snprintf(buf, size, "c%05u", n);
}

static unsigned int count_children(void)
{
	DIR *dir = opendir(".");
	struct dirent *de;
	unsigned int count = 0;

	if (!dir)
		return UINT32_MAX;
	while ((de = readdir(dir))) {
		if (de->d_name[0] == 'c' && strlen(de->d_name) == 6)
			count++;
	}
	closedir(dir);
	return count;
}

static void prepare(unsigned int count)
{
	char name[NAME_LEN];
	int dirfd;

	if (mkdir(BASE, 0700) && errno != EEXIST)
		die("mkdir parent");
	if (chdir(BASE))
		die("chdir parent");
	for (unsigned int i = 0; i < count; i++) {
		child_name(name, sizeof(name), i);
		if (mkdir(name, 0700))
			die("mkdir child");
	}
	dirfd = open(".", O_RDONLY | O_DIRECTORY);
	if (dirfd < 0)
		die("open parent for syncfs");
	if (syncfs(dirfd))
		die("syncfs prepare");
	close(dirfd);
	printf("PREPARE_RESULT children=%u\n", count);
}

static void memory_hog(size_t megabytes, int ready_fd)
{
	const size_t page_size = (size_t)sysconf(_SC_PAGESIZE);
	size_t bytes = megabytes * 1024 * 1024;
	volatile unsigned char *mem;
	char ready = 'R';
	int fd;

	fd = open("/proc/self/oom_score_adj", O_WRONLY);
	if (fd >= 0) {
		ssize_t written = write(fd, "1000", 4);
		(void)written;
		close(fd);
	}
	mem = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (mem == MAP_FAILED) {
		int saved = errno;
		ssize_t written = write(ready_fd, "F", 1);
		(void)written;
		dprintf(STDOUT_FILENO,
			"PRESSURE_RESULT mmap_errno=%d (%s) requested_mib=%zu\n",
			saved, strerror(saved), megabytes);
		_exit(0);
	}
	for (size_t offset = 0; offset < bytes; offset += page_size)
		mem[offset] = (unsigned char)(offset >> 12);
	dprintf(STDOUT_FILENO,
		"PRESSURE_RESULT resident_mib=%zu page_size=%zu\n",
		megabytes, page_size);
	{
		ssize_t written = write(ready_fd, &ready, 1);
		if (written != 1)
			_exit(2);
	}
	for (;;)
		pause();
}

static size_t fill_pipes(size_t count)
{
	int (*pipes)[2] = calloc(count, sizeof(*pipes));
	char data[4096];
	size_t made = 0, pages = 0;

	if (!pipes)
		die("allocate pipe tracking array");
	memset(data, 0xa5, sizeof(data));
	for (; made < count; made++) {
		if (pipe2(pipes[made], O_NONBLOCK | O_CLOEXEC))
			break;
		for (;;) {
			ssize_t n = write(pipes[made][1], data, sizeof(data));
			if (n == (ssize_t)sizeof(data)) {
				pages++;
				continue;
			}
			if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
				break;
			if (n < 0 && errno == EINTR)
				continue;
			fprintf(stderr, "PIPE_PRESSURE_WRITE_RESULT errno=%d (%s)\n",
				errno, strerror(errno));
			goto out;
		}
	}
out:
	printf("PIPE_PRESSURE_RESULT requested_pipes=%zu created_pipes=%zu "
	       "resident_4k_pages=%zu bytes=%zu stop_errno=%d (%s)\n",
	       count, made, pages, pages * sizeof(data),
	       made < count ? errno : 0,
	       made < count ? strerror(errno) : "none");
	/* Keep both ends open so the pages remain pinned through all rmdir calls. */
	/* The caller closes these descriptors after rmdir pressure finishes. */
	free(pipes);
	return pages;
}

static void mount_pressure_tmpfs(size_t megabytes)
{
	char mount_opts[64];

	snprintf(mount_opts, sizeof(mount_opts), "size=%zuM", megabytes);
	if (mount("tmpfs", "/pressure", "tmpfs", 0, mount_opts) &&
	    errno != EBUSY)
		die("mount pressure tmpfs");
}

static void drop_clean_caches(void)
{
	int fd = open("/proc/sys/vm/drop_caches", O_WRONLY | O_CLOEXEC);
	const char command[] = "3\n";

	if (fd < 0) {
		perror("open drop_caches");
		return;
	}
	if (write(fd, command, sizeof(command) - 1) !=
	    (ssize_t)(sizeof(command) - 1))
		perror("write drop_caches");
	else
		printf("DROP_CACHES_RESULT clean_pagecache_and_unused_dentries=1\n");
	close(fd);
}

static pid_t start_blocked_tmpfs_writer(int *gate_fd, int *progress_fd)
{
	int gate[2], progress[2];
	pid_t writer;
	int oomfd;

	if (pipe(gate))
		die("pipe tmpfs writer gate");
	if (pipe(progress))
		die("pipe tmpfs writer progress");
	/* Prefer the rmdir caller so the disposable pressure writer is killed
	 * first if the guest reaches global OOM.
	 */
	oomfd = open("/proc/self/oom_score_adj", O_WRONLY);
	if (oomfd >= 0) {
		ssize_t written = write(oomfd, "-500", 4);
		(void)written;
		close(oomfd);
	}
	writer = fork();
	if (writer < 0)
		die("fork gated tmpfs pressure writer");
	if (!writer) {
		char data[1024 * 1024];
		char ready;
		unsigned long long total = 0;
		int fd, child_oomfd;
		close(gate[1]);
		close(progress[0]);
		child_oomfd = open("/proc/self/oom_score_adj", O_WRONLY);
		if (child_oomfd >= 0) {
			ssize_t written = write(child_oomfd, "1000", 4);
			(void)written;
			close(child_oomfd);
		}
		if (read(gate[0], &ready, 1) != 1)
			_exit(3);
		close(gate[0]);
		memset(data, 0x5a, sizeof(data));
		fd = open("/pressure/blob", O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC,
			  0600);
		if (fd < 0)
			_exit(2);
		for (;;) {
			ssize_t n = write(fd, data, sizeof(data));
			char tick = 'P';
			if (n < 0 || n != (ssize_t)sizeof(data)) {
				int saved = n < 0 ? errno : EIO;
				dprintf(STDOUT_FILENO,
					"TMPFS_WRITE_STOP errno=%d (%s) bytes=%llu\n",
					saved, strerror(saved), total);
				break;
			}
			total += n;
			if (write(progress[1], &tick, 1) != 1)
				break;
		}
		(void)fsync(fd);
		close(fd);
		dprintf(STDOUT_FILENO,
			"TMPFS_WRITER_RESULT bytes=%llu\n", total);
		_exit(0);
	}
	close(gate[0]);
	close(progress[1]);
	*gate_fd = gate[1];
	*progress_fd = progress[0];
	return writer;
}

static void release_tmpfs_writer(int gate_fd, int progress_fd,
				 pid_t writer, size_t megabytes)
{
	char start = 'G', tick;
	size_t target_ticks = megabytes / 3;
	ssize_t written = write(gate_fd, &start, 1);
	if (written != 1 && errno != EPIPE)
		perror("release tmpfs writer gate");
	close(gate_fd);
	if (target_ticks > 16)
		target_ticks = 16;
	/* Start rmdir after substantial pressure is present; EOF means the writer
	 * hit OOM before reaching this point, and its tmpfs pages remain allocated.
	 */
	for (size_t i = 0; i < target_ticks; i++) {
		if (read(progress_fd, &tick, 1) != 1)
			break;
	}
	/* Keep the read end open: the writer still reports progress while rmdir
	 * runs, and closing it would send SIGPIPE and stop the memory pressure.
	 */
	(void)writer;
	{
		const char marker[] = "PRESSURE_READY_FOR_RMDIR\n";
		ssize_t n = write(STDOUT_FILENO, marker, sizeof(marker) - 1);
		(void)n;
	}
}

static void run(unsigned int count, const char *pressure_kind,
		size_t pressure_amount)
{
	int pipefd[2], status;
	int dirfd;
	int pin_children = strcmp(pressure_kind, "rawtmpfs") != 0;
	pid_t hog = -1;
	char ready;
	char name[NAME_LEN];
	struct stat parent_st;
	unsigned int successes = 0, errors = 0, absent_after_enomem = 0;
	unsigned int first_enomem_index = UINT32_MAX;
	int enomem_child_absent = 0;
	int first_errno = 0;
	int *held_fds = NULL;
	int *child_fds = NULL;
	size_t held_fd_count = 0;
	size_t pipe_pages = 0;
	int pressure_gate = -1;
	int pressure_progress = -1;
	pid_t pressure_writer = -1;

	if (chdir(BASE))
		die("chdir parent");
	/* Pin each positive dentry/inode while the clean inode-table buffers can
	 * still be reclaimed under the later tmpfs pressure.  This keeps rmdir's
	 * path lookup from being the first allocation point.
	 */
	if (count_children() != count)
		fprintf(stderr, "WARM_WARNING expected=%u actual=%u\n", count,
			count_children());
	{
		struct rlimit nofile;
		if (!getrlimit(RLIMIT_NOFILE, &nofile)) {
			rlim_t desired = count + 64;
			if (desired > nofile.rlim_max)
				nofile.rlim_max = desired;
			if (desired > nofile.rlim_cur)
				nofile.rlim_cur = desired;
			if (setrlimit(RLIMIT_NOFILE, &nofile))
				perror("raise RLIMIT_NOFILE for child directory pins");
		}
	}
	if (pin_children) {
		child_fds = calloc(count, sizeof(*child_fds));
		if (!child_fds)
			die("allocate child directory fd tracking array");
		for (unsigned int i = 0; i < count; i++) {
			child_name(name, sizeof(name), i);
			child_fds[i] = open(name, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
			if (child_fds[i] < 0)
				die("pin child directory dentry");
		}
		printf("PINNED_CHILD_DIRECTORIES count=%u\n", count);
	} else {
		printf("PINNED_CHILD_DIRECTORIES count=0 mode=lookup_each_rmdir\n");
	}
	/* Keep the child dentries/inodes referenced by open fds, but discard clean
	 * inode-table and directory page-cache buffers.  ext4_mark_inode_dirty()
	 * may then need to allocate/read the child's inode-table buffer after the
	 * parent directory entry has already been deleted.
	 */
	drop_clean_caches();
	if (!strcmp(pressure_kind, "fd")) {
		struct rlimit nofile;
		if (!getrlimit(RLIMIT_NOFILE, &nofile)) {
			rlim_t desired = pressure_amount + 64;
			if (desired > nofile.rlim_max)
				nofile.rlim_max = desired;
			if (desired > nofile.rlim_cur)
				nofile.rlim_cur = desired;
			if (setrlimit(RLIMIT_NOFILE, &nofile))
				perror("raise RLIMIT_NOFILE");
			if (!getrlimit(RLIMIT_NOFILE, &nofile))
				printf("RLIMIT_NOFILE cur=%lu max=%lu requested=%zu\n",
				       (unsigned long)nofile.rlim_cur,
				       (unsigned long)nofile.rlim_max, pressure_amount + 64);
		}
		held_fds = calloc(pressure_amount, sizeof(*held_fds));
		if (!held_fds)
			die("allocate fd tracking array");
		for (; held_fd_count < pressure_amount; held_fd_count++) {
			held_fds[held_fd_count] = open("/dev/null",
						       O_RDONLY | O_CLOEXEC);
			if (held_fds[held_fd_count] < 0)
				break;
		}
		printf("PRESSURE_RESULT kind=fd requested=%zu held=%zu "
		       "stop_errno=%d (%s)\n", pressure_amount, held_fd_count,
		       held_fd_count < pressure_amount ? errno : 0,
		       held_fd_count < pressure_amount ? strerror(errno) : "none");
	} else if (!strcmp(pressure_kind, "pipes")) {
		pipe_pages = fill_pipes(pressure_amount);
	} else if (!strcmp(pressure_kind, "fdpipes")) {
		struct rlimit nofile;
		size_t fd_pressure = 300000;
		if (!getrlimit(RLIMIT_NOFILE, &nofile)) {
			rlim_t desired = fd_pressure + pressure_amount * 2 + 64;
			if (desired > nofile.rlim_max)
				nofile.rlim_max = desired;
			if (desired > nofile.rlim_cur)
				nofile.rlim_cur = desired;
			if (setrlimit(RLIMIT_NOFILE, &nofile))
				perror("raise RLIMIT_NOFILE for fdpipe pressure");
		}
		held_fds = calloc(fd_pressure, sizeof(*held_fds));
		if (!held_fds)
			die("allocate fd tracking array");
		for (; held_fd_count < fd_pressure; held_fd_count++) {
			held_fds[held_fd_count] = open("/dev/null",
						       O_RDONLY | O_CLOEXEC);
			if (held_fds[held_fd_count] < 0)
				break;
		}
		printf("FD_PRESSURE_RESULT requested=%zu held=%zu\n",
		       fd_pressure, held_fd_count);
		pipe_pages = fill_pipes(pressure_amount);
	} else if (!strcmp(pressure_kind, "none")) {
		/* Pressure was set up by the initramfs using persistent tmpfs pages. */
	} else if (!strcmp(pressure_kind, "tmpfs") ||
		   !strcmp(pressure_kind, "rawtmpfs")) {
		mount_pressure_tmpfs(pressure_amount);
		pressure_writer = start_blocked_tmpfs_writer(&pressure_gate,
							     &pressure_progress);
		release_tmpfs_writer(pressure_gate, pressure_progress,
				     pressure_writer, pressure_amount);
		pressure_gate = -1;
	} else if (!strcmp(pressure_kind, "fdtmpfs")) {
		struct rlimit nofile;
		size_t fd_pressure = 50000;
		signal(SIGPIPE, SIG_IGN);
		mount_pressure_tmpfs(pressure_amount);
		pressure_writer = start_blocked_tmpfs_writer(&pressure_gate,
							     &pressure_progress);
		if (!getrlimit(RLIMIT_NOFILE, &nofile)) {
			rlim_t desired = fd_pressure + count + 64;
			if (desired > nofile.rlim_max)
				nofile.rlim_max = desired;
			if (desired > nofile.rlim_cur)
				nofile.rlim_cur = desired;
			if (setrlimit(RLIMIT_NOFILE, &nofile))
				perror("raise RLIMIT_NOFILE for fdtmpfs pressure");
		}
		held_fds = calloc(fd_pressure, sizeof(*held_fds));
		if (!held_fds)
			die("allocate fdtmpfs tracking array");
		for (; held_fd_count < fd_pressure; held_fd_count++) {
			held_fds[held_fd_count] = open("/dev/null",
						       O_RDONLY | O_CLOEXEC);
			if (held_fds[held_fd_count] < 0)
				break;
		}
		printf("FD_PRESSURE_RESULT requested=%zu held=%zu\n",
		       fd_pressure, held_fd_count);
		release_tmpfs_writer(pressure_gate, pressure_progress,
				     pressure_writer, pressure_amount);
		pressure_gate = -1;
	} else if (!strcmp(pressure_kind, "anon")) {
		if (pipe(pipefd))
			die("pipe");
		hog = fork();
		if (hog < 0)
			die("fork memory pressure process");
		if (!hog) {
			close(pipefd[0]);
			memory_hog(pressure_amount, pipefd[1]);
		}
		close(pipefd[1]);
		if (read(pipefd[0], &ready, 1) != 1)
			die("wait for memory pressure");
		close(pipefd[0]);
		if (ready != 'R') {
			fprintf(stderr, "PRESSURE_NOT_ESTABLISHED marker=%c\n", ready);
			kill(hog, SIGTERM);
			waitpid(hog, &status, 0);
			return;
		}
	} else {
		fprintf(stderr, "unknown pressure kind: %s\n", pressure_kind);
		exit(2);
	}
	if (pipe_pages)
		printf("PRESSURE_PAGES_HELD pages=%zu bytes=%zu\n", pipe_pages,
		       pipe_pages * 4096);

	for (unsigned int i = 0; i < count; i++) {
		int rc, saved;
		struct stat child_st;

		child_name(name, sizeof(name), i);
		rc = rmdir(name);
		saved = rc ? errno : 0;
		if (!rc) {
			successes++;
			continue;
		}
		errors++;
		if (!first_errno)
			first_errno = saved;
		if (saved == ENOMEM) {
			int exists = fstatat(AT_FDCWD, name, &child_st,
					     AT_SYMLINK_NOFOLLOW) == 0;
			int verify_errno = exists ? 0 : errno;
			if (first_enomem_index == UINT32_MAX)
				first_enomem_index = i;
			if (!exists && verify_errno == ENOENT)
				absent_after_enomem++;
			printf("RMDIR_ENOMEM index=%u child_exists=%d "
			       "verify_errno=%d (%s)\n", i, exists, verify_errno,
			       verify_errno ? strerror(verify_errno) : "none");
		}
	}
	if (!strcmp(pressure_kind, "fd") || !strcmp(pressure_kind, "fdpipes") ||
	    !strcmp(pressure_kind, "fdtmpfs")) {
		for (size_t i = 0; i < held_fd_count; i++)
			close(held_fds[i]);
		free(held_fds);
	}
	if (child_fds) {
		for (unsigned int i = 0; i < count; i++)
			close(child_fds[i]);
	}
	free(child_fds);
	if (!strcmp(pressure_kind, "anon")) {
		kill(hog, SIGTERM);
		waitpid(hog, &status, 0);
	} else if (!strcmp(pressure_kind, "tmpfs") ||
		   !strcmp(pressure_kind, "rawtmpfs") ||
		   !strcmp(pressure_kind, "fdtmpfs")) {
		if (waitpid(pressure_writer, &status, 0) != pressure_writer)
			die("wait tmpfs pressure writer after rmdir");
		if (WIFSIGNALED(status))
			printf("TMPFS_WRITER_EXIT signal=%d\n", WTERMSIG(status));
		else
			printf("TMPFS_WRITER_EXIT status=%d\n",
			       WIFEXITED(status) ? WEXITSTATUS(status) : -1);
		if (pressure_progress >= 0)
			close(pressure_progress);
	} else if (!strcmp(pressure_kind, "pipes")) {
		/* pipe descriptors are reclaimed automatically at process exit. */
	}
	/* Release pressure before collecting the authoritative namespace counts. */
	if (fstatat(AT_FDCWD, ".", &parent_st, 0))
		die("stat parent");
	{
		unsigned int remaining = count_children();
		if (remaining == UINT32_MAX)
			die("count remaining child directories");
		if (fstatat(AT_FDCWD, ".", &parent_st, 0))
			die("restat parent");
		if (absent_after_enomem && parent_st.st_nlink != 2 + remaining)
			enomem_child_absent = 1;
		printf("RMDIR_PRESSURE_RESULT attempts=%u succeeded=%u errors=%u "
		       "first_errno=%d (%s) remaining_subdirs=%u parent_nlink=%lu "
		       "expected_nlink=%u absent_after_enomem=%u "
		       "target_mismatch=%d first_enomem_index=%u\n",
		       count, successes, errors, first_errno,
		       first_errno ? strerror(first_errno) : "none", remaining,
		       (unsigned long)parent_st.st_nlink, 2 + remaining,
		       absent_after_enomem, enomem_child_absent,
		       first_enomem_index);
	}
	dirfd = open(".", O_RDONLY | O_DIRECTORY);
	if (dirfd < 0)
		die("open parent for final syncfs");
	if (syncfs(dirfd))
		die("syncfs run");
	close(dirfd);
}

int main(int argc, char **argv)
{
	unsigned long count, pressure_amount;

	setvbuf(stdout, NULL, _IOLBF, 0);
	if (argc < 2 || (strcmp(argv[1], "prepare") && strcmp(argv[1], "run"))) {
		fprintf(stderr, "usage: %s prepare CHILDREN | run CHILDREN anon|fd|pipes|fdpipes|tmpfs|rawtmpfs|fdtmpfs AMOUNT\n",
			argv[0]);
		return 2;
	}
	if ((!strcmp(argv[1], "prepare") && argc != 3) ||
	    (!strcmp(argv[1], "run") && argc != 5)) {
		fprintf(stderr, "wrong number of arguments\n");
		return 2;
	}
	count = strtoul(argv[2], NULL, 10);
	if (!count || count > 20000) {
		fprintf(stderr, "invalid workload size\n");
		return 2;
	}
	if (!strcmp(argv[1], "prepare")) {
		prepare(count);
	} else {
		if (argc != 5) {
			fprintf(stderr, "run requires pressure kind and amount\n");
			return 2;
		}
		pressure_amount = strtoul(argv[4], NULL, 10);
		if (pressure_amount > 500000) {
			fprintf(stderr, "invalid pressure amount\n");
			return 2;
		}
		run(count, argv[3], pressure_amount);
	}
	return 0;
}
