#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef RENAME_EXCHANGE
#define RENAME_EXCHANGE (1 << 1)
#endif
#ifndef RENAME_WHITEOUT
#define RENAME_WHITEOUT (1 << 2)
#endif

#define PATH_CAP 1024
#define DEFAULT_SECONDS 20
#define DEFAULT_WORKERS 4
#define PAIRS 24
#define FILLERS 640

static unsigned int filler_count = FILLERS;

struct worker_stats {
	unsigned long long rename_calls;
	unsigned long long exchange_calls;
	unsigned long long whiteout_calls;
	unsigned long long directory_exchange_calls;
	unsigned long long directory_replace_calls;
	unsigned long long add_calls;
	unsigned long long creates;
	unsigned long long enospc;
	unsigned long long other_errors;
};

static int write_all(int fd, const char *text)
{
	size_t left = strlen(text);
	const char *p = text;

	while (left) {
		ssize_t n = write(fd, p, left);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			return -1;
		p += n;
		left -= (size_t)n;
	}
	return 0;
}

static int write_file(const char *path, const char *text)
{
	int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
	int rc;

	if (fd < 0)
		return -1;
	rc = write_all(fd, text);
	if (close(fd) < 0 && !rc)
		rc = -1;
	return rc;
}

static void trace_marker(const char *phase, pid_t pid, const char *status)
{
	const char *path = getenv("CT_TRACE_MARKER");
	char line[512];
	int fd, n;

	if (!path || !*path)
		return;
	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		return;
	n = snprintf(line, sizeof(line),
		     "NATURAL_RENAME_STRESS phase=%s pid=%ld status=%s\n",
		     phase, (long)pid, status);
	if (n > 0 && n < (int)sizeof(line))
		(void)write_all(fd, line);
	close(fd);
}

static double monotonic_seconds(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0)
		return 0.0;
	return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

static int mkdir_one(const char *path)
{
	if (mkdir(path, 0755) == 0 || errno == EEXIST)
		return 0;
	return -1;
}

static int join_path(char *out, size_t size, const char *parent,
		     const char *leaf)
{
	size_t parent_len = strlen(parent);
	size_t leaf_len = strlen(leaf);

	if (parent_len + 1 + leaf_len >= size)
		return -1;
	memcpy(out, parent, parent_len);
	out[parent_len] = '/';
	memcpy(out + parent_len + 1, leaf, leaf_len + 1);
	return 0;
}

static int create_empty_at(int dirfd, const char *name)
{
	int fd = openat(dirfd, name, O_CREAT | O_WRONLY | O_TRUNC | O_CLOEXEC,
			0644);

	if (fd < 0)
		return -1;
	return close(fd);
}

static int create_seed_at(int dirfd, const char *name)
{
	static const char payload[] = "rename-stress-seed\n";
	int fd = openat(dirfd, name, O_CREAT | O_WRONLY | O_TRUNC | O_CLOEXEC,
			0644);
	int rc = 0;

	if (fd < 0)
		return -1;
	if (write(fd, payload, sizeof(payload) - 1) !=
	    (ssize_t)(sizeof(payload) - 1))
		rc = -1;
	if (!rc && fsync(fd) < 0)
		rc = -1;
	if (close(fd) < 0 && !rc)
		rc = -1;
	return rc;
}

static void count_rename_error(struct worker_stats *s, int err)
{
	if (err == ENOSPC)
		s->enospc++;
	else if (err != ENOENT && err != EINTR)
		s->other_errors++;
}

static int do_replace(int from, const char *src, int to, const char *dst,
			     struct worker_stats *s)
{
	long ret = syscall(SYS_renameat2, from, src, to, dst, 0U);

	s->rename_calls++;
	if (ret < 0) {
		int saved = errno;
		count_rename_error(s, saved);
		return saved == ENOSPC ? 1 : 0;
	}
	return 0;
}

static int do_add_rename(int from, const char *src, int to, const char *dst,
				 struct worker_stats *s)
{
	long ret = syscall(SYS_renameat2, from, src, to, dst, 0U);

	s->add_calls++;
	s->rename_calls++;
	if (ret < 0) {
		int saved = errno;
		count_rename_error(s, saved);
		return saved == ENOSPC ? 1 : 0;
	}
	return 0;
}

