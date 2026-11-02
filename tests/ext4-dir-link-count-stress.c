// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_NODES 32768
#define DEFAULT_WORKERS 8
#define DEFAULT_ROUNDS 20
#define DEFAULT_ITERATIONS 500

struct node_count {
	dev_t dev;
	ino_t ino;
	mode_t mode;
	nlink_t nlink;
	unsigned int entries;
	unsigned int child_dirs;
	char path[512];
};

static struct node_count nodes[MAX_NODES];
static size_t node_count;

static void fail(const char *what)
{
	fprintf(stderr, "%s: %s\n", what, strerror(errno));
	_exit(1);
}

static int rename_exchange(int olddirfd, const char *oldname,
			   int newdirfd, const char *newname)
{
	return syscall(SYS_renameat2, olddirfd, oldname, newdirfd, newname,
		       RENAME_EXCHANGE);
}

static void worker(int afd, int bfd, unsigned int id, unsigned int iterations)
{
	char a_name[48], b_name[48], temp_name[48], churn_name[48];
	char file_name[48], link_name[48], symlink_name[48];
	int fd;

	snprintf(a_name, sizeof(a_name), "a-%u", id);
	snprintf(b_name, sizeof(b_name), "b-%u", id);
	snprintf(temp_name, sizeof(temp_name), "tmp-%u", id);
	snprintf(churn_name, sizeof(churn_name), "churn-%u", id);
	snprintf(file_name, sizeof(file_name), "file-%u", id);
	snprintf(link_name, sizeof(link_name), "hard-%u", id);
	snprintf(symlink_name, sizeof(symlink_name), "sym-%u", id);

	for (unsigned int i = 0; i < iterations; i++) {
		if (mkdirat(afd, churn_name, 0700) < 0)
			fail("mkdir churn");
		if (renameat(afd, a_name, bfd, temp_name) < 0)
			fail("move A to B");
		if (renameat(bfd, temp_name, afd, a_name) < 0)
			fail("move B to A");
		if (rename_exchange(afd, a_name, bfd, b_name) < 0)
			fail("exchange directories");
		if (linkat(afd, file_name, bfd, link_name, 0) < 0)
			fail("link file");
		if (unlinkat(bfd, link_name, 0) < 0)
			fail("unlink file link");
		if (symlinkat(a_name, afd, symlink_name) < 0)
			fail("symlink");
		if (unlinkat(afd, symlink_name, 0) < 0)
			fail("unlink symlink");
		if (unlinkat(afd, churn_name, AT_REMOVEDIR) < 0)
			fail("rmdir churn");
	}
	fd = openat(afd, file_name, O_RDONLY | O_CLOEXEC);
	if (fd >= 0)
		close(fd);
	_exit(0);
}

static struct node_count *find_or_add(const struct stat *st,
					      const char *path,
					      int is_entry)
{
	for (size_t i = 0; i < node_count; i++) {
		if (nodes[i].dev == st->st_dev && nodes[i].ino == st->st_ino) {
			if (nodes[i].mode != (st->st_mode & S_IFMT) ||
			    nodes[i].nlink != st->st_nlink) {
				fprintf(stderr, "inconsistent stat for %s\n", path);
				exit(2);
			}
			if (is_entry)
				nodes[i].entries++;
			return &nodes[i];
		}
	}
	if (node_count == MAX_NODES) {
		fprintf(stderr, "directory checker node limit reached\n");
		exit(2);
	}
	struct node_count *node = &nodes[node_count++];
	memset(node, 0, sizeof(*node));
	node->dev = st->st_dev;
	node->ino = st->st_ino;
	node->mode = st->st_mode & S_IFMT;
	node->nlink = st->st_nlink;
	if (is_entry)
		node->entries = 1;
	strncpy(node->path, path, sizeof(node->path) - 1);
	return node;
}

static int walk_dir(int dirfd, const char *path)
{
	DIR *dir = fdopendir(dup(dirfd));
	struct dirent *de;
	int errors = 0;

	if (!dir)
		fail("fdopendir");
	while ((de = readdir(dir))) {
		struct node_count *parent;
		struct stat st;
		char child_path[512];
		int childfd;

		if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, ".."))
			continue;
		if (fstatat(dirfd, de->d_name, &st, AT_SYMLINK_NOFOLLOW) < 0) {
			fprintf(stderr, "stat %s/%s: %s\n", path, de->d_name,
				strerror(errno));
			errors++;
			continue;
		}
		snprintf(child_path, sizeof(child_path), "%s/%s", path,
			 de->d_name);
		find_or_add(&st, child_path, 1);
		if (!S_ISDIR(st.st_mode))
			continue;
		if (fstat(dirfd, &st) < 0) {
			fprintf(stderr, "stat parent %s: %s\n", path, strerror(errno));
			errors++;
			continue;
		}
		parent = find_or_add(&st, path, 0);
		parent->child_dirs++;
		childfd = openat(dirfd, de->d_name,
				 O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
		if (childfd < 0) {
			fprintf(stderr, "open directory %s: %s\n", child_path,
				strerror(errno));
			errors++;
			continue;
		}
		if (walk_dir(childfd, child_path))
			errors++;
		close(childfd);
	}
	closedir(dir);
	return errors;
}

