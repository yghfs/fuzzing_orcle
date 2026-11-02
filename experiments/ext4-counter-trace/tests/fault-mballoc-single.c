/* Deterministic single-worker fixture for the ext4 mballoc_mark fault. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fiemap.h>
#include <linux/fs.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

#define PATH_MAX_LOCAL 512

struct file_state {
    long long size;
    long long blocks;
    unsigned long nlink;
    unsigned int extent_count;
    unsigned long long extent_hash;
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
    close(fd);
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

static int status_matches_arm(const char *status, pid_t pid)
{
    char expected_pid[64];

    snprintf(expected_pid, sizeof(expected_pid), "\"target_pid\":%ld", (long)pid);
    return strstr(status, "\"enabled\":true") &&
           strstr(status, "\"operation\":\"mballoc_mark\"") &&
           strstr(status, "\"errno\":-5") &&
           strstr(status, "\"remaining\":1") &&
           strstr(status, "\"injected\":0") &&
           strstr(status, expected_pid) &&
           strstr(status, "\"program\":\"fault-single\"") &&
           strstr(status, "\"call_index\":0") &&
           strstr(status, "\"generation\":");
}

static int capture_file_state(int fd, struct file_state *state)
{
    struct fiemap *map;
    struct stat st;
    unsigned long long hash = 1469598103934665603ULL;
    unsigned int i;

    if (fstat(fd, &st) < 0)
        return -1;
    map = calloc(1, sizeof(*map) + 16 * sizeof(struct fiemap_extent));
    if (!map)
        return -1;
    map->fm_start = 0;
    map->fm_length = FIEMAP_MAX_OFFSET;
    map->fm_flags = FIEMAP_FLAG_SYNC;
    map->fm_extent_count = 16;
    if (ioctl(fd, FS_IOC_FIEMAP, map) < 0) {
        free(map);
        return -1;
    }
    for (i = 0; i < map->fm_mapped_extents; i++) {
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
    state->size = (long long)st.st_size;
    state->blocks = (long long)st.st_blocks;
    state->nlink = (unsigned long)st.st_nlink;
    state->extent_count = map->fm_mapped_extents;
    state->extent_hash = hash;
    free(map);
    return 0;
}

static void marker(const char *phase, const char *status)
{
    const char *path = getenv("CT_TRACE_MARKER");
    const char *kind = strcmp(phase, "ARM_END") == 0 ? "CT_FAULT_ARM" :
                       "CT_FAULT_FIXTURE";
    char line[768];
    int fd, n;
    if (!path || !*path)
        return;
    fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0)
        return;
    n = snprintf(line, sizeof(line),
                 "%s phase=%s pid=%ld call_index=0 operation=mballoc_mark "
                 "errno=-5 program=fault-single status=%s\n",
                 kind, phase, (long)getpid(), status);
    if (n > 0 && n < (int)sizeof(line))
        (void)write_all(fd, line);
    close(fd);
}

static void syscall_marker(const char *phase, long result, int call_errno)
{
    const char *path = getenv("CT_TRACE_MARKER");
    char line[768];
    int fd, n;
    if (!path || !*path)
        return;
    fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0)
        return;
    n = snprintf(line, sizeof(line),
                 "CT_SYSCALL phase=%s id=fault-single-%ld-1 nr=285 "
                 "name=fallocate program=fault-single call_index=0 "
                 "ret=%ld errno=%d\n",
                 phase, (long)getpid(), result, call_errno);
    if (n > 0 && n < (int)sizeof(line))
        (void)write_all(fd, line);
    close(fd);
}

static void json_string(FILE *out, const char *key, const char *value)
{
    char escaped[4096];
    size_t j = 0;
    for (size_t i = 0; value && value[i] && j + 2 < sizeof(escaped); i++) {
        unsigned char c = (unsigned char)value[i];
        if (c == '"' || c == '\\')
            escaped[j++] = '\\';
        if (c == '\n') {
            escaped[j++] = '\\';
            escaped[j++] = 'n';
        } else if (c >= 0x20) {
            escaped[j++] = (char)c;
        }
    }
    escaped[j] = '\0';
    fprintf(out, "\"%s\":\"%s\"", key, escaped);
}

static void write_file_state(FILE *out, const char *key,
                             const struct file_state *state)
{
    fprintf(out, "\"%s\":{\"size\":%lld,\"blocks\":%lld,\"nlink\":%lu,"
                 "\"extent_count\":%u,\"extent_hash\":%llu}",
            key, state->size, state->blocks, state->nlink,
            state->extent_count, state->extent_hash);
}

int main(int argc, char **argv)
{
    const char *mount_dir = argc > 1 ? argv[1] : "/mnt/fault";
    const char *fault_path = getenv("CT_FAULT_PATH");
    const char *status_path = getenv("CT_FAULT_STATUS");
    const char *evidence_path = getenv("CT_FAULT_RESULT");
    char target[PATH_MAX_LOCAL];
    char before[4096] = "";
    char after[4096] = "";
    char config[768];
    FILE *out;
    int fd, arm_rc, before_rc, after_rc, disable_rc, arm_verified;
    int call_errno = 0, call_performed = 0, file_before_rc = -1, file_after_rc = -1;
    long call_rc = -1;
    struct file_state file_before = {0}, file_after = {0};

    if (argc == 3 && !strcmp(argv[1], "--inspect")) {
        struct file_state inspected = {0};
        int inspect_fd = open(argv[2], O_RDONLY | O_CLOEXEC);
        int inspect_rc;

        if (inspect_fd < 0)
            return 94;
        inspect_rc = capture_file_state(inspect_fd, &inspected);
        close(inspect_fd);
        if (inspect_rc < 0)
            return 95;
        fputc('{', stdout);
        write_file_state(stdout, "state", &inspected);
        fputs("}\n", stdout);
        return ferror(stdout) ? 96 : 0;
    }

    if (!fault_path || !status_path || !evidence_path || !getenv("CT_TRACE_MARKER"))
        return 90;
    if (snprintf(target, sizeof(target), "%s/fixture/target", mount_dir) >=
        (int)sizeof(target))
        return 90;
    fd = open(target, O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return 91;
    if (snprintf(config, sizeof(config),
                 "operation=mballoc_mark errno=-EIO count=1 pid=%ld "
                 "program=fault-single call_index=0\n",
                 (long)getpid()) >= (int)sizeof(config)) {
        close(fd);
        return 90;
    }
    marker("ARM_BEGIN", "configured");
    arm_rc = write_path(fault_path, config);
    before_rc = read_path(status_path, before, sizeof(before));
    arm_verified = arm_rc == 0 && before_rc >= 0 &&
                   status_matches_arm(before, getpid());
    marker("ARM_END", arm_verified ? "armed" : "arm_failed");
    if (arm_verified) {
        file_before_rc = capture_file_state(fd, &file_before);
        if (file_before_rc == 0) {
            errno = 0;
            syscall_marker("BEGIN", 0, 0);
            call_performed = 1;
            call_rc = fallocate(fd, 0, 0, 4096);
            call_errno = call_rc < 0 ? errno : 0;
            syscall_marker("END", call_rc, call_errno);
            marker("CALL_END", call_rc < 0 ? "error" : "success");
            file_after_rc = capture_file_state(fd, &file_after);
        } else {
            call_errno = EIO;
        }
    } else {
        call_errno = EAGAIN;
    }
    after_rc = read_path(status_path, after, sizeof(after));
    disable_rc = write_path(fault_path, "off\n");
    marker("DISABLE_END", disable_rc == 0 ? "off" : "disable_failed");
    close(fd);

    out = fopen(evidence_path, "w");
    if (!out)
        return 92;
    fprintf(out, "{\n\"schema\":1,\n\"fixture\":\"fault-mballoc-single\",\n");
    fprintf(out, "\"pid\":%ld,\n\"call_index\":0,\n", (long)getpid());
    json_string(out, "operation", "mballoc_mark");
    fprintf(out, ",\n\"expected_errno\":5,\"arm_write_rc\":%d,\n", arm_rc);
    fprintf(out, "\"arm_verified\":%s,\"call_performed\":%s,\n",
            arm_verified ? "true" : "false", call_performed ? "true" : "false");
    fprintf(out, "\"status_before_read_rc\":%d,\n", before_rc);
    json_string(out, "status_before", before);
    fprintf(out, ",\n\"syscall_rc\":%ld,\"syscall_errno\":%d,\n", call_rc, call_errno);
    fprintf(out, "\"status_after_read_rc\":%d,\n", after_rc);
    json_string(out, "status_after", after);
    fprintf(out, ",\n\"file_before_rc\":%d,\"file_after_rc\":%d,\n",
            file_before_rc, file_after_rc);
    if (file_before_rc == 0)
        write_file_state(out, "target_state_before", &file_before);
    else
        fprintf(out, "\"target_state_before\":null");
    fputs(",\n", out);
    if (file_after_rc == 0)
        write_file_state(out, "target_state_after", &file_after);
    else
        fprintf(out, "\"target_state_after\":null");
    fputs(",\n", out);
    fprintf(out, "\"disable_rc\":%d,\"return_matches_expected\":%s,\n",
            disable_rc, call_performed && call_rc == -1 && call_errno == EIO ? "true" : "false");
    fprintf(out, "\"status_payload_present\":%s\n}\n",
            before_rc >= 0 && after_rc >= 0 ? "true" : "false");
    fclose(out);
    return arm_verified && call_performed ? 0 : 93;
}
