/* Deterministic single-worker workload for formula-related ext4 fault stages. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <linux/ext4.h>
#include <linux/fiemap.h>
#include <linux/fs.h>
#include <linux/magic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/vfs.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>

#ifndef RENAME_EXCHANGE
#define RENAME_EXCHANGE (1 << 1)
#endif
#ifndef RENAME_WHITEOUT
#define RENAME_WHITEOUT (1 << 2)
#endif

#define PATH_CAP 1024
#define STATUS_CAP 8192
#define PROGRAM_NAME "fault-formula"

static int configured_fault_errno = -EIO;

struct file_state {
	int exists;
	int stat_ok;
	int xattr_present;
	int acl_present;
	int file_flags_valid;
	int inline_data_valid;
	int inline_data;
	int block0_mapped;
	int block0_initialized;
	int project_id_valid;
	long long size;
	long long blocks;
	long long mode;
	long long xattr_size;
	long long acl_size;
	unsigned long long ino;
	unsigned long long file_flags;
	unsigned long long acl_hash;
	unsigned long nlink;
	unsigned int project_id;
	unsigned int block0_flags;
	unsigned int extent_count;
	unsigned long long extent_hash;
	unsigned long long xattr_hash;
};

struct resize_state {
	int captured;
	int valid;
	int relations_valid;
	unsigned long long fs_blocks;
	unsigned long long fs_free_blocks;
	unsigned long long fs_files;
	unsigned long long fs_free_files;
	long long snapshot_id;
	long long version;
	long long groups;
	long long free_clusters;
	long long free_inodes;
	long long dirs;
};

struct action {
	const char *name;
	const char *operation;
	const char *syscall_name;
	long syscall_nr;
	long (*run)(const char *, const char *, int *);
	int setup_fd;
	int setup_fd2;
	struct file_state setup_state;
	unsigned long long xattr_block;
	char run_arg[PATH_CAP];
	char target[PATH_CAP];
	char source[PATH_CAP];
	char destination[PATH_CAP];
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

static int write_path(const char *path, const char *text)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	int rc;

	if (fd < 0)
		return -1;
	rc = write_all(fd, text);
	if (close(fd) < 0 && !rc)
		rc = -1;
	return rc;
}

static int read_path(const char *path, char *buffer, size_t size)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	ssize_t n;

	if (fd < 0)
		return -1;
	n = read(fd, buffer, size - 1);
	close(fd);
	if (n < 0)
		return -1;
	buffer[n] = '\0';
	return (int)n;
}

static int wait_for_snapshot_gate(void)
{
	const char *ready = getenv("CT_FIXTURE_READY");
	const char *proceed = getenv("CT_FIXTURE_PROCEED");
	int fd;
	static const char text[] = "prepared\n";

	if (!ready || !*ready || !proceed || !*proceed)
		return 0;
	fd = open(ready, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
	if (fd < 0)
		return -1;
	if (write(fd, text, sizeof(text) - 1) != (ssize_t)(sizeof(text) - 1)) {
		close(fd);
		return -1;
	}
	close(fd);
	for (int i = 0; i < 1200 && access(proceed, F_OK) < 0; i++) {
		struct timespec delay = { .tv_sec = 0, .tv_nsec = 50000000 };

		nanosleep(&delay, NULL);
	}
	return access(proceed, F_OK) == 0 ? 0 : -1;
}

static int status_matches_arm(const char *status, const char *operation,
			      pid_t pid, int error)
{
	char needle[160];

	snprintf(needle, sizeof(needle), "\"operation\":\"%s\"", operation);
	if (!strstr(status, "\"enabled\":true") || !strstr(status, needle))
		return 0;
	snprintf(needle, sizeof(needle), "\"errno\":%d", error);
	if (!strstr(status, needle))
		return 0;
	snprintf(needle, sizeof(needle), "\"target_pid\":%ld", (long)pid);
	if (!strstr(status, needle))
		return 0;
	return strstr(status, "\"remaining\":1") &&
	       strstr(status, "\"injected\":0") &&
	       strstr(status, "\"matches\":0") &&
	       strstr(status, "\"program\":\"" PROGRAM_NAME "\"") &&
	       strstr(status, "\"call_index\":0") &&
	       strstr(status, "\"generation\":");
}

static int ensure_dir(const char *path)
{
	if (mkdir(path, 0755) == 0 || errno == EEXIST)
		return 0;
	return -1;
}

static int make_path(char *out, size_t size, const char *mount,
		     const char *leaf)
{
	int n = snprintf(out, size, "%s/fixture/%s", mount, leaf);

	return n >= 0 && (size_t)n < size ? 0 : -1;
}

static int create_file(const char *path, int allocate_blocks)
{
	int fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0644);
	int rc = 0;

	if (fd < 0)
		return -1;
	if (allocate_blocks && fallocate(fd, 0, 0, 1024 * 1024) < 0)
		rc = -1;
	if (!rc && fsync(fd) < 0)
		rc = -1;
	if (close(fd) < 0 && !rc)
		rc = -1;
	return rc;
}

/* Fill a real directory before arming the hook.  The entries are setup state,
 * so their syscalls happen before the target operation is traced.  A full
 * directory is required for the append/read branches in ext4_add_entry(). */
static int populate_directory(const char *parent, unsigned int count)
{
	char path[PATH_CAP];
	unsigned int i;

	for (i = 0; i < count; i++) {
		if (snprintf(path, sizeof(path), "%s/fill-%03u", parent, i) >=
		    (int)sizeof(path) || create_file(path, 0) < 0)
			return -1;
	}
	return 0;
}

static int create_inline_file(const char *path)
{
	char buffer[32];
	long flags;
	int fd;
	int saved;

	memset(buffer, 'i', sizeof(buffer));
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0644);
	if (fd < 0)
		return -1;
	if (write(fd, buffer, sizeof(buffer)) != (ssize_t)sizeof(buffer) ||
	    fsync(fd) < 0 || ioctl(fd, FS_IOC_GETFLAGS, &flags) < 0) {
		saved = errno;
		close(fd);
		errno = saved;
		return -1;
	}
	if (!(flags & FS_INLINE_DATA_FL)) {
		close(fd);
		errno = EOPNOTSUPP;
		return -1;
	}
	if (close(fd) < 0)
		return -1;
	return 0;
}

/* Build nonmergeable, separated extents before arming the fault rule. */
static int create_sparse_extent_count(const char *path, unsigned int count)
{
	int fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0644);
	unsigned int i;

	if (fd < 0)
		return -1;
	for (i = 0; i < count; i++) {
		off_t offset = (off_t)i * 2 * 4096;

		if (fallocate(fd, 0, offset, 4096) < 0) {
			int saved = errno;

			close(fd);
			errno = saved;
			return -1;
		}
	}
	if (fsync(fd) < 0) {
		int saved = errno;

		close(fd);
		errno = saved;
		return -1;
	}
	return close(fd);
}

/* Prepare an inode in the indirect format so EXT4_IOC_MIGRATE has real work. */
static int create_indirect_file(const char *path)
{
	int fd;
	long flags;

	fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0644);
	if (fd < 0)
		return -1;
	/* An empty indirect inode still takes the full ext4_ext_migrate()
	 * temporary-inode and inode-dirty path.  Keeping i_data empty avoids
	 * unrelated extent insertion credits masking the selected dirty hook. */
	if (ioctl(fd, FS_IOC_GETFLAGS, &flags) < 0) {
		int saved = errno;

		close(fd);
		errno = saved;
		return -1;
	}
	flags &= ~FS_EXTENT_FL;
	if (ioctl(fd, FS_IOC_SETFLAGS, &flags) < 0) {
		int saved = errno;

		close(fd);
		errno = saved;
		return -1;
	}
	if (fsync(fd) < 0) {
		int saved = errno;

		close(fd);
		errno = saved;
		return -1;
	}
	return close(fd);
}