static int check_tree(const char *path)
{
	struct stat st;
	int rootfd, errors = 0;

	node_count = 0;
	rootfd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (rootfd < 0)
		fail("open checker root");
	if (fstat(rootfd, &st) < 0)
		fail("stat checker root");
	find_or_add(&st, path, 0);
	errors += walk_dir(rootfd, path);
	close(rootfd);
	for (size_t i = 0; i < node_count; i++) {
		struct node_count *node = &nodes[i];
		unsigned int expected;

		if (S_ISDIR(node->mode)) {
			/* ext4 may compress very large directory link counts to one. */
			if (node->nlink == 1)
				continue;
			expected = 2 + node->child_dirs;
		} else {
			expected = node->entries;
		}
		if (node->nlink != expected) {
			fprintf(stderr,
				"count mismatch path=%s type=%s st_nlink=%llu "
				"namespace_count=%u child_dirs=%u\n",
				node->path, S_ISDIR(node->mode) ? "dir" : "inode",
				(unsigned long long)node->nlink, expected,
				node->child_dirs);
			errors++;
		}
	}
	if (!errors)
		printf("directory_link_count_check=PASS inodes=%zu\n", node_count);
	return errors ? 1 : 0;
}

int main(int argc, char **argv)
{
	char path[512];
	int workers = argc > 2 ? atoi(argv[2]) : DEFAULT_WORKERS;
	int rounds = argc > 3 ? atoi(argv[3]) : DEFAULT_ROUNDS;
	int iterations = argc > 4 ? atoi(argv[4]) : DEFAULT_ITERATIONS;
	int rootfd, afd, bfd;
	unsigned long operations = 0;

	if (argc < 2 || workers < 1 || workers > 32 || rounds < 1 ||
	    iterations < 1) {
		fprintf(stderr, "usage: %s DIR [WORKERS [ROUNDS [ITERATIONS]]]\n",
			argv[0]);
		return 2;
	}
	if (mkdir(argv[1], 0700) < 0)
		fail("mkdir root");
	snprintf(path, sizeof(path), "%s/A", argv[1]);
	if (mkdir(path, 0700) < 0)
		fail("mkdir A");
	snprintf(path, sizeof(path), "%s/B", argv[1]);
	if (mkdir(path, 0700) < 0)
		fail("mkdir B");
	rootfd = open(argv[1], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (rootfd < 0)
		fail("open root");
	afd = openat(rootfd, "A", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	bfd = openat(rootfd, "B", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (afd < 0 || bfd < 0)
		fail("open parents");
	for (int i = 0; i < workers; i++) {
		char name[48];
		int fd;
		snprintf(name, sizeof(name), "a-%d", i);
		if (mkdirat(afd, name, 0700) < 0)
			fail("seed A child");
		snprintf(name, sizeof(name), "b-%d", i);
		if (mkdirat(bfd, name, 0700) < 0)
			fail("seed B child");
		snprintf(name, sizeof(name), "file-%d", i);
		fd = openat(afd, name, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
		if (fd < 0)
			fail("seed file");
		close(fd);
	}
	for (int round = 0; round < rounds; round++) {
		pid_t pids[32];
		for (int i = 0; i < workers; i++) {
			pids[i] = fork();
			if (pids[i] < 0)
				fail("fork worker");
			if (!pids[i])
				worker(afd, bfd, i, iterations);
		}
		for (int i = 0; i < workers; i++) {
			int status;
			if (waitpid(pids[i], &status, 0) != pids[i])
				fail("wait worker");
			if (!WIFEXITED(status) || WEXITSTATUS(status))
				return 1;
		}
		if (syncfs(rootfd) < 0)
			fail("syncfs round");
		if (check_tree(argv[1])) {
			fprintf(stderr, "directory_link_count_round=%d\n", round);
			return 1;
		}
		operations += (unsigned long)workers * iterations * 9;
	}
	printf("directory_count_stress=complete workers=%d rounds=%d "
	       "iterations=%d syscalls_estimate=%lu\n",
	       workers, rounds, iterations, operations);
	return 0;
}