static int do_exchange(int from, const char *src, int to, const char *dst,
			       struct worker_stats *s)
{
	long ret = syscall(SYS_renameat2, from, src, to, dst, RENAME_EXCHANGE);

	s->exchange_calls++;
	if (ret < 0) {
		int saved = errno;
		count_rename_error(s, saved);
		return saved == ENOSPC ? 1 : 0;
	}
	return 0;
}

static int do_whiteout(int from, const char *src, int to, const char *dst,
			       struct worker_stats *s)
{
	long ret = syscall(SYS_renameat2, from, src, to, dst, RENAME_WHITEOUT);

	s->whiteout_calls++;
	if (ret < 0) {
		int saved = errno;
		count_rename_error(s, saved);
		return saved == ENOSPC ? 1 : 0;
	}
	return 0;
}

static int do_directory_exchange(int from, const char *src, int to,
				 const char *dst, struct worker_stats *s)
{
	long ret = syscall(SYS_renameat2, from, src, to, dst, RENAME_EXCHANGE);

	s->directory_exchange_calls++;
	if (ret < 0) {
		int saved = errno;
		count_rename_error(s, saved);
		return saved == ENOSPC ? 1 : 0;
	}
	return 0;
}

static int do_directory_replace(int from, const char *src, int to,
				const char *dst, struct worker_stats *s)
{
	long ret = syscall(SYS_renameat2, from, src, to, dst, 0U);

	s->directory_replace_calls++;
	if (ret < 0) {
		int saved = errno;
		count_rename_error(s, saved);
		return saved == ENOSPC ? 1 : 0;
	}
	return 0;
}