static int create_inline_dir_fixture(const char *parent, char *target)
{
	char calibration[PATH_CAP], child[PATH_CAP];
	unsigned long flags;
	int converted_after = -1, fd, i;

	if (snprintf(calibration, sizeof(calibration), "%s-calibration", parent) >=
	    (int)sizeof(calibration) || mkdir(calibration, 0755) < 0)
		return -1;
	/* Calibrate the exact inline capacity on this inode layout, then rebuild
	 * the target one entry below it so the armed mkdir performs conversion. */
	for (i = 0; i < 48; i++) {
		if (snprintf(child, sizeof(child), "%s/fill-%02d-abcdefghijklmnop",
			     calibration, i) >= (int)sizeof(child) || mkdir(child, 0755) < 0)
			return -1;
		fd = open(calibration, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		if (fd < 0 || ioctl(fd, FS_IOC_GETFLAGS, &flags) < 0) {
			if (fd >= 0)
				close(fd);
			return -1;
		}
		close(fd);
		if (!(flags & FS_INLINE_DATA_FL)) {
			converted_after = i + 1;
			break;
		}
	}
	if (converted_after < 1) {
		errno = EOPNOTSUPP;
		return -1;
	}
	for (i = 0; i < converted_after; i++) {
		if (snprintf(child, sizeof(child), "%s/fill-%02d-abcdefghijklmnop",
			     calibration, i) >= (int)sizeof(child) || rmdir(child) < 0)
			return -1;
	}
	if (rmdir(calibration) < 0 || mkdir(parent, 0755) < 0)
		return -1;
	for (i = 0; i < converted_after - 1; i++) {
		if (snprintf(child, sizeof(child), "%s/fill-%02d-abcdefghijklmnop",
			     parent, i) >= (int)sizeof(child) || mkdir(child, 0755) < 0)
			return -1;
	}
	fd = open(parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0 || ioctl(fd, FS_IOC_GETFLAGS, &flags) < 0) {
		if (fd >= 0)
			close(fd);
		return -1;
	}
	close(fd);
	if (!(flags & FS_INLINE_DATA_FL)) {
		errno = EOPNOTSUPP;
		return -1;
	}
	if (snprintf(target, PATH_CAP, "%s/fill-%02d-abcdefghijklmnop", parent,
		     converted_after - 1) >= PATH_CAP)
		return -1;
	return 0;
}

/* Return the physical xattr mapping reported by ext4's xattr FIEMAP view. */
static int xattr_block_for_path(const char *path, unsigned long long *block)
{
	struct fiemap *map;
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	ssize_t value_size;
	int rc = -1;

	if (fd < 0)
		return -1;
	map = calloc(1, sizeof(*map) + 4 * sizeof(struct fiemap_extent));
	if (!map) {
		close(fd);
		return -1;
	}
	map->fm_start = 0;
	map->fm_length = FIEMAP_MAX_OFFSET;
	/* xattr updates are journaled separately from the file data.  Force the
	 * metadata through the same sync path before asking ext4 for its mapping. */
	(void)fsync(fd);
	map->fm_flags = FIEMAP_FLAG_XATTR | FIEMAP_FLAG_SYNC;
	map->fm_extent_count = 4;
	if (ioctl(fd, FS_IOC_FIEMAP, map) == 0 && map->fm_mapped_extents == 1 &&
	    (map->fm_extents[0].fe_flags & FIEMAP_EXTENT_DATA_INLINE) == 0) {
		*block = map->fm_extents[0].fe_physical;
		rc = 0;
	} else {
		/* Some ext4 builds do not expose the xattr iomap through FIEMAP even
		 * though the external attribute is present.  The fixture deliberately
		 * uses a 2048-byte value, which cannot fit in the inode body.  Keep the
		 * block number unknown (zero) and let the independent kernel/disk scans
		 * prove the actual block and reference count. */
		value_size = getxattr(path, "user.fault-fixture", NULL, 0);
		if (value_size >= 1024) {
			*block = 0;
			rc = 0;
		}
	}
	free(map);
	close(fd);
	return rc;
}

static int create_shared_external_xattr(const char *first, const char *second,
					unsigned long long *block)
{
	static const char name[] = "user.fault-fixture";
	char value[2048];
	unsigned long long first_block, second_block;
	size_t i;

	/* Larger than inode-body xattr space, and identical for cache sharing. */
	for (i = 0; i < sizeof(value); i++)
		value[i] = (char)('a' + (i % 23));
	if (create_file(first, 1) < 0)
		goto first_file_error;
	if (create_file(second, 1) < 0)
		goto second_file_error;
	if (setxattr(first, name, value, sizeof(value), 0) < 0)
		goto first_xattr_error;
	if (setxattr(second, name, value, sizeof(value), 0) < 0)
		goto second_xattr_error;
	if (xattr_block_for_path(first, &first_block) < 0)
		goto first_fiemap_error;
	if (xattr_block_for_path(second, &second_block) < 0)
		goto second_fiemap_error;
	if (first_block != second_block) {
		errno = EOPNOTSUPP;
		return -1;
	}
	*block = first_block;
	return 0;

first_file_error:
	fprintf(stderr, "create first xattr fixture file: %s\n", strerror(errno));
	return -1;
second_file_error:
	fprintf(stderr, "create second xattr fixture file: %s\n", strerror(errno));
	return -1;
first_xattr_error:
	fprintf(stderr, "set first external xattr: %s\n", strerror(errno));
	return -1;
second_xattr_error:
	fprintf(stderr, "set second external xattr: %s\n", strerror(errno));
	return -1;
first_fiemap_error:
	fprintf(stderr, "fiemap first external xattr block: %s\n", strerror(errno));
	return -1;
second_fiemap_error:
	fprintf(stderr, "fiemap second external xattr block: %s\n", strerror(errno));
	return -1;
}

/* Start with exactly one owner of the EA inode.  The armed setxattr replaces
 * this value with a different 8 KiB value, so ext4 decrements the old EA
 * inode's reference count from one to zero at ea_inode_ref_dirty.  Sharing
 * the setup value across multiple files would add unrelated refcount state
 * and make the fixture's baseline depend on EA inode sharing behavior. */
static int create_ea_ref_fixture(const char *path,
				 unsigned long long *block)
{
	static const char name[] = "user.fault-fixture";
	/* ext4 stores EA values larger than almost one block in an EA inode. */
	char value[8192];
	size_t i;

	for (i = 0; i < sizeof(value); i++)
		value[i] = (char)('a' + (i % 23));
	if (create_file(path, 1) < 0)
		return -1;
	if (setxattr(path, name, value, sizeof(value), 0) < 0)
		return -1;
	*block = 0;
	return 0;
}

static int create_single_external_xattr(const char *path,
					unsigned long long *block)
{
	static const char name[] = "user.fault-fixture";
	char value[2048];
	size_t i;

	for (i = 0; i < sizeof(value); i++)
		value[i] = (char)('a' + (i % 23));
	if (create_file(path, 1) < 0) {
		fprintf(stderr, "create xattr fixture file: %s\n", strerror(errno));
		return -1;
	}
	if (setxattr(path, name, value, sizeof(value), 0) < 0) {
		fprintf(stderr, "set external xattr: %s\n", strerror(errno));
		return -1;
	}
	if (xattr_block_for_path(path, block) < 0) {
		fprintf(stderr, "fiemap external xattr block: %s\n", strerror(errno));
		return -1;
	}
	return 0;
}

static int capture_state_path(const char *path, struct file_state *state);

static int capture_state_fd(int fd, struct file_state *state)
{
	static const char xattr_name[] = "user.fault-fixture";
	static const char acl_name[] = "system.posix_acl_access";
	struct fiemap *map;
	struct stat st;
	struct fsxattr fsx;
	long file_flags;
	unsigned long long hash = 1469598103934665603ULL;
	unsigned long long xattr_hash = 1469598103934665603ULL;
	unsigned long long acl_hash = 1469598103934665603ULL;
	unsigned char xattr_value[16384];
	unsigned char acl_value[4096];
	ssize_t xattr_size;
	ssize_t acl_size;
	unsigned int i;

	memset(state, 0, sizeof(*state));
	if (fstat(fd, &st) < 0)
		return -1;
	map = calloc(1, sizeof(*map) + 32 * sizeof(struct fiemap_extent));
	if (!map)
		return -1;
	map->fm_start = 0;
	map->fm_length = FIEMAP_MAX_OFFSET;
	map->fm_flags = FIEMAP_FLAG_SYNC;
	map->fm_extent_count = 32;
	if (ioctl(fd, FS_IOC_FIEMAP, map) < 0) {
		if (!S_ISDIR(st.st_mode)) {
			free(map);
			return -1;
		}
		/* Directory FIEMAP is optional; namespace and inode fields remain
		 * useful for the directory actions even without extent mapping. */
		map->fm_mapped_extents = 0;
	}
	xattr_size = fgetxattr(fd, xattr_name, xattr_value, sizeof(xattr_value));
	if (xattr_size < 0 && errno != ENODATA && errno != ENOTSUP &&
	    errno != EOPNOTSUPP) {
		free(map);
		return -1;
	}
	if (xattr_size >= 0) {
		ssize_t j;

		for (j = 0; j < xattr_size; j++) {
			xattr_hash ^= xattr_value[j];
			xattr_hash *= 1099511628211ULL;
		}
		state->xattr_present = 1;
		state->xattr_size = xattr_size;
		state->xattr_hash = xattr_hash;
	}
	acl_size = fgetxattr(fd, acl_name, acl_value, sizeof(acl_value));
	if (acl_size < 0 && errno != ENODATA && errno != ENOTSUP &&
	    errno != EOPNOTSUPP) {
		free(map);
		return -1;
	}
	if (acl_size >= 0) {
		ssize_t j;

		for (j = 0; j < acl_size; j++) {
			acl_hash ^= acl_value[j];
			acl_hash *= 1099511628211ULL;
		}
		state->acl_present = 1;
		state->acl_size = acl_size;
		state->acl_hash = acl_hash;
	}
	state->mode = st.st_mode & 07777;
	if (ioctl(fd, FS_IOC_GETFLAGS, &file_flags) == 0) {
		state->file_flags_valid = 1;
		state->file_flags = (unsigned long long)file_flags;
		state->inline_data_valid = 1;
		state->inline_data = (file_flags & FS_INLINE_DATA_FL) != 0;
	}
	memset(&fsx, 0, sizeof(fsx));
	if (ioctl(fd, FS_IOC_FSGETXATTR, &fsx) == 0) {
		state->project_id_valid = 1;
		state->project_id = fsx.fsx_projid;
	}
	for (i = 0; i < map->fm_mapped_extents; i++) {
		/* FIEMAP reports inline data as an extent too.  R-INLINE's M_i
		 * operand is an actual mapped data block, so DATA_INLINE must not
		 * satisfy it. */
		if (map->fm_extents[i].fe_logical == 0 &&
		    !(map->fm_extents[i].fe_flags & FIEMAP_EXTENT_DATA_INLINE)) {
			state->block0_mapped = 1;
			state->block0_flags = map->fm_extents[i].fe_flags;
			state->block0_initialized =
				!(map->fm_extents[i].fe_flags & FIEMAP_EXTENT_UNWRITTEN);
		}
		const unsigned long long values[] = {
			map->fm_extents[i].fe_logical,
			map->fm_extents[i].fe_physical,
			map->fm_extents[i].fe_length,
			map->fm_extents[i].fe_flags,
		};
		size_t j;

		for (j = 0; j < sizeof(values) / sizeof(values[0]); j++) {
			unsigned int byte;

			for (byte = 0; byte < sizeof(values[j]); byte++) {
				hash ^= (values[j] >> (byte * 8)) & 0xff;
				hash *= 1099511628211ULL;
			}
		}
	}
	state->exists = 1;
	state->stat_ok = 1;
	state->size = st.st_size;
	state->blocks = st.st_blocks;
	state->ino = st.st_ino;
	state->nlink = st.st_nlink;
	state->extent_count = map->fm_mapped_extents;
	state->extent_hash = hash;
	free(map);
	return 0;
}

static int capture_state_path(const char *path, struct file_state *state)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	int rc;

	memset(state, 0, sizeof(*state));
	if (fd < 0) {
		if (errno == ENOENT)
			return 0;
		return -1;
	}
	rc = capture_state_fd(fd, state);
	close(fd);
	return rc;
}

