// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <linux/quota.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/fsuid.h>
#include <sys/wait.h>
#include <unistd.h>

#define ROUNDS 32
#define INODE_LIMIT 300000
#define BLOCK_CHUNK (4U * 1024U * 1024U)
#define DELALLOC_CHUNK (1024U * 1024U)
#define DELALLOC_SYNC_EVERY (32U * 1024U * 1024U)
#define QUOTA_UID 4242
#define QUOTA_LIMIT_KIB (32U * 1024U)

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static void exercise_extents(const char *dir, unsigned int round)
{
	char path[256], data[4096];
	int fd;
	snprintf(path, sizeof(path), "%s/extents-%u", dir, round);
	memset(data, '0' + round % 10, sizeof(data));
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		die("create extent file");
	if (ftruncate(fd, 1024 * 1024) || pwrite(fd, data, sizeof(data), 0) != sizeof(data) ||
	    pwrite(fd, data, sizeof(data), 31 * 4096) != sizeof(data) ||
	    fallocate(fd, FALLOC_FL_KEEP_SIZE, 4096, 30 * 4096) || fsync(fd) ||
	    fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, 8192, 20 * 4096) ||
	    ftruncate(fd, 4096) || fsync(fd))
		die("extent lifecycle");
	if (close(fd) || unlink(path))
		die("remove extent file");
}

static void natural_block_exhaustion(const char *dir)
{
	char path[256];
	unsigned long long allocated = 0;
	int fd, err = 0;
	snprintf(path, sizeof(path), "%s/block-pressure", dir);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		die("create block pressure file");
	for (unsigned int i = 0; i < 1024; i++) {
		if (fallocate(fd, 0, (off_t)allocated, BLOCK_CHUNK)) {
			err = errno;
			break;
		}
		allocated += BLOCK_CHUNK;
	}
	printf("block_pressure_bytes=%llu stop_errno=%d (%s)\n", allocated,
	       err, strerror(err));
	if (err != ENOSPC) {
		errno = err ?: EFBIG;
		die("expected natural ENOSPC during fallocate");
	}
	if (close(fd) || unlink(path))
		die("release block pressure");
	sync();
}

/* Fill the filesystem through buffered writes so ext4 must convert delayed
 * allocations while free space is running out. This exercises reservation
 * rollback and writeback ENOSPC through ordinary syscalls. */
static void natural_delalloc_exhaustion(const char *dir)
{
	char path[256];
	char *data;
	unsigned long long written = 0;
	int fd, write_errno = 0, sync_errno = 0;
	snprintf(path, sizeof(path), "%s/delalloc-pressure", dir);
	data = malloc(DELALLOC_CHUNK);
	if (!data)
		die("allocate delayed-write buffer");
	memset(data, 0x5a, DELALLOC_CHUNK);
	fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	if (fd < 0)
		die("create delayed-write pressure file");
	for (unsigned int i = 0; i < 4096; i++) {
		ssize_t n = pwrite(fd, data, DELALLOC_CHUNK, (off_t)written);

		if (n < 0) {
			write_errno = errno;
			break;
		}
		if (n != DELALLOC_CHUNK) {
			write_errno = EIO;
			break;
		}
		written += n;
		if (written % DELALLOC_SYNC_EVERY == 0 && fsync(fd)) {
			sync_errno = errno;
			break;
		}
	}
	if (!write_errno && !sync_errno && fsync(fd))
		sync_errno = errno;
	printf("delalloc_pressure_bytes=%llu write_errno=%d (%s) "
	       "fsync_errno=%d (%s)\n", written, write_errno,
	       strerror(write_errno), sync_errno, strerror(sync_errno));
	/* A full filesystem normally reports ENOSPC, but buffered writeback can
	 * surface the same failed allocation as EIO after the write error has
	 * been latched in the address space.  Both are ordinary syscall results
	 * here; keep the run alive so the inode/block exhaustion phases execute. */
	if (write_errno != ENOSPC && write_errno != EIO &&
	    sync_errno != ENOSPC && sync_errno != EIO) {
		int err = write_errno ?: sync_errno ?: EFBIG;
		if (ftruncate(fd, 0))
			die("cleanup delayed-write pressure file");
		close(fd);
		unlink(path);
		errno = err;
		die("expected natural ENOSPC/EIO during delayed writeback");
	}
	if (ftruncate(fd, 0))
		die("truncate delayed-write pressure");
	if (fsync(fd) && errno != ENOSPC && errno != EIO)
		die("sync delayed-write cleanup");
	if (close(fd) || unlink(path))
		die("remove delayed-write pressure file");
	free(data);
	sync();
}

