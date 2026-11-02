// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <unistd.h>

#define TASK_COMM "fc-replay"

int main(int argc, char **argv)
{
	char options[128];
	pid_t pid = getpid();
	int rc, saved_errno;

	if (argc != 4) {
		fprintf(stderr, "usage: %s DEVICE MOUNTPOINT OPERATION\n", argv[0]);
		return 2;
	}
	if (strcmp(argv[3], "fast_commit_inode_claim")) {
		fprintf(stderr, "unsupported fault operation: %s\n", argv[3]);
		return 2;
	}
	if (prctl(PR_SET_NAME, TASK_COMM, 0, 0, 0) < 0) {
		perror("prctl(PR_SET_NAME)");
		return 1;
	}
	if (snprintf(options, sizeof(options),
		     "fault_prearm=%s:-EIO:1:%ld:%s:0",
		     argv[3], (long)pid, TASK_COMM) >= (int)sizeof(options)) {
		fprintf(stderr, "fault_prearm option is too long\n");
		return 2;
	}
	errno = 0;
	rc = mount(argv[1], argv[2], "ext4", 0, options);
	saved_errno = errno;
	printf("{\"pid\":%ld,\"comm\":\"%s\",\"operation\":\"%s\","
	       "\"options\":\"%s\",\"mount_rc\":%d,\"errno\":%d}\n",
	       (long)pid, TASK_COMM, argv[3], options, rc, rc < 0 ? saved_errno : 0);
	return rc < 0 ? 1 : 0;
}