static void marker(const char *kind, const char *phase, const char *fault_id,
		   const char *operation, pid_t pid, const char *status)
{
	const char *path = getenv("CT_TRACE_MARKER");
	char line[1024];
	int fd, n;

	if (!path || !*path)
		return;
	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		return;
	n = snprintf(line, sizeof(line),
		     "%s phase=%s fault_id=%s pid=%ld call_index=0 "
		     "operation=%s errno=%d program=%s status=%s\n",
		     kind, phase, fault_id, (long)pid, operation,
		     configured_fault_errno, PROGRAM_NAME,
		     status);
	if (n > 0 && n < (int)sizeof(line))
		(void)write_all(fd, line);
	close(fd);
}

static void syscall_marker(const char *phase, const char *fault_id,
			  const struct action *action, long result, int call_errno)
{
	const char *path = getenv("CT_TRACE_MARKER");
	char line[1024];
	int fd, n;

	if (!path || !*path)
		return;
	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		return;
	n = snprintf(line, sizeof(line),
		     "CT_SYSCALL phase=%s id=%s-%ld-0 nr=%ld name=%s "
		     "program=%s call_index=0 ret=%ld errno=%d action=%s "
		     "target_fd=%d\n",
		     phase, fault_id, (long)getpid(), action->syscall_nr,
		     action->syscall_name, PROGRAM_NAME, result, call_errno,
		     action->name, action->setup_fd);
	if (n > 0 && n < (int)sizeof(line))
		(void)write_all(fd, line);
	close(fd);
}

static void json_string(FILE *out, const char *key, const char *value)
{
	const unsigned char *p = (const unsigned char *)(value ? value : "");

	fprintf(out, "\"%s\":\"", key);
	for (; *p; p++) {
		if (*p == '"' || *p == '\\')
			fputc('\\', out);
		if (*p == '\n')
			fputs("\\n", out);
		else if (*p >= 0x20)
			fputc(*p, out);
	}
	fputc('"', out);
}

static void write_state(FILE *out, const char *key,
			const struct file_state *state)
{
	fprintf(out, "\"%s\":{\"exists\":%s,\"stat_ok\":%s,"
		"\"size\":%lld,\"blocks\":%lld,\"mode\":%lld,"
		"\"ino\":%llu,"
		"\"nlink\":%lu,\"extent_count\":%u,\"extent_hash\":%llu,"
		"\"inline_data_valid\":%s,\"inline_data\":%s,"
		"\"block0_mapped\":%s,\"block0_initialized\":%s,"
		"\"block0_flags\":%u,"
		"\"xattr_present\":%s,\"xattr_size\":%lld,"
		"\"xattr_hash\":%llu,\"acl_present\":%s,"
		"\"acl_size\":%lld,\"acl_hash\":%llu,"
		"\"file_flags_valid\":%s,\"file_flags\":%llu,"
		"\"project_id_valid\":%s,\"project_id\":%u}",
		key, state->exists ? "true" : "false",
		state->stat_ok ? "true" : "false", state->size, state->blocks,
		state->mode, state->ino, state->nlink, state->extent_count,
		state->extent_hash,
		state->inline_data_valid ? "true" : "false",
		state->inline_data ? "true" : "false",
		state->block0_mapped ? "true" : "false",
		state->block0_initialized ? "true" : "false",
		state->block0_flags,
		state->xattr_present ? "true" : "false", state->xattr_size,
		state->xattr_hash, state->acl_present ? "true" : "false",
		state->acl_size, state->acl_hash,
		state->file_flags_valid ? "true" : "false", state->file_flags,
		state->project_id_valid ? "true" : "false", state->project_id);
}

static int read_counter_snapshot(char *buffer, size_t size)
{
	const char *path = getenv("CT_COUNTER_SNAPSHOT");

	if (!path || !*path)
		path = "/sys/fs/ext4/vdb/counter_snapshot";
	return read_path(path, buffer, size);
}

static int json_integer(const char *json, const char *field, long long *value)
{
	char key[80];
	const char *at;

	if (snprintf(key, sizeof(key), "\"%s\":", field) >= (int)sizeof(key))
		return -1;
	at = strstr(json, key);
	if (!at)
		return -1;
	return sscanf(at + strlen(key), "%lld", value) == 1 ? 0 : -1;
}

static int json_boolean(const char *json, const char *field, int *value)
{
	char key[80];
	const char *at;

	if (snprintf(key, sizeof(key), "\"%s\":", field) >= (int)sizeof(key))
		return -1;
	at = strstr(json, key);
	if (!at)
		return -1;
	at += strlen(key);
	if (!strncmp(at, "true", 4) && (at[4] == ',' || at[4] == '}')) {
		*value = 1;
		return 0;
	}
	if (!strncmp(at, "false", 5) && (at[5] == ',' || at[5] == '}')) {
		*value = 0;
		return 0;
	}
	return -1;
}

