#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/fs.h>
#include <linux/fsmap.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *mountpoint = argc > 1 ? argv[1] : "/";
	unsigned long mode = argc > 2 ? strtoul(argv[2], NULL, 0) : 0;
	struct fsmap_head *head;
	size_t size = fsmap_sizeof(0);
	int fd, rc;

	head = calloc(1, size);
	if (!head)
		return 1;
	head->fmh_count = 0;
	head->fmh_keys[1].fmr_device = UINT32_MAX;
	head->fmh_keys[1].fmr_physical = UINT64_MAX;
	head->fmh_keys[1].fmr_owner = UINT64_MAX;
	head->fmh_keys[1].fmr_offset = UINT64_MAX;
	head->fmh_keys[1].fmr_length = UINT64_MAX;
	if (mode) {
		head->fmh_keys[0].fmr_physical = UINT64_MAX;
		head->fmh_keys[0].fmr_length = UINT64_MAX;
		head->fmh_keys[0].fmr_owner = UINT64_MAX;
	}
	fd = open(mountpoint, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0) {
		perror("open mountpoint");
		free(head);
		return 1;
	}
	rc = ioctl(fd, FS_IOC_GETFSMAP, head);
	printf("mode=%lu rc=%d errno=%d (%s) entries=%u flags=0x%x\n",
	       mode, rc, errno, strerror(errno), head->fmh_entries,
	       head->fmh_oflags);
	close(fd);
	free(head);
	return rc < 0 ? 2 : 0;
}