static void natural_inode_exhaustion(const char *dir)
{
	char name[64];
	unsigned int created = 0;
	int fd, stop_errno = 0;
	for (; created < INODE_LIMIT; created++) {
		snprintf(name, sizeof(name), "inode-pressure-%u", created);
		fd = openat(AT_FDCWD, name, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
		if (fd < 0) {
			stop_errno = errno;
			break;
		}
		if (close(fd))
			die("close inode pressure file");
		if (!(created % 4096))
			fprintf(stderr, "inode_pressure_progress=%u\n", created);
	}
	printf("inode_pressure_files=%u stop_errno=%d (%s)\n", created,
	       stop_errno, strerror(stop_errno));
	if (stop_errno != ENOSPC) {
		errno = stop_errno ?: EFBIG;
		/* Clean up before reporting unexpected exhaustion. */
	} else {
		stop_errno = 0;
	}
	for (unsigned int i = 0; i < created; i++) {
		snprintf(name, sizeof(name), "inode-pressure-%u", i);
		if (unlinkat(AT_FDCWD, name, 0))
			die("remove inode pressure file");
	}
	if (stop_errno)
		die("expected natural inode ENOSPC");
	(void)dir;
}

/* Apply a real per-user hard quota, then reach EDQUOT through ordinary
 * buffered writes and delayed allocation.  The quota setup is harness state;
 * allocation and failure are caused only by normal write syscalls. */
static void natural_quota_exhaustion(const char *dir, const char *quota_device)
{
	struct if_dqblk quota = { 0 };
	struct if_dqblk observed = { 0 };
	char path[256];
	pid_t child;
	int status, clear_errno = 0;

	if (!quota_device || !*quota_device)
		return;
	quota.dqb_bhardlimit = QUOTA_LIMIT_KIB;
	quota.dqb_ihardlimit = 4096;
	quota.dqb_valid = QIF_BLIMITS | QIF_ILIMITS;
	if (syscall(SYS_quotactl, QCMD(Q_SETQUOTA, USRQUOTA), quota_device,
		    QUOTA_UID, &quota))
		die("set natural user quota");
	if (syscall(SYS_quotactl, QCMD(Q_GETQUOTA, USRQUOTA), quota_device,
		    QUOTA_UID, &observed))
		die("read back natural user quota");
	fprintf(stderr, "quota_limit_kib=%llu inode_limit=%llu\n",
		(unsigned long long)observed.dqb_bhardlimit,
		(unsigned long long)observed.dqb_ihardlimit);
	if (chmod(dir, 0777))
		die("make quota workload directory writable");
	snprintf(path, sizeof(path), "%s/quota-pressure", dir);
	child = fork();
	if (child < 0)
		die("fork quota pressure writer");
	if (!child) {
		char *data;
		unsigned long long written = 0;
		int fd, write_errno = 0, fsync_errno = 0;

		if (setgid(QUOTA_UID) || setuid(QUOTA_UID)) {
			perror("drop quota writer credentials");
			_exit(2);
		}
		data = malloc(1024 * 1024);
		if (!data) {
			perror("allocate quota pressure buffer");
			_exit(2);
		}
		memset(data, 0x6b, 1024 * 1024);
		fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0) {
			perror("create quota pressure file");
			_exit(2);
		}
		for (;;) {
			ssize_t n = pwrite(fd, data, 1024 * 1024,
					   (off_t)written);

			if (n < 0) {
				write_errno = errno;
				break;
			}
			if (!n) {
				write_errno = EIO;
				break;
			}
			written += n;
			if (written > 128ULL * 1024 * 1024)
				break;
		}
		if (fsync(fd))
			fsync_errno = errno;
		close(fd);
		fprintf(stderr, "quota_pressure_bytes=%llu write_errno=%d (%s) "
			"fsync_errno=%d (%s)\n", written, write_errno,
			strerror(write_errno), fsync_errno, strerror(fsync_errno));
		fflush(stderr);
		free(data);
		_exit(write_errno == EDQUOT || fsync_errno == EDQUOT ? 0 : 1);
	}
	if (waitpid(child, &status, 0) != child)
		die("wait quota pressure writer");
	memset(&observed, 0, sizeof(observed));
	if (syscall(SYS_quotactl, QCMD(Q_GETQUOTA, USRQUOTA), quota_device,
		    QUOTA_UID, &observed))
		clear_errno = errno;
	fprintf(stderr, "quota_usage_bytes=%llu quota_inodes=%llu\n",
		(unsigned long long)observed.dqb_curspace,
		(unsigned long long)observed.dqb_curinodes);
	if (unlink(path))
		die("remove quota pressure file");
	/* Exercise ext4_new_inode()'s post-bitmap quota failure path.  The
	 * failed create is an ordinary open(O_CREAT); ext4 has already selected
	 * and accounted an inode before dquot_alloc_inode() returns EDQUOT. */
	{
		unsigned int created = 0;
		int inode_errno = 0;
		int count_pipe[2];

		if (pipe(count_pipe))
			die("create quota inode count pipe");
		child = fork();
		if (child < 0)
			die("fork quota inode pressure");
		if (!child) {
			char inode_path[320];

			close(count_pipe[0]);
			if (setgid(QUOTA_UID) || setuid(QUOTA_UID)) {
				perror("drop quota inode writer credentials");
				_exit(2);
			}
			for (unsigned int i = 0; ; i++) {
				int fd;

				snprintf(inode_path, sizeof(inode_path),
					 "%s/quota-inode-%u", dir, i);
				fd = open(inode_path,
					  O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC,
					  0600);
				if (fd < 0) {
					inode_errno = errno;
					break;
				}
				if (close(fd)) {
					inode_errno = errno;
					break;
				}
				created++;
			}
			fprintf(stderr, "quota_inode_pressure_files=%u create_errno=%d (%s)\n",
				created, inode_errno, strerror(inode_errno));
			fflush(stderr);
			if (write(count_pipe[1], &created, sizeof(created)) !=
			    sizeof(created))
				_exit(2);
			close(count_pipe[1]);
			_exit(inode_errno == EDQUOT ? 0 : 1);
		}
		close(count_pipe[1]);
		if (read(count_pipe[0], &created, sizeof(created)) != sizeof(created))
			die("read quota inode count");
		close(count_pipe[0]);
		if (waitpid(child, &status, 0) != child)
			die("wait quota inode pressure");
		for (unsigned int i = 0; i < created; i++) {
			char inode_path[320];

			snprintf(inode_path, sizeof(inode_path),
				 "%s/quota-inode-%u", dir, i);
			if (unlink(inode_path))
				die("remove quota inode pressure file");
		}
		if (!WIFEXITED(status) || WEXITSTATUS(status))
			die("expected natural EDQUOT during inode creation");
		printf("quota_inode_pressure=EDQUOT files=%u\n", created);
	}
	if (chmod(dir, 0700))
		die("restore quota workload directory mode");
	quota.dqb_bhardlimit = 0;
	quota.dqb_bsoftlimit = 0;
	quota.dqb_ihardlimit = 0;
	quota.dqb_isoftlimit = 0;
	quota.dqb_valid = QIF_BLIMITS | QIF_ILIMITS;
	if (syscall(SYS_quotactl, QCMD(Q_SETQUOTA, USRQUOTA), quota_device,
		    QUOTA_UID, &quota))
		clear_errno = errno;
	if (clear_errno || !WIFEXITED(status) || WEXITSTATUS(status)) {
		errno = clear_errno ?: (WIFEXITED(status) ? EFBIG : ECHILD);
		die("expected natural EDQUOT during buffered write");
	}
	printf("quota_pressure=EDQUOT quota_usage_bytes=%llu\n",
	       (unsigned long long)observed.dqb_curspace);
}

