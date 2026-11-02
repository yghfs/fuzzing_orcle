// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <linux/fs.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

#define PAUSE_PARAM "/sys/module/ext4/parameters/ext4_checker_namespace_pause"
#define PAUSED_PARAM "/sys/module/ext4/parameters/ext4_checker_namespace_paused"

struct operation {
	int dirfd;
	const char *kind;
	int rc;
	int error;
};

static int write_param(const char *path, char value)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	ssize_t written;

	if (fd < 0)
		return -1;
	written = write(fd, &value, 1);
	close(fd);
	return written == 1 ? 0 : -1;
}

static int read_paused(void)
{
	char value = '0';
	int fd = open(PAUSED_PARAM, O_RDONLY | O_CLOEXEC);

	if (fd < 0)
		return -1;
	if (read(fd, &value, 1) != 1) {
		close(fd);
		return -1;
	}
	close(fd);
	return value == '1';
}

static void *run_operation(void *arg)
{
	struct operation *op = arg;

	errno = 0;
	if (!strcmp(op->kind, "rename"))
		op->rc = renameat(op->dirfd, "rename-old", op->dirfd,
				 "rename-new");
	else if (!strcmp(op->kind, "exchange"))
		op->rc = renameat2(op->dirfd, "exchange-a", op->dirfd,
				   "exchange-b", RENAME_EXCHANGE);
	else if (!strcmp(op->kind, "mkdir"))
		op->rc = mkdirat(op->dirfd, "mkdir-child", 0700);
	else if (!strcmp(op->kind, "link"))
		op->rc = linkat(op->dirfd, "link-source", op->dirfd,
				"link-target", 0);
	else
		op->rc = unlinkat(op->dirfd, "unlink-victim", 0);
	op->error = errno;
	return NULL;
}

static int create_file(int dirfd, const char *name)
{
	int fd = openat(dirfd, name, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC,
			0600);

	if (fd < 0)
		return -1;
	if (fsync(fd) < 0) {
		close(fd);
		return -1;
	}
	return close(fd);
}

static void report_path(int dirfd, const char *name)
{
	struct stat st;

	errno = 0;
	if (!fstatat(dirfd, name, &st, AT_SYMLINK_NOFOLLOW))
		printf("path=%s exists=1 ino=%lu nlink=%lu\n", name,
		       (unsigned long)st.st_ino, (unsigned long)st.st_nlink);
	else
		printf("path=%s exists=0 errno=%d (%s)\n", name, errno,
		       strerror(errno));
}

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/mnt/ext4-link-race";
	const char *kind = argc > 2 ? argv[2] : "unlink";
	const char *source = !strcmp(kind, "rename") ? "rename-old" :
	!strcmp(kind, "exchange") ? NULL :
	!strcmp(kind, "mkdir") ? NULL :
	!strcmp(kind, "link") ? "link-source" :
	!strcmp(kind, "control") ? "control-victim" : "unlink-victim";
	char parent[256];
	struct operation op = { 0 };
	pthread_t worker;
	uint32_t shutdown_flags = EXT4_GOING_FLAGS_NOLOGFLUSH;
	unsigned int spins = 0;
	int mountfd, rootfd, dirfd, syncfd, triggered = 0;
	int shutdown_rc, shutdown_error;

	if (strcmp(kind, "unlink") && strcmp(kind, "rename") &&
	    strcmp(kind, "exchange") &&
	    strcmp(kind, "mkdir") && strcmp(kind, "link") &&
	    strcmp(kind, "control")) {
		fprintf(stderr, "case must be unlink, rename, exchange, mkdir, link, or control\n");
		return 2;
	}
	if (mkdir(mountpoint, 0700) < 0 && errno != EEXIST) {
		perror("mkdir mountpoint");
		return 1;
	}
	if (snprintf(parent, sizeof(parent), "%s/parent", mountpoint) >=
	    (int)sizeof(parent))
		return 1;
	if (mkdir(parent, 0700) < 0 && errno != EEXIST) {
		perror("mkdir parent");
		return 1;
	}
	rootfd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	dirfd = open(parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (rootfd < 0 || dirfd < 0) {
		perror("open mountpoint or parent");
		return 1;
	}
	if (source) {
		if (create_file(dirfd, source) < 0) {
			perror("create source file");
			return 1;
		}
	}
	if (!strcmp(kind, "exchange") &&
	    (create_file(dirfd, "exchange-a") < 0 ||
	     create_file(dirfd, "exchange-b") < 0)) {
		perror("create exchange files");
		return 1;
	}
	syncfd = dup(rootfd);
	if (syncfd < 0 || syncfs(syncfd) < 0) {
		perror("syncfs baseline");
		return 1;
	}
	close(syncfd);
	mountfd = dup(rootfd);
	if (mountfd < 0) {
		perror("dup mountpoint");
		return 1;
	}
	op.dirfd = dirfd;
	op.kind = kind;
	if (strcmp(kind, "control")) {
		if (write_param(PAUSE_PARAM, '1')) {
			perror("arm namespace pause");
			return 1;
		}
		if (pthread_create(&worker, NULL, run_operation, &op)) {
			perror("pthread_create");
			return 1;
		}
		for (spins = 0; spins < 10000; spins++) {
			int paused = read_paused();

			if (paused == 1) {
				triggered = 1;
				break;
			}
			if (paused < 0)
				break;
			usleep(1000);
		}
	}
	errno = 0;
	shutdown_rc = ioctl(mountfd, EXT4_IOC_SHUTDOWN, &shutdown_flags);
	shutdown_error = errno;
	if (strcmp(kind, "control")) {
		if (write_param(PAUSE_PARAM, '0')) {
			perror("release namespace pause");
			return 1;
		}
		pthread_join(worker, NULL);
	} else {
		triggered = 1;
	}
	printf("case=%s triggered=%d spins=%u shutdown_rc=%d errno=%d (%s)\n",
	       kind, triggered, spins, shutdown_rc,
	       shutdown_rc < 0 ? shutdown_error : 0,
	       shutdown_rc < 0 ? strerror(shutdown_error) : "ok");
	printf("operation_rc=%d errno=%d (%s)\n", op.rc, op.error,
	       op.rc < 0 ? strerror(op.error) : "ok");
	if (!strcmp(kind, "rename")) {
		report_path(dirfd, "rename-old");
		report_path(dirfd, "rename-new");
	} else if (!strcmp(kind, "exchange")) {
		report_path(dirfd, "exchange-a");
		report_path(dirfd, "exchange-b");
	} else if (!strcmp(kind, "mkdir")) {
		report_path(dirfd, "mkdir-child");
	} else if (!strcmp(kind, "link")) {
		report_path(dirfd, "link-source");
		report_path(dirfd, "link-target");
	} else if (!strcmp(kind, "control")) {
		report_path(dirfd, "control-victim");
	} else {
		report_path(dirfd, "unlink-victim");
	}
	close(mountfd);
	close(dirfd);
	close(rootfd);
	return triggered ? 0 : 3;
}