static int capture_resize_state(const char *mount, struct resize_state *state)
{
	struct statfs fs;
	char snapshot[STATUS_CAP];

	memset(state, 0, sizeof(*state));
	if (statfs(mount, &fs) < 0 ||
	    read_counter_snapshot(snapshot, sizeof(snapshot)) < 0)
		return -1;
	state->fs_blocks = fs.f_blocks;
	state->fs_free_blocks = fs.f_bfree;
	state->fs_files = fs.f_files;
	state->fs_free_files = fs.f_ffree;
	if (json_boolean(snapshot, "valid", &state->valid) ||
	    json_boolean(snapshot, "relations_valid", &state->relations_valid) ||
	    json_integer(snapshot, "snapshot_id", &state->snapshot_id) ||
	    json_integer(snapshot, "version", &state->version) ||
	    json_integer(snapshot, "groups", &state->groups) ||
	    json_integer(snapshot, "free_clusters", &state->free_clusters) ||
	    json_integer(snapshot, "free_inodes", &state->free_inodes) ||
	    json_integer(snapshot, "dirs", &state->dirs))
		return -1;
	state->captured = 1;
	return 0;
}

static void write_resize_state(FILE *out, const char *key,
			       const struct resize_state *state)
{
	fprintf(out, "\"%s\":{\"captured\":%s,\"valid\":%s,"
		"\"relations_valid\":%s,\"fs_blocks\":%llu,"
		"\"fs_free_blocks\":%llu,\"fs_files\":%llu,"
		"\"fs_free_files\":%llu,\"snapshot_id\":%lld,"
		"\"version\":%lld,\"groups\":%lld,"
		"\"free_clusters\":%lld,\"free_inodes\":%lld,\"dirs\":%lld}",
		key, state->captured ? "true" : "false",
		state->valid ? "true" : "false",
		state->relations_valid ? "true" : "false",
		state->fs_blocks, state->fs_free_blocks, state->fs_files,
		state->fs_free_files, state->snapshot_id, state->version,
		state->groups, state->free_clusters, state->free_inodes,
		state->dirs);
}

static int json_string_equal(const char *first, const char *second,
			     const char *field)
{
	char key[80];
	const char *a, *b, *a_end, *b_end;
	size_t a_len, b_len;

	if (snprintf(key, sizeof(key), "\"%s\":\"", field) >=
	    (int)sizeof(key))
		return 0;
	a = strstr(first, key);
	b = strstr(second, key);
	if (!a || !b)
		return 0;
	a += strlen(key);
	b += strlen(key);
	a_end = strchr(a, '"');
	b_end = strchr(b, '"');
	if (!a_end || !b_end)
		return 0;
	a_len = a_end - a;
	b_len = b_end - b;
	return a_len == b_len && !memcmp(a, b, a_len);
}

static int save_snapshot_pair(const char *path, const char *first,
			      const char *second)
{
	char first_path[PATH_CAP], second_path[PATH_CAP];
	FILE *out;
	int n;

	n = snprintf(first_path, sizeof(first_path), "%s.a.json", path);
	if (n < 0 || n >= (int)sizeof(first_path))
		return -1;
	n = snprintf(second_path, sizeof(second_path), "%s.b.json", path);
	if (n < 0 || n >= (int)sizeof(second_path))
		return -1;
	out = fopen(first_path, "w");
	if (!out)
		return -1;
	if (fprintf(out, "%s\n", first) < 0) {
		fclose(out);
		return -1;
	}
	if (fclose(out))
		return -1;
	out = fopen(second_path, "w");
	if (!out)
		return -1;
	if (fprintf(out, "%s\n", second) < 0) {
		fclose(out);
		return -1;
	}
	if (fclose(out))
		return -1;
	return 0;
}

static int snapshot_semantics_match(const char *first, const char *second)
{
	static const char * const integer_fields[] = {
		"groups", "scanned", "observed_mismatches",
		"namespace_compared", "namespace_mismatched",
		"namespace_skipped_dir_nlink", "namespace_skipped_inline",
		"namespace_skipped_ea", "namespace_skipped_reserved",
		"namespace_skipped_nlink0", "relations_mismatches",
		"xattr_blocks", "orphan_slots", "free_clusters",
		"free_inodes", "dirs", "version",
	};
	static const char * const boolean_fields[] = {
		"valid", "namespace_valid", "relations_valid",
	};
	static const char * const string_fields[] = {
		"reason", "namespace_reason", "relations_reason",
	};
	long long a, b;
	int a_bool, b_bool;
	size_t i;

	for (i = 0; i < sizeof(integer_fields) / sizeof(integer_fields[0]); i++) {
		if (json_integer(first, integer_fields[i], &a) ||
		    json_integer(second, integer_fields[i], &b) || a != b)
			return 0;
	}
	for (i = 0; i < sizeof(boolean_fields) / sizeof(boolean_fields[0]); i++) {
		if (json_boolean(first, boolean_fields[i], &a_bool) ||
		    json_boolean(second, boolean_fields[i], &b_bool) ||
		    a_bool != b_bool)
			return 0;
	}
	for (i = 0; i < sizeof(string_fields) / sizeof(string_fields[0]); i++)
		if (!json_string_equal(first, second, string_fields[i]))
			return 0;
	/* Each read gets a new snapshot id; the observed state version must stay
	 * equal across the pair for the two reads to witness one stable state.
	 */
	if (json_integer(first, "snapshot_id", &a) ||
	    json_integer(second, "snapshot_id", &b) || a >= b)
		return 0;
	return 1;
}

static int capture_stable_snapshot(const char *path, int require_healthy)
{
	char first[STATUS_CAP] = "", second[STATUS_CAP] = "";
	char candidate_first[STATUS_CAP], candidate_second[STATUS_CAP];
	int valid_a, valid_b, relations_a, relations_b;
	int have_pair = 0;
	FILE *out;
	int i;

	for (i = 0; i < 200; i++) {
		if (read_counter_snapshot(candidate_first, sizeof(candidate_first)) >= 0 &&
		    read_counter_snapshot(candidate_second, sizeof(candidate_second)) >= 0) {
			strcpy(first, candidate_first);
			strcpy(second, candidate_second);
			have_pair = 1;
			if (json_boolean(first, "valid", &valid_a) ||
			    json_boolean(second, "valid", &valid_b) ||
			    json_boolean(first, "relations_valid", &relations_a) ||
			    json_boolean(second, "relations_valid", &relations_b))
				goto retry;
			if (snapshot_semantics_match(first, second) &&
			    valid_a == valid_b && relations_a == relations_b &&
			    (!require_healthy || (valid_a && relations_a))) {
				size_t n = strlen(first);
				if (save_snapshot_pair(path, first, second) < 0)
					return -1;
				out = fopen(path, "w");
				if (!out)
					return -1;
				while (n && (first[n - 1] == '\n' || first[n - 1] == '\r'))
					first[--n] = '\0';
				if (n && first[n - 1] == '}')
					first[n - 1] = '\0';
				fprintf(out, "%s,\"observation_stable\":true,\"workers\":1}\n", first);
				fclose(out);
				return 0;
			}
		}
	retry:
		{
			struct timespec delay = { .tv_sec = 0, .tv_nsec = 50000000 };
			nanosleep(&delay, NULL);
		}
	}
	if (have_pair) {
		size_t n;

		(void)save_snapshot_pair(path, first, second);
		n = strlen(second);
		while (n && (second[n - 1] == '\n' || second[n - 1] == '\r'))
			second[--n] = '\0';
		if (n && second[n - 1] == '}')
			second[n - 1] = '\0';
		out = fopen(path, "w");
		if (out) {
			fprintf(out, "%s,\"observation_stable\":false,\"workers\":1}\n",
				second);
			fclose(out);
		}
	}
	return -1;
}

static long call_mkdir(const char *path, const char *dest, int *err)
{
	(void)dest;
	long ret = syscall(SYS_mkdirat, AT_FDCWD, path, 0755);

	*err = ret < 0 ? errno : 0;
	return ret;
}

