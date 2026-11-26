// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <linux/fsmap.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

static void require(int ok, const char *what)
{
	if (!ok) {
		perror(what);
		exit(1);
	}
}

static void control(const char *trace, const char *name, const char *value)
{
	char path[4096];
	int fd;
	size_t length = strlen(value);

	require(snprintf(path, sizeof(path), "%s/%s", trace, name) < (int)sizeof(path), "trace path");
	fd = open(path, O_WRONLY);
	require(fd >= 0, path);
	require(write(fd, value, length) == (ssize_t)length, path);
	require(!close(fd), "close trace control");
}

int main(int argc, char **argv)
{
	char data[8192] = { 1 }, filter[80], name[80];
	int fd, dirfd, initial_dir_flags = 0, file_flags = 0;
	struct stat small_stat;
	DIR *dir;

	require(argc == 3, "usage: error-site-workload MOUNT TRACEFS");
	require(!chdir(argv[1]), "chdir target mount");
	snprintf(filter, sizeof(filter), "common_pid == %ld", (long)getpid());
	control(argv[2], "events/ext4_error/error_site/filter", filter);
	control(argv[2], "tracing_on", "1");

	require(!mkdir("es-run", 0700), "mkdir");
	dirfd = open("es-run", O_RDONLY | O_DIRECTORY);
	require(dirfd >= 0, "open initial dir");
	require(!ioctl(dirfd, FS_IOC_GETFLAGS, &initial_dir_flags), "get dir flags");
	require(!close(dirfd), "close initial dir");
	/* Grow an inline directory beyond its initial 56-byte dirent area. */
	for (int index = 0; index < 8; index++) {
		snprintf(name, sizeof(name), "es-run/entry-%02d", index);
		fd = open(name, O_CREAT | O_EXCL | O_RDWR, 0600);
		require(fd >= 0, "create inline directory entry");
		require(!close(fd), "close inline directory entry");
	}
	fd = open("es-run/data", O_CREAT | O_EXCL | O_RDWR, 0600);
	require(fd >= 0, "create file");
	/* This small write also exercises the inline_data filesystem variant. */
	require(write(fd, data, 32) == 32, "small write");
	require(!fsync(fd), "small fsync");
	require(write(fd, data, 64) == 64, "grow inline data");
	require(!fsync(fd), "grown inline fsync");
	require(!ioctl(fd, FS_IOC_GETFLAGS, &file_flags), "get file flags");
	require(!fstat(fd, &small_stat), "stat small file");
	require(write(fd, data, sizeof(data)) == sizeof(data), "write");
	require(!fsetxattr(fd, "user.error-site", data, 1024, 0), "setxattr");
	require(fgetxattr(fd, "user.error-site", data, sizeof(data)) == 1024, "getxattr");
	require(!fremovexattr(fd, "user.error-site"), "removexattr");
	require(!fallocate(fd, 0, 0, 32768), "fallocate");
	require(!ftruncate(fd, 16384), "truncate");
	require(lseek(fd, 0, SEEK_DATA) >= 0, "seek data");
	require(lseek(fd, 0, SEEK_HOLE) >= 0, "seek hole");
	require(!fsync(fd), "fsync");
	/* Natural copy_from_user failure: its return is positive, not -errno. */
	errno = 0;
	require(ioctl(fd, FS_IOC_GETFSMAP, (void *)1) == -1 && errno == EFAULT,
		"GETFSMAP invalid userspace pointer");
	require(!close(fd), "close file");
	require(!link("es-run/data", "es-run/hard"), "link");
	require(!rename("es-run/data", "es-run/renamed"), "rename");
	require(!symlink("renamed", "es-run/sym"), "symlink");
	require(readlink("es-run/sym", data, sizeof(data)) == 7, "readlink");
	dir = opendir("es-run");
	require(dir != NULL, "opendir");
	errno = 0;
	while (readdir(dir))
		;
	require(!errno, "readdir");
	require(!closedir(dir), "closedir");
	require(!unlink("es-run/sym"), "unlink symlink");
	require(!unlink("es-run/hard"), "unlink hardlink");
	require(!unlink("es-run/renamed"), "unlink file");
	for (int index = 0; index < 8; index++) {
		snprintf(name, sizeof(name), "es-run/entry-%02d", index);
		require(!unlink(name), "unlink inline directory entry");
	}
	require(!rmdir("es-run"), "rmdir");
	dirfd = open(".", O_RDONLY | O_DIRECTORY);
	require(dirfd >= 0, "open root dir");
	require(!fsync(dirfd), "fsync root dir");
	require(!close(dirfd), "close root dir");
	control(argv[2], "tracing_on", "0");
	printf("workload_pid=%ld\ninitial_dir_flags=0x%x\nsmall_file_flags=0x%x\nsmall_file_blocks=%lld\nworkload=passed\n",
	       (long)getpid(), initial_dir_flags, file_flags, (long long)small_stat.st_blocks);
	return 0;
}