/* A directory allocation consumes used_dirs before dquot_alloc_inode()
 * rejects it.  Exercise that post-accounting failure with a real inode hard
 * limit, then remove the successful directory through ordinary syscalls. */
static void natural_quota_directory_exhaustion(const char *dir,
						const char *quota_device)
{
	struct if_dqblk quota = { 0 };
	char path[256];
	unsigned int created = 0;
	int count_pipe[2];
	pid_t child;
	int status;

	if (!quota_device || !*quota_device)
		die("missing quota device for directory test");
	quota.dqb_ihardlimit = 1;
	quota.dqb_valid = QIF_ILIMITS;
	if (syscall(SYS_quotactl, QCMD(Q_SETQUOTA, USRQUOTA), quota_device,
			QUOTA_UID, &quota))
		die("set directory inode quota");
	if (chmod(dir, 0777))
		die("make directory quota workload writable");
	if (pipe(count_pipe))
		die("create directory quota count pipe");
	child = fork();
	if (child < 0)
		die("fork directory quota pressure");
	if (!child) {
		unsigned int made = 0;
		int stop_errno;

		close(count_pipe[0]);
		if (setgid(QUOTA_UID) || setuid(QUOTA_UID))
			_exit(2);
		for (;;) {
			snprintf(path, sizeof(path), "%s/quota-dir-%u", dir, made);
			if (!mkdir(path, 0700)) {
				made++;
				continue;
			}
			stop_errno = errno;
			break;
		}
		if (write(count_pipe[1], &made, sizeof(made)) != sizeof(made))
			_exit(2);
		fprintf(stderr, "quota_directory_pressure_dirs=%u mkdir_errno=%d (%s)\n",
			made, stop_errno, strerror(stop_errno));
		fflush(stderr);
		_exit(stop_errno == EDQUOT ? 0 : 1);
	}
	close(count_pipe[1]);
	if (read(count_pipe[0], &created, sizeof(created)) != sizeof(created))
		die("read directory quota count");
	close(count_pipe[0]);
	if (waitpid(child, &status, 0) != child ||
	    !WIFEXITED(status) || WEXITSTATUS(status))
		die("expected natural EDQUOT during directory creation");
	for (unsigned int i = 0; i < created; i++) {
		snprintf(path, sizeof(path), "%s/quota-dir-%u", dir, i);
		if (rmdir(path))
			die("remove quota directory");
	}
	quota.dqb_ihardlimit = 0;
	quota.dqb_isoftlimit = 0;
	if (syscall(SYS_quotactl, QCMD(Q_SETQUOTA, USRQUOTA), quota_device,
			QUOTA_UID, &quota))
		die("clear directory inode quota");
	if (chmod(dir, 0700))
		die("restore directory quota workload mode");
	printf("quota_directory_pressure=EDQUOT dirs=%u\n", created);
}