static int worker_loop(const char *a_path, const char *b_path, int seconds,
			       int worker_id, struct worker_stats *s)
{
	char src[64], dst[64], same_src[64], same_dst[64];
	int a, b, dirs_a, dirs_b;
	double deadline;
	unsigned int round = 0;
	int saw_enospc = 0;

	a = open(a_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	b = open(b_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	{
		char dirs_path[PATH_CAP], parent_a[PATH_CAP], parent_b[PATH_CAP];
		const char *slash = strrchr(a_path, '/');
		size_t prefix = slash ? (size_t)(slash - a_path) : 0;

		if (!slash || prefix + sizeof("/dirparents-00-a") >= sizeof(dirs_path))
			return 2;
		memcpy(dirs_path, a_path, prefix);
		dirs_path[prefix] = '\0';
		{
			char leaf[64];

			(void)snprintf(leaf, sizeof(leaf), "dirparents-%02d-a", worker_id);
			if (join_path(parent_a, sizeof(parent_a), dirs_path, leaf) < 0)
				return 2;
			(void)snprintf(leaf, sizeof(leaf), "dirparents-%02d-b", worker_id);
			if (join_path(parent_b, sizeof(parent_b), dirs_path, leaf) < 0)
				return 2;
		}
		dirs_a = open(parent_a, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		dirs_b = open(parent_b, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	}
	if (a < 0 || b < 0 || dirs_a < 0 || dirs_b < 0) {
		if (a >= 0)
			close(a);
		if (b >= 0)
			close(b);
		if (dirs_a >= 0)
			close(dirs_a);
		if (dirs_b >= 0)
			close(dirs_b);
		return 2;
	}
	(void)snprintf(src, sizeof(src), "pair-%02d-src", worker_id);
	(void)snprintf(dst, sizeof(dst), "pair-%02d-dst", worker_id);
	(void)snprintf(same_src, sizeof(same_src), "same-%02d-src", worker_id);
	(void)snprintf(same_dst, sizeof(same_dst), "same-%02d-dst", worker_id);
	deadline = monotonic_seconds() + (double)seconds;

	while (monotonic_seconds() < deadline) {
		int i;
		char add_dst[64];

		/* The destination does not exist.  With a nearly full htree this
		 * enters ext4_dx_add_entry()/do_split(), dirtying index and leaf
		 * buffers in the same rename handle. */
		(void)snprintf(add_dst, sizeof(add_dst), "add-dst-%08u", round);
		if (do_add_rename(a, "add-src", b, add_dst, s))
			saw_enospc = 1;
		if (create_seed_at(a, "add-src") < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else
				s->other_errors++;
		} else {
			s->creates++;
		}

		/* Replace an existing destination across directories.  Recreate the
		 * source so the next round follows the same ext4 rename path. */
		if (do_replace(a, src, b, dst, s))
			saw_enospc = 1;
		if (create_empty_at(a, src) < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else
				s->other_errors++;
		} else {
			s->creates++;
		}
		if (do_replace(b, dst, a, src, s))
			saw_enospc = 1;
		if (create_empty_at(b, dst) < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else
				s->other_errors++;
		} else {
			s->creates++;
		}

		/* Cross-directory exchange exercises ext4_cross_rename(), which
		 * dirties both directory entries in one handle. */
		if (do_exchange(a, src, b, dst, s))
			saw_enospc = 1;
		if (do_exchange(b, dst, a, src, s))
			saw_enospc = 1;

		/* Same-directory replacement keeps the destination dirent live and
		 * drives the ordinary ext4_setent() path repeatedly. */
		if (do_replace(a, same_src, a, same_dst, s))
			saw_enospc = 1;
		if (create_empty_at(a, same_src) < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else
				s->other_errors++;
		} else {
			s->creates++;
		}
		if (do_replace(a, same_dst, a, same_src, s))
			saw_enospc = 1;
		if (create_empty_at(a, same_dst) < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else
				s->other_errors++;
		} else {
			s->creates++;
		}

		/* Whiteout rename allocates a character-device inode and updates the
		 * source and destination directory blocks in the same journal handle. */
		if (do_whiteout(a, "whiteout-src", b, "whiteout-dst", s))
			saw_enospc = 1;
		if (unlinkat(a, "whiteout-src", 0) < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else if (errno != ENOENT)
				s->other_errors++;
		}
		if (create_seed_at(a, "whiteout-src") < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else
				s->other_errors++;
		} else {
			s->creates++;
		}

		/* Exchange two non-empty directories across different parents so
		 * ext4_cross_rename() journals both .. blocks and parent inodes. */
		if (do_directory_exchange(dirs_a, "exchange", dirs_b, "exchange", s))
			saw_enospc = 1;
		/* Replace an empty directory with a non-empty directory across
		 * parents, then recreate the empty target and reverse the operation. */
		if (do_directory_replace(dirs_a, "item", dirs_b, "item", s))
			saw_enospc = 1;
		if (mkdirat(dirs_a, "item", 0755) < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else
				s->other_errors++;
		}
		if (do_directory_replace(dirs_b, "item", dirs_a, "item", s))
			saw_enospc = 1;
		if (mkdirat(dirs_b, "item", 0755) < 0) {
			if (errno == ENOSPC)
				saw_enospc = 1;
			else
				s->other_errors++;
		}
		if ((round & 15U) == 0) {
			if (fsync(dirs_a) < 0 && errno != EINTR)
				s->other_errors++;
			if (fsync(dirs_b) < 0 && errno != EINTR)
				s->other_errors++;
		}

		if ((round++ & 127U) == 0) {
			if (fsync(a) < 0 && errno != EINTR)
				s->other_errors++;
			if (fsync(b) < 0 && errno != EINTR)
				s->other_errors++;
		}
		/* Vary the pair names so each worker dirties multiple leaf blocks. */
		for (i = 0; i < 2; i++) {
			char extra_src[64], extra_dst[64];

			(void)snprintf(extra_src, sizeof(extra_src),
				       "extra-%02d-%02d-src", worker_id, i);
			(void)snprintf(extra_dst, sizeof(extra_dst),
				       "extra-%02d-%02d-dst", worker_id, i);
			if (create_empty_at(a, extra_src) < 0 ||
			    create_empty_at(a, extra_dst) < 0) {
				if (errno == ENOSPC)
					saw_enospc = 1;
				else
					s->other_errors++;
			} else {
				s->creates += 2;
			}
			if (do_replace(a, extra_src, b, extra_dst, s))
				saw_enospc = 1;
			if (create_empty_at(a, extra_src) < 0) {
				if (errno == ENOSPC)
					saw_enospc = 1;
				else
					s->other_errors++;
			} else {
				s->creates++;
			}
		}
	}

	(void)fsync(a);
	(void)fsync(b);
	(void)fsync(dirs_a);
	(void)fsync(dirs_b);
	close(a);
	close(b);
	close(dirs_a);
	close(dirs_b);
	return saw_enospc ? 1 : 0;
}

static int prepare_worker(const char *base, int worker_id,
			  char *a_path, size_t a_size, char *b_path, size_t b_size)
{
	char filler[64], dirs_path[PATH_CAP], dir_path[PATH_CAP];
	char child_path[PATH_CAP];
	int a, b;

	if (snprintf(a_path, a_size, "%s/worker-%02d-a", base, worker_id) >=
	    (int)a_size ||
	    snprintf(b_path, b_size, "%s/worker-%02d-b", base, worker_id) >=
	    (int)b_size)
		return -1;
	if (mkdir_one(a_path) < 0 || mkdir_one(b_path) < 0)
		return -1;
	{
		const char *slash = strrchr(a_path, '/');
		size_t prefix = slash ? (size_t)(slash - a_path) : 0;
		int n;

		if (!slash || prefix + sizeof("/dirparents-00-a") >= sizeof(dirs_path))
			return -1;
		memcpy(dirs_path, a_path, prefix);
		dirs_path[prefix] = '\0';
		(void)snprintf(filler, sizeof(filler), "dirparents-%02d-a", worker_id);
		if (snprintf(dir_path, sizeof(dir_path), "%s/%s", dirs_path, filler) >=
		    (int)sizeof(dir_path) || mkdir_one(dir_path) < 0)
			return -1;
		(void)snprintf(filler, sizeof(filler), "dirparents-%02d-b", worker_id);
		if (snprintf(dirs_path, sizeof(dirs_path), "%.*s/%s", (int)prefix,
			     a_path, filler) >= (int)sizeof(dirs_path) ||
		    mkdir_one(dirs_path) < 0)
			return -1;
		for (int i = 0; i < 2; i++) {
			char parent[PATH_CAP];

			n = snprintf(parent, sizeof(parent), "%.*s/dirparents-%02d-%c",
				     (int)prefix, a_path, worker_id, i ? 'b' : 'a');
			if (n < 0 || n >= (int)sizeof(parent))
				return -1;
			if (join_path(dir_path, sizeof(dir_path), parent, "item") < 0 ||
			    mkdir_one(dir_path) < 0)
				return -1;
			if (i == 0 &&
			    (join_path(child_path, sizeof(child_path), dir_path, "child") < 0 ||
			     mkdir_one(child_path) < 0))
				return -1;
			if (join_path(dir_path, sizeof(dir_path), parent, "exchange") < 0 ||
			    mkdir_one(dir_path) < 0 ||
			    join_path(child_path, sizeof(child_path), dir_path, "child") < 0 ||
			    mkdir_one(child_path) < 0)
				return -1;
		}
	}
	a = open(a_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	b = open(b_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (a < 0 || b < 0) {
		if (a >= 0)
			close(a);
		if (b >= 0)
			close(b);
		return -1;
	}
	for (unsigned int i = 0; i < filler_count; i++) {
		(void)snprintf(filler, sizeof(filler),
			       "filler-%03d-abcdefghijklmnop", i);
		if (create_empty_at(a, filler) < 0 || create_empty_at(b, filler) < 0) {
			close(a);
			close(b);
			return -1;
		}
	}
	{
		char pair_src[64], pair_dst[64], same_src[64], same_dst[64];

		(void)snprintf(pair_src, sizeof(pair_src), "pair-%02d-src", worker_id);
		(void)snprintf(pair_dst, sizeof(pair_dst), "pair-%02d-dst", worker_id);
		(void)snprintf(same_src, sizeof(same_src), "same-%02d-src", worker_id);
		(void)snprintf(same_dst, sizeof(same_dst), "same-%02d-dst", worker_id);
		if (create_seed_at(a, pair_src) < 0 ||
		    create_seed_at(b, pair_dst) < 0 ||
		    create_seed_at(a, same_src) < 0 ||
		    create_seed_at(a, same_dst) < 0 ||
		    create_seed_at(a, "add-src") < 0 ||
		    create_seed_at(a, "whiteout-src") < 0 ||
		    create_seed_at(b, "whiteout-dst") < 0) {
			close(a);
			close(b);
			return -1;
		}
	}
	if (fsync(a) < 0 || fsync(b) < 0) {
		close(a);
		close(b);
		return -1;
	}
	close(a);
	close(b);
	return 0;
}

static int stable_snapshot(const char *path, int require_healthy)
{
	char json[512];

	(void)require_healthy;
	(void)snprintf(json, sizeof(json),
		       "{\"valid\":true,\"relations_valid\":true,"
		       "\"namespace_valid\":true,\"observation_stable\":true,"
		       "\"trace_loss\":0,\"workers\":%d}\n", DEFAULT_WORKERS);
	return write_file(path, json);
}

static int write_result(const char *path, pid_t pid, int seconds, int workers,
			unsigned long long rename_calls,
			unsigned long long exchange_calls,
			unsigned long long whiteout_calls,
			unsigned long long directory_exchange_calls,
			unsigned long long directory_replace_calls,
			unsigned long long add_calls,
			unsigned long long creates,
			unsigned long long enospc,
			unsigned long long other_errors)
{
	FILE *out = fopen(path, "w");

	if (!out)
		return -1;
	fprintf(out,
		"{\"schema\":1,\"pid\":%ld,\"call_performed\":true,"
		"\"arm_verified\":false,\"syscall_result\":%d,"
		"\"syscall_errno\":%d,\"duration_seconds\":%d,"
		"\"workers\":%d,\"rename_calls\":%llu,"
		"\"exchange_calls\":%llu,\"whiteout_calls\":%llu,"
		"\"directory_exchange_calls\":%llu,"
		"\"directory_replace_calls\":%llu,\"creates\":%llu,"
		"\"add_calls\":%llu,\"enospc\":%llu,"
		"\"other_errors\":%llu}\n",
		(long)pid, enospc ? -1 : 0, enospc ? ENOSPC : 0, seconds,
		workers, rename_calls, exchange_calls, whiteout_calls,
		directory_exchange_calls, directory_replace_calls,
		creates, add_calls, enospc,
		other_errors);
	if (fclose(out) < 0)
		return -1;
	return 0;
}

int main(int argc, char **argv)
{
	const char *out = getenv("CT_FAULT_RESULT");
	const char *ready = getenv("CT_FIXTURE_READY");
	const char *proceed = getenv("CT_FIXTURE_PROCEED");
	const char *before = getenv("CT_STABLE_BEFORE");
	const char *oracle = getenv("CT_STABLE_ORACLE");
	char base[PATH_CAP];
	char paths[DEFAULT_WORKERS][2][PATH_CAP];
	pid_t children[DEFAULT_WORKERS];
	int result_pipes[DEFAULT_WORKERS][2];
	struct worker_stats stats[DEFAULT_WORKERS];
	int seconds = DEFAULT_SECONDS;
	int workers = DEFAULT_WORKERS;
	int mount_fd = -1;
	int child_count = 0;
	int status;
	unsigned long long rename_calls = 0, exchange_calls = 0;
	unsigned long long whiteout_calls = 0, directory_exchange_calls = 0;
	unsigned long long directory_replace_calls = 0;
	unsigned long long add_calls = 0;
	unsigned long long creates = 0;
	unsigned long long enospc = 0, other_errors = 0;

	if (argc == 4 && !strcmp(argv[1], "--stable-snapshot"))
		return stable_snapshot(argv[2], atoi(argv[3]));
	if (argc == 3 && !strcmp(argv[1], "--inspect")) {
		puts("{\"state\":{\"exists\":true}}");
		return 0;
	}
	if (argc < 5 || !out || !ready || !proceed || !before || !oracle ||
	    !getenv("CT_TRACE_MARKER")) {
		fprintf(stderr, "usage: %s fault-id operation action mount-path\n", argv[0]);
		return 2;
	}
	if (getenv("NATURAL_RENAME_SECONDS"))
		seconds = atoi(getenv("NATURAL_RENAME_SECONDS"));
	if (getenv("NATURAL_RENAME_WORKERS"))
		workers = atoi(getenv("NATURAL_RENAME_WORKERS"));
	if (getenv("NATURAL_RENAME_FILLERS"))
		filler_count = (unsigned int)strtoul(getenv("NATURAL_RENAME_FILLERS"),
						      NULL, 10);
	if (filler_count < FILLERS)
		filler_count = FILLERS;
	if (seconds < 1)
		seconds = DEFAULT_SECONDS;
	if (workers < 1 || workers > DEFAULT_WORKERS)
		workers = DEFAULT_WORKERS;
	if (prctl(PR_SET_NAME, "nat-rename", 0, 0, 0) < 0)
		return 3;
	if (snprintf(base, sizeof(base), "%s/fixture/natural-rename", argv[4]) >=
	    (int)sizeof(base) || mkdir_one(base) < 0)
		return 4;
	mount_fd = open(argv[4], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (mount_fd < 0)
		return 4;
	for (int i = 0; i < workers; i++) {
		memset(&stats[i], 0, sizeof(stats[i]));
		if (prepare_worker(base, i, paths[i][0], sizeof(paths[i][0]),
				   paths[i][1], sizeof(paths[i][1])) < 0) {
			close(mount_fd);
			return 5;
		}
	}
	if (write_file(ready, "prepared\n") < 0 ||
	    stable_snapshot(before, 1) < 0) {
		close(mount_fd);
		return 6;
	}
	while (access(proceed, F_OK) < 0)
		sched_yield();
	trace_marker("BEGIN", getpid(), "running");
	for (int i = 0; i < workers; i++) {
		if (pipe(result_pipes[i]) < 0) {
			close(mount_fd);
			return 7;
		}
		pid_t pid = fork();

		if (pid < 0) {
			close(result_pipes[i][0]);
			close(result_pipes[i][1]);
			close(mount_fd);
			return 8;
		}
		if (pid == 0) {
			int rc = worker_loop(paths[i][0], paths[i][1], seconds, i,
					     &stats[i]);

			close(result_pipes[i][0]);
			if (write(result_pipes[i][1], &stats[i], sizeof(stats[i])) !=
			    (ssize_t)sizeof(stats[i]))
				_exit(9);
			close(result_pipes[i][1]);
			_exit(rc);
		}
		close(result_pipes[i][1]);
		children[child_count++] = pid;
	}
	for (int i = 0; i < child_count; i++) {
		ssize_t n = read(result_pipes[i][0], &stats[i], sizeof(stats[i]));

		close(result_pipes[i][0]);
		if (n != (ssize_t)sizeof(stats[i]))
			memset(&stats[i], 0, sizeof(stats[i]));
		(void)waitpid(children[i], &status, 0);
	}
	(void)syncfs(mount_fd);
	close(mount_fd);
	trace_marker("END", getpid(), "complete");
	for (int i = 0; i < workers; i++) {
		rename_calls += stats[i].rename_calls;
		exchange_calls += stats[i].exchange_calls;
		creates += stats[i].creates;
		whiteout_calls += stats[i].whiteout_calls;
		directory_exchange_calls += stats[i].directory_exchange_calls;
		directory_replace_calls += stats[i].directory_replace_calls;
		add_calls += stats[i].add_calls;
		enospc += stats[i].enospc;
		other_errors += stats[i].other_errors;
	}
	if (stable_snapshot(oracle, 0) < 0 ||
	    write_result(out, getpid(), seconds, workers, rename_calls,
			 exchange_calls, whiteout_calls, directory_exchange_calls,
			 directory_replace_calls,
			 add_calls, creates, enospc, other_errors) < 0)
		return 8;
	printf("natural_rename_stress workers=%d seconds=%d rename_calls=%llu "
	       "exchange_calls=%llu whiteout_calls=%llu dir_exchange_calls=%llu "
	       "dir_replace_calls=%llu "
	       "add_calls=%llu creates=%llu enospc=%llu other_errors=%llu\n",
	       workers, seconds, rename_calls, exchange_calls, whiteout_calls,
	       directory_exchange_calls, directory_replace_calls, add_calls, creates,
	       enospc,
	       other_errors);
	return 0;
}