static long call_rmdir(const char *path, const char *dest, int *err)
{
	(void)dest;
	long ret = syscall(SYS_rmdir, path);

	*err = ret < 0 ? errno : 0;
	return ret;
}

static long call_unlink(const char *path, const char *dest, int *err)
{
	(void)dest;
	long ret = syscall(SYS_unlinkat, AT_FDCWD, path, 0);

	*err = ret < 0 ? errno : 0;
	return ret;
}

static long call_write(const char *path, const char *dest, int *err)
{
	char buffer[4096];
	char *end;
	unsigned long length = sizeof(buffer);
	int fd;
	long ret;

	if (dest && *dest) {
		unsigned long parsed = strtoul(dest, &end, 10);

		if (*end || parsed == 0 || parsed > sizeof(buffer)) {
			*err = EINVAL;
			return -1;
		}
		length = parsed;
	}
	memset(buffer, 'w', sizeof(buffer));
	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0) {
		*err = errno;
		return -1;
	}
	ret = syscall(SYS_write, fd, buffer, length);
	*err = ret < 0 ? errno : 0;
	if (close(fd) < 0 && ret >= 0) {
		ret = -1;
		*err = errno;
	}
	return ret;
}

static long call_rename(const char *path, const char *dest, int *err)
{
	long ret = syscall(SYS_renameat2, AT_FDCWD, path, AT_FDCWD, dest, 0);

	*err = ret < 0 ? errno : 0;
	return ret;
}

static long call_rename_dir(const char *path, const char *dest, int *err)
{
	long ret = syscall(SYS_renameat2, AT_FDCWD, path, AT_FDCWD, dest, 0);

	*err = ret < 0 ? errno : 0;
	return ret;
}

static long call_rename_exchange(const char *path, const char *dest, int *err)
{
	long ret = syscall(SYS_renameat2, AT_FDCWD, path, AT_FDCWD, dest,
			   RENAME_EXCHANGE);

	*err = ret < 0 ? errno : 0;
	return ret;
}

static long call_rename_whiteout(const char *path, const char *dest, int *err)
{
	long ret = syscall(SYS_renameat2, AT_FDCWD, path, AT_FDCWD, dest,
			   RENAME_WHITEOUT);

	*err = ret < 0 ? errno : 0;
	return ret;
}

static long call_alloc(const char *path, const char *dest, int *err)
{
	(void)dest;
	int fd = open(path, O_RDWR | O_CLOEXEC);
	long ret;

	if (fd < 0) {
		*err = errno;
		return -1;
	}
	ret = syscall(SYS_fallocate, fd, 0, 0, 4096);
	*err = ret < 0 ? errno : 0;
	close(fd);
	return ret;
}

static long call_extent_insert(const char *path, const char *dest, int *err)
{
	int fd = open(path, O_RDWR | O_CLOEXEC);
	long ret;
	char *end;
	long long offset = strtoll(dest, &end, 10);

	if (fd < 0) {
		*err = errno;
		return -1;
	}
	if (*dest == '\0' || *end != '\0' || offset < 0) {
		close(fd);
		*err = EINVAL;
		return -1;
	}
	ret = syscall(SYS_fallocate, fd, 0, (off_t)offset, 4096);
	*err = ret < 0 ? errno : 0;
	close(fd);
	return ret;
}

static long call_resize_fs(const char *path, const char *dest, int *err)
{
	int fd;
	char *end;
	unsigned long long blocks = strtoull(dest, &end, 10);
	long ret;

	if (*dest == '\0' || *end != '\0' || !blocks) {
		*err = EINVAL;
		return -1;
	}
	/* ext4_resize_fs requires a writable file reference. The fixture passes
	 * its pre-created regular file, avoiding an O_RDWR open on a directory.
	 */
	fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		*err = errno;
		return -1;
	}
	ret = syscall(SYS_ioctl, fd, EXT4_IOC_RESIZE_FS, &blocks);
	*err = ret < 0 ? errno : 0;
	close(fd);
	return ret;
}

static long call_migrate(const char *path, const char *dest, int *err)
{
	int fd;
	long ret;

	(void)dest;
	fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		*err = errno;
		return -1;
	}
	ret = ioctl(fd, EXT4_IOC_MIGRATE);
	*err = ret < 0 ? errno : 0;
	close(fd);
	return ret;
}

static long call_setxattr(const char *path, const char *dest, int *err)
{
	static const char name[] = "user.fault-fixture";
	unsigned char value[8192];
	size_t length = !strcmp(dest, "ea") ? 8192 :
		(!strcmp(dest, "large") ? 2048 : 32);
	size_t i;

	for (i = 0; i < sizeof(value); i++)
		value[i] = (unsigned char)('A' + (i % 23));
	if (setxattr(path, name, value, length, 0) < 0) {
		*err = errno;
		return -1;
	}
	*err = 0;
	return 0;
}

static long call_acl_mode(const char *path, const char *dest, int *err)
{
	/* POSIX ACL xattr v2 has an 8-byte xattr entry for every tag, including
	 * the four-byte undefined e_id on object, mask, and other entries.  This
	 * valid minimal ACL changes mode 0644 to 0600 through the mask, so
	 * ext4_set_acl() takes its update_mode branch and reaches acl_mode_dirty. */
	static const unsigned char acl[] = {
		0x02, 0x00, 0x00, 0x00,
		0x01, 0x00, 0x06, 0x00, 0xff, 0xff, 0xff, 0xff,
		0x04, 0x00, 0x04, 0x00, 0xff, 0xff, 0xff, 0xff,
		0x10, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0xff,
		0x20, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0xff,
	};
	const char name[] = "system.posix_acl_access";
	long ret;

	(void)dest;
	ret = setxattr(path, name, acl, sizeof(acl), 0);
	*err = ret < 0 ? errno : 0;
	return ret;
}

static long call_fileattr_flags(const char *path, const char *dest, int *err)
{
	int fd;
	long flags;
	long ret;

	(void)dest;
	fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		*err = errno;
		return -1;
	}
	if (ioctl(fd, FS_IOC_GETFLAGS, &flags) < 0) {
		*err = errno;
		close(fd);
		return -1;
	}
	flags ^= FS_NODUMP_FL;
	ret = ioctl(fd, FS_IOC_SETFLAGS, &flags);
	*err = ret < 0 ? errno : 0;
	close(fd);
	return ret;
}

static long call_fileattr_project(const char *path, const char *dest, int *err)
{
	int fd;
	long ret;
	struct fsxattr attr;

	(void)dest;
	fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		*err = errno;
		return -1;
	}
	memset(&attr, 0, sizeof(attr));
	if (ioctl(fd, FS_IOC_FSGETXATTR, &attr) < 0) {
		*err = errno;
		close(fd);
		return -1;
	}
	attr.fsx_projid = attr.fsx_projid == 123 ? 124 : 123;
	ret = ioctl(fd, FS_IOC_FSSETXATTR, &attr);
	*err = ret < 0 ? errno : 0;
	close(fd);
	return ret;
}

static long call_punch(const char *path, const char *dest, int *err)
{
	(void)dest;
	int fd = open(path, O_RDWR | O_CLOEXEC);
	long ret;

	if (fd < 0) {
		*err = errno;
		return -1;
	}
	ret = syscall(SYS_fallocate, fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
		      0, 1024 * 1024);
	*err = ret < 0 ? errno : 0;
	close(fd);
	return ret;
}

static long call_close(int fd, int *err)
{
	long ret = syscall(SYS_close, fd);

	*err = ret < 0 ? errno : 0;
	return ret;
}

static int prepare_action(const char *mount, const char *operation,
			  const char *name,
			 struct action *action)
{
	char dir[PATH_CAP];