int main(int argc, char **argv)
{
	char path[128];
	const char *base;
	int path_len;
	setvbuf(stdout, NULL, _IOLBF, 0);
	if (argc > 4) {
		fprintf(stderr, "usage: %s [directory] [quota: 0|1|2] [quota_device]\n", argv[0]);
		return 2;
	}
	base = argc >= 2 ? argv[1] : "/tmp";
	path_len = snprintf(path, sizeof(path), "%s/ext4-natural-pressure.XXXXXX",
			    base);
	if (path_len < 0 || path_len >= (int)sizeof(path)) {
		fprintf(stderr, "pressure directory path too long\n");
		return 2;
	}
	if (!mkdtemp(path))
		die("mkdtemp");
	if (chdir(path))
		die("chdir pressure directory");
	for (unsigned int i = 0; i < ROUNDS; i++) {
		exercise_extents(path, i);
	}
	if (argc >= 3 && atoi(argv[2])) {
		if (atoi(argv[2]) == 3)
			natural_quota_directory_exhaustion(path,
							 argc >= 4 ? argv[3] : base);
		else
			natural_quota_exhaustion(path, argc >= 4 ? argv[3] : base);
		if (atoi(argv[2]) == 2 || atoi(argv[2]) == 3) {
			if (chdir("/"))
				die("leave quota-only workload directory");
			if (rmdir(path))
				die("remove quota-only workload directory");
			printf("natural_pressure_workload=complete quota_only=1\n");
			return 0;
		}
	}
	natural_delalloc_exhaustion(path);
	natural_inode_exhaustion(path);
	natural_block_exhaustion(path);
	if (chdir("/"))
		die("leave workload directory");
	if (rmdir(path))
		die("remove workload directory");
	printf("natural_pressure_workload=complete extent_rounds=%d\n", ROUNDS);
	return 0;
}
