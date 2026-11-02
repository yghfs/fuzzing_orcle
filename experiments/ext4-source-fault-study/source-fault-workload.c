#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/ext4.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>
#ifndef RENAME_EXCHANGE
#define RENAME_EXCHANGE (1 << 1)
#endif
static int report(const char *op, int rc)
{
	int e = rc < 0 ? errno : 0;
	printf("WORKLOAD_RESULT op=%s rc=%d errno=%d (%s)\n", op, rc, e,
	       e ? strerror(e) : "none");
	return rc < 0 ? 1 : 0;
}
static void parent_path(const char *path, char *parent, size_t size)
{
	char *slash;
	snprintf(parent, size, "%s", path);
	slash = strrchr(parent, '/');
	if (!slash || slash == parent)
		snprintf(parent, size, "/");
	else
		*slash = '\0';
}
static const char *base_name(const char *path)
{
	const char *slash = strrchr(path, '/');
	return slash ? slash + 1 : path;
}
static void scan_dir(const char *path, unsigned long ino, const char *name,
		     unsigned int *ino_refs, unsigned int *name_refs,
		     unsigned int *subdirs)
{
	DIR *dir = opendir(path);
	struct dirent *de;
	*ino_refs = 0;
	*name_refs = 0;
	*subdirs = 0;
	if (!dir)
		return;
	while ((de = readdir(dir))) {
		if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, ".."))
			continue;
		if (ino && de->d_ino == ino)
			++*ino_refs;
		if (name && !strcmp(de->d_name, name))
			++*name_refs;
		if (de->d_type == DT_DIR)
			++*subdirs;
	}
	closedir(dir);
}
int main(int argc, char **argv)
{
	int rc, saved_errno;
	if (argc < 2)
		return 2;
	if (!strcmp(argv[1], "mkdir") && argc == 3) {
		char parent[512];
		struct stat st;
		unsigned int refs, names, dirs;
		rc = mkdir(argv[2], 0755);
		saved_errno = rc < 0 ? errno : 0;
		if (rc < 0) {
			parent_path(argv[2], parent, sizeof(parent));
			scan_dir(parent, 0, base_name(argv[2]), &refs, &names, &dirs);
			if (stat(parent, &st) == 0)
				printf("DIRLINK_RESULT op=mkdir parent_nlink=%lu immediate_subdirs=%u expected_nlink=%u child_dirent_refs=%u\n",
				       (unsigned long)st.st_nlink, dirs, 2 + dirs, names);
		}
		if (rc < 0)
			errno = saved_errno;
		return report("mkdir", rc);
	}
	if (!strcmp(argv[1], "rmdir") && argc == 3) {
		char parent[512];
		struct stat st, child;
		unsigned int refs, names, dirs;
		int fd = open(argv[2], O_RDONLY | O_DIRECTORY);
		unsigned long ino = fd >= 0 && fstat(fd, &child) == 0 ? child.st_ino : 0;
		rc = rmdir(argv[2]);
		saved_errno = rc < 0 ? errno : 0;
		if (rc < 0) {
			parent_path(argv[2], parent, sizeof(parent));
			scan_dir(parent, ino, base_name(argv[2]), &refs, &names, &dirs);
			if (stat(parent, &st) == 0)
				printf("DIRLINK_RESULT op=rmdir parent_nlink=%lu immediate_subdirs=%u expected_nlink=%u child_inode=%lu child_dirent_refs=%u child_name_entries=%u child_exists_by_dcache=%d\n",
				       (unsigned long)st.st_nlink, dirs, 2 + dirs, ino,
				       refs, names, access(argv[2], F_OK) == 0);
		}
		if (fd >= 0)
			close(fd);
		if (rc < 0)
			errno = saved_errno;
		return report("rmdir", rc);
	}
	if (!strcmp(argv[1], "rmdir-plain") && argc == 3)
		return report("rmdir", rmdir(argv[2]));
	if (!strcmp(argv[1], "unlink") && argc == 3) {
		char parent[512];
		struct stat st;
		unsigned int refs, names, dirs;
		int fd = open(argv[2], O_RDONLY);
		unsigned long ino = fd >= 0 && fstat(fd, &st) == 0 ? st.st_ino : 0;
		rc = unlink(argv[2]);
		saved_errno = rc < 0 ? errno : 0;
		if (rc < 0 && fd >= 0) {
			parent_path(argv[2], parent, sizeof(parent));
			scan_dir(parent, ino, base_name(argv[2]), &refs, &names, &dirs);
			if (fstat(fd, &st) == 0)
				printf("LINK_RESULT op=unlink inode=%lu nlink=%lu parent_dirent_refs=%u basename_entries=%u path_exists_by_dcache=%d expected_nlink=0\n",
				       (unsigned long)st.st_ino,
				       (unsigned long)st.st_nlink, refs, names,
				       access(argv[2], F_OK) == 0);
		}
		if (fd >= 0)
			close(fd);
		if (rc < 0)
			errno = saved_errno;
		return report("unlink", rc);
	}
	if (!strcmp(argv[1], "rename") && argc == 4) {
		char parent[512];
		struct stat st;
		unsigned int refs, old_entries, dirs;
		int fd = open(argv[2], O_RDONLY);
		unsigned long ino = fd >= 0 && fstat(fd, &st) == 0 ? st.st_ino : 0;
		rc = rename(argv[2], argv[3]);
		saved_errno = rc < 0 ? errno : 0;
		if (rc < 0) {
			parent_path(argv[2], parent, sizeof(parent));
			scan_dir(parent, ino, base_name(argv[2]), &refs, &old_entries, &dirs);
			printf("RENAME_RESULT op=rename inode=%lu nlink=%lu parent_dirent_refs=%u old_name_entries=%u new_name_dcache=%d expected_total_refs=1\n",
			       ino, fd >= 0 && fstat(fd, &st) == 0 ? (unsigned long)st.st_nlink : 0,
			       refs, old_entries, access(argv[3], F_OK) == 0);
		}
		if (fd >= 0)
			close(fd);
		if (rc < 0)
			errno = saved_errno;
		return report("rename", rc);
	}
	if (!strcmp(argv[1], "rename-plain") && argc == 4)
		return report("rename", rename(argv[2], argv[3]));
	if (!strcmp(argv[1], "exchange") && argc == 4)
		return report("renameat2_exchange",
			syscall(SYS_renameat2, AT_FDCWD, argv[2], AT_FDCWD,
				argv[3], RENAME_EXCHANGE));
	if (!strcmp(argv[1], "xattr") && argc == 4) {
		static const char value[256] = "same-value";
		if (setxattr(argv[2], "user.shared", value, sizeof(value), 0) < 0)
			return report("setxattr_first", -1);
		if (setxattr(argv[3], "user.shared", value, sizeof(value), 0) < 0)
			return report("setxattr_second", -1);
		printf("WORKLOAD_RESULT op=setxattr_pair rc=0 errno=0 (none)\n");
		return 0;
	}
	if ((!strcmp(argv[1], "group-extend") || !strcmp(argv[1], "group-add")) && argc >= 3) {
		struct statfs before, after;
		unsigned long blocks = strtoul(argv[2], NULL, 0);
		int fd = open("/mnt", O_RDONLY | O_DIRECTORY);
		if (fd < 0)
			return report("resize_open", -1);
		statfs("/mnt", &before);
		if (!strcmp(argv[1], "group-extend"))
			rc = ioctl(fd, EXT4_IOC_GROUP_EXTEND, &blocks);
		else {
			struct ext4_new_group_input in = { 0 };
			if (argc != 7)
				return 2;
			in.group = strtoul(argv[2], NULL, 0);
			in.block_bitmap = strtoull(argv[3], NULL, 0);
			in.inode_bitmap = strtoull(argv[4], NULL, 0);
			in.inode_table = strtoull(argv[5], NULL, 0);
			in.blocks_count = strtoul(argv[6], NULL, 0);
			rc = ioctl(fd, EXT4_IOC_GROUP_ADD, &in);
		}
		saved_errno = rc < 0 ? errno : 0;
		statfs("/mnt", &after);
		printf("RESIZE_RESULT op=%s before_blocks=%lu after_blocks=%lu before_bfree=%lu after_bfree=%lu before_files=%lu after_files=%lu\n",
		       argv[1], (unsigned long)before.f_blocks,
		       (unsigned long)after.f_blocks,
		       (unsigned long)before.f_bfree,
		       (unsigned long)after.f_bfree,
		       (unsigned long)before.f_files,
		       (unsigned long)after.f_files);
		close(fd);
		if (rc < 0)
			errno = saved_errno;
		return report(argv[1], rc);
	}
	return 2;
}