	memset(action, 0, sizeof(*action));
	action->setup_fd = -1;
	action->setup_fd2 = -1;
	if (snprintf(dir, sizeof(dir), "%s/fixture", mount) >= (int)sizeof(dir) ||
	    ensure_dir(dir) < 0)
		return -1;
	if (make_path(action->target, sizeof(action->target), mount, "target") < 0 ||
	    make_path(action->source, sizeof(action->source), mount, "source") < 0 ||
	    make_path(action->destination, sizeof(action->destination), mount, "destination") < 0)
		return -1;
	action->name = name;
	action->operation = operation;
	if (!strcmp(name, "mkdir")) {
		/* Append allocation and directory-block reads are only reached after
		 * ext4 has to inspect a populated parent directory. */
		if (strstr(operation, "mkdir_append") ||
		    !strcmp(operation, "mkdir_add_entry_read")) {
			unsigned int entries = strstr(operation, "append") ? 220 : 24;

			if (populate_directory(dir, entries) < 0)
				return -1;
		}
		action->syscall_name = "mkdirat";
		action->syscall_nr = SYS_mkdirat;
		action->run = call_mkdir;
		return 0;
	}
	if (!strcmp(name, "rmdir")) {
		if (mkdir(action->target, 0755) < 0 && errno != EEXIST)
			return -1;
		action->syscall_name = "rmdir";
		action->syscall_nr = SYS_rmdir;
		action->run = call_rmdir;
		return 0;
	}
	if (!strcmp(name, "unlink")) {
		if (create_file(action->target, 1) < 0)
			return -1;
		action->syscall_name = "unlinkat";
		action->syscall_nr = SYS_unlinkat;
		action->run = call_unlink;
		return 0;
	}
	if (!strcmp(name, "migrate-extents")) {
		if (create_indirect_file(action->target) < 0)
			return -1;
		action->syscall_name = "ioctl(EXT4_IOC_MIGRATE)";
		action->syscall_nr = SYS_ioctl;
		action->run = call_migrate;
		return 0;
	}
	if (!strcmp(name, "xattr-set")) {
		if (create_file(action->target, 1) < 0)
			return -1;
		(void)snprintf(action->run_arg, sizeof(action->run_arg), "%s", "large");
		action->syscall_name = "setxattr";
		action->syscall_nr = SYS_setxattr;
		action->run = call_setxattr;
		return 0;
	}
	if (!strcmp(name, "ea-ref-set")) {
		if (create_ea_ref_fixture(action->target,
					  &action->xattr_block) < 0)
			return -1;
		(void)snprintf(action->run_arg, sizeof(action->run_arg), "%s", "ea");
		action->syscall_name = "setxattr";
		action->syscall_nr = SYS_setxattr;
		action->run = call_setxattr;
		return 0;
	}
	if (!strcmp(name, "acl-mode")) {
		if (create_file(action->target, 1) < 0)
			return -1;
		action->syscall_name = "setxattr(system.posix_acl_access)";
		action->syscall_nr = SYS_setxattr;
		action->run = call_acl_mode;
		return 0;
	}
	if (!strcmp(name, "inline-dir")) {
		char parent[PATH_CAP];

		if (snprintf(parent, sizeof(parent), "%s/fixture/inline-parent",
			     mount) >= (int)sizeof(parent) ||
		    create_inline_dir_fixture(parent, action->target) < 0)
			return -1;
		action->syscall_name = "mkdirat";
		action->syscall_nr = SYS_mkdirat;
		action->run = call_mkdir;
		return 0;
	}
	if (!strcmp(name, "inline-file-write")) {
		if (create_inline_file(action->target) < 0)
			return -1;
		if (snprintf(action->run_arg, sizeof(action->run_arg), "%s", "4096") >=
		    (int)sizeof(action->run_arg))
			return -1;
		action->syscall_name = "write";
		action->syscall_nr = SYS_write;
		action->run = call_write;
		return 0;
	}
	if (!strcmp(name, "fileattr-flags")) {
		if (create_file(action->target, 1) < 0)
			return -1;
		action->syscall_name = "ioctl(FS_IOC_SETFLAGS)";
		action->syscall_nr = SYS_ioctl;
		action->run = call_fileattr_flags;
		return 0;
	}
	if (!strcmp(name, "fileattr-project")) {
		if (create_file(action->target, 1) < 0)
			return -1;
		action->syscall_name = "ioctl(FS_IOC_FSSETXATTR)";
		action->syscall_nr = SYS_ioctl;
		action->run = call_fileattr_project;
		return 0;
	}
	if (!strcmp(name, "evict-close")) {
		if (create_file(action->target, 1) < 0)
			return -1;
		action->setup_fd = open(action->target, O_RDWR | O_CLOEXEC);
		if (action->setup_fd < 0 ||
		    capture_state_fd(action->setup_fd, &action->setup_state) < 0 ||
		    unlink(action->target) < 0)
			return -1;
		action->syscall_name = "close";
		action->syscall_nr = SYS_close;
		return 0;
	}
	if (!strcmp(name, "xattr-evict-close")) {
		if (create_shared_external_xattr(action->target,
						action->destination,
						&action->xattr_block) < 0)
			return -1;
		action->setup_fd = open(action->target, O_RDWR | O_CLOEXEC);
		if (action->setup_fd < 0 ||
		    capture_state_fd(action->setup_fd, &action->setup_state) < 0 ||
		    unlink(action->target) < 0)
			return -1;
		action->syscall_name = "close";
		action->syscall_nr = SYS_close;
		return 0;
	}
	if (!strcmp(name, "xattr-single-evict-close")) {
		if (create_single_external_xattr(action->target,
						&action->xattr_block) < 0)
			return -1;
		action->setup_fd = open(action->target, O_RDWR | O_CLOEXEC);
		if (action->setup_fd < 0 ||
		    capture_state_fd(action->setup_fd, &action->setup_state) < 0 ||
		    unlink(action->target) < 0)
			return -1;
		action->syscall_name = "close";
		action->syscall_nr = SYS_close;
		return 0;
	}
	if (!strcmp(name, "evict-close-prev")) {
		if (create_file(action->source, 1) < 0 ||
		    create_file(action->destination, 1) < 0)
			return -1;
		action->setup_fd = open(action->source, O_RDWR | O_CLOEXEC);
		action->setup_fd2 = open(action->destination, O_RDWR | O_CLOEXEC);
		if (action->setup_fd < 0 || action->setup_fd2 < 0 ||
		    capture_state_fd(action->setup_fd, &action->setup_state) < 0 ||
		    unlink(action->source) < 0 || unlink(action->destination) < 0)
			return -1;
		action->syscall_name = "close";
		action->syscall_nr = SYS_close;
		return 0;
	}
	if (!strcmp(name, "rename")) {
		/* Parent-dirty/access hooks are reached only when a directory crosses
		 * directory parents.  Replacement/setent hooks instead need an existing
		 * destination inode in the same namespace. */
		if (strstr(operation, "new_parent") || strstr(operation, "old_parent")) {
			char parent_a[PATH_CAP], parent_b[PATH_CAP];

			if (snprintf(parent_a, sizeof(parent_a), "%s/fixture/rename-a", mount) >=
				(int)sizeof(parent_a) ||
			    snprintf(parent_b, sizeof(parent_b), "%s/fixture/rename-b", mount) >=
				(int)sizeof(parent_b) || mkdir(parent_a, 0755) < 0 ||
			    mkdir(parent_b, 0755) < 0 ||
			    snprintf(action->source, sizeof(action->source), "%s/source-dir", parent_a) >=
				(int)sizeof(action->source) ||
			    snprintf(action->destination, sizeof(action->destination), "%s/moved-dir", parent_b) >=
				(int)sizeof(action->destination) || mkdir(action->source, 0755) < 0)
				return -1;
			/* The parent access/dirty hooks are directory-only paths. */
			action->syscall_name = "renameat2";
			action->syscall_nr = SYS_renameat2;
			action->run = call_rename;
			return 0;
		}
		if (create_file(action->source, 1) < 0)
			return -1;
		if (strstr(operation, "replaced_inode") ||
		    strstr(operation, "setent")) {
			if (create_file(action->destination, 1) < 0)
				return -1;
		}
		action->syscall_name = "renameat2";
		action->syscall_nr = SYS_renameat2;
		action->run = call_rename;
		return 0;
	}
	if (!strcmp(name, "rename-exchange")) {
		char parent_a[PATH_CAP], parent_b[PATH_CAP];

		/* ext4_cross_rename() updates parent directory link counts only for a
		 * cross-parent exchange involving a directory and a non-directory. */
		if (snprintf(parent_a, sizeof(parent_a), "%s/fixture/exchange-a", mount) >=
		    (int)sizeof(parent_a) ||
		    snprintf(parent_b, sizeof(parent_b), "%s/fixture/exchange-b", mount) >=
		    (int)sizeof(parent_b) || mkdir(parent_a, 0755) < 0 ||
		    mkdir(parent_b, 0755) < 0 ||
		    snprintf(action->source, sizeof(action->source), "%s/source-dir", parent_a) >=
		    (int)sizeof(action->source) ||
		    snprintf(action->destination, sizeof(action->destination), "%s/destination", parent_b) >=
		    (int)sizeof(action->destination) || mkdir(action->source, 0755) < 0 ||
		    create_file(action->destination, 1) < 0)
			return -1;
		action->syscall_name = "renameat2";
		action->syscall_nr = SYS_renameat2;
		action->run = call_rename_exchange;
		return 0;
	}
	if (!strcmp(name, "rename-replace")) {
		if (create_file(action->source, 1) < 0 ||
		    create_file(action->destination, 1) < 0)
			return -1;
		action->syscall_name = "renameat2";
		action->syscall_nr = SYS_renameat2;
		action->run = call_rename;
		return 0;
	}
	if (!strcmp(name, "rename-whiteout")) {
		if (create_file(action->source, 1) < 0)
			return -1;
		action->syscall_name = "renameat2";
		action->syscall_nr = SYS_renameat2;
		action->run = call_rename_whiteout;
		return 0;
	}
	if (!strcmp(name, "rename-dir") || !strcmp(name, "rename-dir-inline")) {
		char parent_a[PATH_CAP], parent_b[PATH_CAP];

		if (snprintf(parent_a, sizeof(parent_a), "%s/fixture/parent-a", mount) >=
			    (int)sizeof(parent_a) ||
		    snprintf(parent_b, sizeof(parent_b), "%s/fixture/parent-b", mount) >=
			    (int)sizeof(parent_b) ||
		    mkdir(parent_a, 0755) < 0 || mkdir(parent_b, 0755) < 0 ||
		    snprintf(action->source, sizeof(action->source), "%s/source-dir",
			     parent_a) >= (int)sizeof(action->source) ||
		    snprintf(action->destination, sizeof(action->destination), "%s/moved-dir",
			     parent_b) >= (int)sizeof(action->destination) ||
		    mkdir(action->source, 0755) < 0)
			return -1;
		action->syscall_name = "renameat2";
		action->syscall_nr = SYS_renameat2;
		action->run = call_rename_dir;
		return 0;
	}
	if (!strcmp(name, "extent-grow-depth") ||
	    !strcmp(name, "extent-split")) {
		unsigned int existing = !strcmp(name, "extent-grow-depth") ? 4 : 340;
		long long offset = (long long)existing * 2 * 4096;

		if (create_sparse_extent_count(action->target, existing) < 0)
			return -1;
		if (snprintf(action->run_arg, sizeof(action->run_arg), "%lld", offset) >=
		    (int)sizeof(action->run_arg))
			return -1;
		action->syscall_name = "fallocate";
		action->syscall_nr = SYS_fallocate;
		action->run = call_extent_insert;
		return 0;
	}
	if (!strcmp(name, "resize-extend") ||
	    !strcmp(name, "resize-add-group")) {
		unsigned long long target;
		unsigned long long increase = !strcmp(name, "resize-extend") ?
			4096 : 50000;

		/* run-fault-formula-single-worker.sh creates this fixture filesystem
		 * with exactly 40,000 blocks. statfs.f_blocks excludes ext4 metadata
		 * overhead and is not the absolute block count accepted by
		 * EXT4_IOC_RESIZE_FS.
		 */
		if (create_file(action->target, 0) < 0)
			return -1;
		target = 40000ULL + increase;
		if (snprintf(action->run_arg, sizeof(action->run_arg), "%llu",
			     target) >= (int)sizeof(action->run_arg))
			return -1;
		action->syscall_name = "ioctl(EXT4_IOC_RESIZE_FS)";
		action->syscall_nr = SYS_ioctl;
		action->run = call_resize_fs;
		return 0;
	}
	if (!strcmp(name, "alloc-mballoc")) {
		if (create_file(action->target, 0) < 0)
			return -1;
		action->syscall_name = "fallocate";
		action->syscall_nr = SYS_fallocate;
		action->run = call_alloc;
		return 0;
	}
	if (!strcmp(name, "free-punch")) {
		if (create_file(action->target, 1) < 0)
			return -1;
		action->syscall_name = "fallocate";
		action->syscall_nr = SYS_fallocate;
		action->run = call_punch;
		return 0;
	}
	return -1;
}

