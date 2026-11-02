// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/userfaultfd.h>
#include <pthread.h>
#include <poll.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>
#include <linux/fs.h>

struct writer_args {
	int fd;
	void *page;
	ssize_t result;
	int error;
};

static void *writer(void *data)
{
	struct writer_args *args = data;

	args->result = pwrite(args->fd, args->page, 4096, 0);
	args->error = errno;
	return NULL;
}

int main(int argc, char **argv)
{
	struct uffdio_api api = { .api = UFFD_API };
	struct uffdio_register reg;
	struct uffd_msg msg;
	struct pollfd pollfd;
	struct uffdio_copy copy;
	struct writer_args args;
	pthread_t thread;
	char path[512];
	char *source;
	void *page;
	int uffd, fd, devfd, readonly = 1;
	ssize_t got;

	if (argc != 3) {
		fprintf(stderr, "usage: %s DIR BLOCK_DEVICE\n", argv[0]);
		return 2;
	}
	if (mkdir(argv[1], 0700) < 0 && errno != EEXIST) {
		perror("mkdir");
		return 1;
	}
	snprintf(path, sizeof(path), "%s/uffd-dio", argv[1]);
	fd = open(path, O_CREAT | O_TRUNC | O_RDWR | O_DIRECT | O_CLOEXEC, 0600);
	if (fd < 0) {
		perror("open DIO file");
		return 1;
	}
	devfd = open(argv[2], O_RDONLY | O_CLOEXEC);
	if (devfd < 0) {
		perror("open block device");
		return 1;
	}
	uffd = syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK);
	if (uffd < 0) {
		perror("userfaultfd");
		return 1;
	}
	if (ioctl(uffd, UFFDIO_API, &api) < 0) {
		perror("UFFDIO_API");
		return 1;
	}
	page = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (page == MAP_FAILED) {
		perror("mmap page");
		return 1;
	}
	reg.range.start = (unsigned long)page;
	reg.range.len = 4096;
	reg.mode = UFFDIO_REGISTER_MODE_MISSING;
	if (ioctl(uffd, UFFDIO_REGISTER, &reg) < 0) {
		perror("UFFDIO_REGISTER");
		return 1;
	}
	if (posix_memalign((void **)&source, 4096, 4096)) {
		fprintf(stderr, "allocate source page failed\n");
		return 1;
	}
	memset(source, 0x5a, 4096);
	args.fd = fd;
	args.page = page;
	args.result = -1;
	args.error = 0;
	if (pthread_create(&thread, NULL, writer, &args)) {
		perror("pthread_create");
		return 1;
	}
	pollfd.fd = uffd;
	pollfd.events = POLLIN;
	if (poll(&pollfd, 1, 5000) <= 0) {
		pthread_join(thread, NULL);
		printf("no_userfault_event dio_rc=%zd errno=%d (%s)\n",
		       args.result, args.error, strerror(args.error));
		return 1;
	}
	got = read(uffd, &msg, sizeof(msg));
	if (got != sizeof(msg) || msg.event != UFFD_EVENT_PAGEFAULT) {
		fprintf(stderr, "unexpected userfault event got=%zd event=%u\n",
			got, got == sizeof(msg) ? msg.event : 0);
		return 1;
	}
	printf("dio_fault_paused address=%llx\n",
	       (unsigned long long)msg.arg.pagefault.address);
	if (ioctl(devfd, BLKROSET, &readonly) < 0) {
		perror("BLKROSET");
		return 1;
	}
	copy.dst = (unsigned long)page;
	copy.src = (unsigned long)source;
	copy.len = 4096;
	copy.mode = 0;
	copy.copy = 0;
	if (ioctl(uffd, UFFDIO_COPY, &copy) < 0) {
		perror("UFFDIO_COPY");
		return 1;
	}
	if (pthread_join(thread, NULL)) {
		perror("pthread_join");
		return 1;
	}
	printf("dio_after_readonly rc=%zd errno=%d (%s)\n", args.result,
	       args.result < 0 ? args.error : 0,
	       args.result < 0 ? strerror(args.error) : "ok");
	fsync(fd);
	close(fd);
	close(devfd);
	close(uffd);
	free(source);
	munmap(page, 4096);
	return 0;
}