static long run_action(struct action *action, int *call_errno)
{
	if (!strcmp(action->name, "evict-close") ||
	    !strcmp(action->name, "xattr-evict-close") ||
	    !strcmp(action->name, "xattr-single-evict-close") ||
	    !strcmp(action->name, "evict-close-prev"))
		return call_close(action->setup_fd, call_errno);
	if (!strcmp(action->name, "rename") ||
	    !strcmp(action->name, "rename-exchange") ||
	    !strcmp(action->name, "rename-replace") ||
	    !strcmp(action->name, "rename-whiteout") ||
	    !strcmp(action->name, "rename-dir") ||
	    !strcmp(action->name, "rename-dir-inline"))
		return action->run(action->source, action->destination, call_errno);
	if (!strcmp(action->name, "extent-grow-depth") ||
	    !strcmp(action->name, "extent-split"))
		return action->run(action->target, action->run_arg, call_errno);
	if (!strcmp(action->name, "resize-extend") ||
	    !strcmp(action->name, "resize-add-group"))
		return action->run(action->target, action->run_arg, call_errno);
	if (!strcmp(action->name, "inline-file-write"))
		return action->run(action->target, action->run_arg, call_errno);
	if (!strcmp(action->name, "xattr-set") ||
	    !strcmp(action->name, "ea-ref-set"))
		return action->run(action->target, action->run_arg, call_errno);
	return action->run(action->target, action->destination, call_errno);
}

static void write_evidence(const char *path, const char *fault_id,
			   const char *operation, const struct action *action,
			   pid_t pid, int arm_rc, int arm_verified,
			   const char *status_before, const char *status_after,
			   int status_before_rc, int status_after_rc,
			   long result, int call_errno, int call_performed,
			   int disable_rc, int before_capture_ok,
			   int after_capture_ok, const struct file_state *before,
			   const struct file_state *after,
			   const struct file_state *source_before,
			   const struct file_state *source_after,
			   const struct file_state *dest_before,
				const struct file_state *dest_after,
				const struct resize_state *resize_before,
				const struct resize_state *resize_after)
{
	FILE *out = fopen(path, "w");

	if (!out)
		return;
	fputs("{\"schema\":2,", out);
	json_string(out, "fault_id", fault_id);
	fputc(',', out);
	json_string(out, "operation", operation);
	fputc(',', out);
	json_string(out, "action", action->name);
	fprintf(out, ",\"pid\":%ld,\"call_index\":0,\"syscall_nr\":%ld,"
		"\"xattr_block\":%llu,",
		(long)pid, action->syscall_nr, action->xattr_block);
	json_string(out, "syscall_name", action->syscall_name);
	fputc(',', out);
	json_string(out, "action_argument", action->run_arg);
	fprintf(out, ",\"arm_write_rc\":%d,\"arm_verified\":%s,"
		"\"status_before_read_rc\":%d,\"status_after_read_rc\":%d,",
		arm_rc, arm_verified ? "true" : "false",
		status_before_rc, status_after_rc);
	json_string(out, "status_before", status_before);
	fputc(',', out);
	json_string(out, "status_after", status_after);
	fprintf(out, ",\"call_performed\":%s,\"syscall_result\":%ld,"
		"\"syscall_errno\":%d,\"disable_rc\":%d,"
		"\"before_capture_ok\":%s,\"after_capture_ok\":%s,",
		call_performed ? "true" : "false", result, call_errno, disable_rc,
		before_capture_ok ? "true" : "false",
		after_capture_ok ? "true" : "false");
	write_state(out, "target_before", before);
	fputc(',', out);
	write_state(out, "target_after", after);
	fputc(',', out);
	write_state(out, "source_before", source_before);
	fputc(',', out);
	write_state(out, "source_after", source_after);
	fputc(',', out);
	write_state(out, "destination_before", dest_before);
	fputc(',', out);
	write_state(out, "destination_after", dest_after);
	fputc(',', out);
	write_resize_state(out, "resize_before", resize_before);
	fputc(',', out);
	write_resize_state(out, "resize_after", resize_after);
	fputs("}\n", out);
	fclose(out);
}

int main(int argc, char **argv)
{
	const char *fault_path = getenv("CT_FAULT_PATH");
	const char *fault_disabled_env = getenv("CT_FAULT_DISABLED");
	const char *status_path = getenv("CT_FAULT_STATUS");
	const char *evidence_path = argc > 5 ? argv[5] : getenv("CT_FAULT_RESULT");
	const char *fault_errno_env = getenv("CT_FAULT_ERRNO");
	const char *fault_id, *operation, *action_name, *mount;
	char config[1024];
	char status_before[STATUS_CAP] = "", status_after[STATUS_CAP] = "";
	struct action action;
	struct file_state before = {0}, after = {0};
	struct file_state source_before = {0}, source_after = {0};
	struct file_state dest_before = {0}, dest_after = {0};
	struct resize_state resize_before = {0}, resize_after = {0};
	int arm_rc, arm_verified, status_before_rc, status_after_rc;
	int control_mode = fault_disabled_env && !strcmp(fault_disabled_env, "1");
	int call_errno = 0, call_performed = 0, disable_rc;
	int before_capture_ok, after_capture_ok;
	long result = -1;
	pid_t pid = getpid();
	char *fault_errno_end;
	long parsed_fault_errno;

	if (argc == 3 && !strcmp(argv[1], "--inspect")) {
		struct file_state state;

		if (capture_state_path(argv[2], &state) < 0)
			return 1;
		fputc('{', stdout);
		write_state(stdout, "state", &state);
		fputs("}\n", stdout);
		return 0;
	}
	if (argc == 4 && !strcmp(argv[1], "--stable-snapshot")) {
		char *end;
		long require_healthy = strtol(argv[3], &end, 10);

		if (*argv[3] == '\0' || *end != '\0' ||
		    (require_healthy != 0 && require_healthy != 1))
			return 2;
		return capture_stable_snapshot(argv[2], (int)require_healthy) == 0 ?
			0 : 1;
	}
	if (argc < 5) {
		fprintf(stderr, "usage: %s fault-id operation action mount-path [evidence-file]\n",
			argv[0]);
		return 2;
	}
	fault_id = argv[1];
	operation = argv[2];
	action_name = argv[3];
	mount = argv[4];
	if ((!fault_path && !control_mode) || !status_path || !evidence_path ||
	    !getenv("CT_TRACE_MARKER"))
		return 3;
	if (fault_errno_env && *fault_errno_env) {
		parsed_fault_errno = strtol(fault_errno_env, &fault_errno_end, 0);
		if (*fault_errno_end || parsed_fault_errno >= 0 ||
		    parsed_fault_errno < -4095)
			return 3;
		configured_fault_errno = (int)parsed_fault_errno;
	}
	if (prctl(PR_SET_NAME, PROGRAM_NAME, 0, 0, 0) < 0)
		return 3;
	if (prepare_action(mount, operation, action_name, &action) < 0) {
		perror("prepare_action");
		return 4;
	}
	if (wait_for_snapshot_gate() < 0 ||
	    (getenv("CT_STABLE_BEFORE") &&
	     capture_stable_snapshot(getenv("CT_STABLE_BEFORE"), 1) < 0)) {
		perror("prepared_snapshot_gate");
		return 7;
	}
	before_capture_ok = capture_state_path(action.target, &before) == 0;
	if ((!strcmp(action.name, "resize-extend") ||
	     !strcmp(action.name, "resize-add-group")) &&
	    capture_resize_state(mount, &resize_before) < 0)
		before_capture_ok = 0;
	if (!strcmp(action.name, "evict-close") ||
	    !strcmp(action.name, "xattr-evict-close") ||
	    !strcmp(action.name, "xattr-single-evict-close") ||
	    !strcmp(action.name, "evict-close-prev"))
		before = action.setup_state;
	if (capture_state_path(action.source, &source_before) < 0 ||
	    capture_state_path(action.destination, &dest_before) < 0)
		before_capture_ok = 0;
	if (!before_capture_ok) {
		perror("capture_before_action_state");
		if (action.setup_fd >= 0)
			close(action.setup_fd);
		if (action.setup_fd2 >= 0)
			close(action.setup_fd2);
		return 8;
	}
	if (snprintf(config, sizeof(config),
		     "operation=%s errno=%d count=1 pid=%ld program=%s call_index=0\n",
		     operation, configured_fault_errno, (long)pid,
		     PROGRAM_NAME) >= (int)sizeof(config))
		return 5;
	if (control_mode) {
		arm_rc = 0;
		status_before_rc = 0;
		status_after_rc = 0;
		arm_verified = 0;
		marker("CT_FAULT_ARM", "ARM_BEGIN", fault_id, operation, pid,
		       "disabled");
		marker("CT_FAULT_ARM", "ARM_END", fault_id, operation, pid,
		       "disabled");
	}
	else {
		marker("CT_FAULT_ARM", "ARM_BEGIN", fault_id, operation, pid, "configuring");
		arm_rc = write_path(fault_path, config);
		status_before_rc = read_path(status_path, status_before,
					     sizeof(status_before));
		arm_verified = arm_rc == 0 && status_before_rc >= 0 &&
			       status_matches_arm(status_before, operation, pid,
						   configured_fault_errno);
		marker("CT_FAULT_ARM", "ARM_END", fault_id, operation, pid,
		       arm_verified ? "armed" : "arm_failed");
	}
	if (control_mode || arm_verified) {
		call_performed = 1;
		errno = 0;
		syscall_marker("BEGIN", fault_id, &action, 0, 0);
		result = run_action(&action, &call_errno);
		syscall_marker("END", fault_id, &action, result, call_errno);
		/* Capture formula terminal points in this PID after the action has
		 * returned.  The separate oracle envelope keeps its helper syscalls
		 * outside the target syscall's native entry/exit attribution.
		 */
		marker("CT_ORACLE", "BEGIN", fault_id, operation, pid, "sampling");
		if (getenv("CT_STABLE_ORACLE") &&
		    capture_stable_snapshot(getenv("CT_STABLE_ORACLE"), 0) < 0)
			fprintf(stderr, "oracle snapshot was not stable\n");
		marker("CT_ORACLE", "END", fault_id, operation, pid, "sampled");
		if (!strcmp(action.name, "evict-close") ||
		    !strcmp(action.name, "xattr-evict-close") ||
		    !strcmp(action.name, "xattr-single-evict-close") ||
		    !strcmp(action.name, "evict-close-prev"))
			action.setup_fd = -1;
		if (action.setup_fd2 >= 0) {
			close(action.setup_fd2);
			action.setup_fd2 = -1;
		}
		marker("CT_FAULT_FIXTURE", "ACTION_END", fault_id, operation, pid,
		       result < 0 ? "error" : "success");
	}
	if (!control_mode)
		status_after_rc = read_path(status_path, status_after,
					    sizeof(status_after));
	disable_rc = control_mode ? 0 : write_path(fault_path, "off\n");
	marker("CT_FAULT_FIXTURE", "DISABLE_END", fault_id, operation, pid,
	       disable_rc == 0 ? "off" : "disable_failed");
	after_capture_ok = capture_state_path(action.target, &after) == 0 &&
		capture_state_path(action.source, &source_after) == 0 &&
		capture_state_path(action.destination, &dest_after) == 0;
	if (after_capture_ok &&
	    (!strcmp(action.name, "resize-extend") ||
	     !strcmp(action.name, "resize-add-group")) &&
	    capture_resize_state(mount, &resize_after) < 0)
		after_capture_ok = 0;
	if (action.setup_fd >= 0)
		close(action.setup_fd);
	if (action.setup_fd2 >= 0)
		close(action.setup_fd2);
	write_evidence(evidence_path, fault_id, operation, &action, pid, arm_rc,
		       arm_verified, status_before, status_after,
		       status_before_rc, status_after_rc, result, call_errno,
			       call_performed, disable_rc, before_capture_ok,
			       after_capture_ok, &before, &after,
			       &source_before, &source_after, &dest_before, &dest_after,
			       &resize_before, &resize_after);
	return (control_mode || arm_verified) && call_performed && disable_rc == 0 ?
		0 : 6;
}
