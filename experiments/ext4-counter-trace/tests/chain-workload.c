#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <linux/fs.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int marker;
static unsigned long sequence;
static int checkpoints;
static void checkpoint(const char *stage)
{
    if (!checkpoints) return;
    const char *path = getenv("CT_SNAPSHOT_PATH");
    const char *prefix = getenv("CT_RESULT_PREFIX");
    if (!path || !prefix) { fprintf(stderr,"snapshot environment missing\n"); exit(2); }
    if (!strcmp(stage,"inline-live") || !strcmp(stage,"inline-converted")) {
        unsigned long flags=0;
        int dir=open("p/inline",O_RDONLY|O_DIRECTORY);
        if (dir<0 || ioctl(dir,FS_IOC_GETFLAGS,&flags)<0) { perror("directory flags"); exit(2); }
        close(dir);
        printf("checkpoint=%s inode_flags=%lu inline=%d\n",stage,flags,!!(flags & FS_INLINE_DATA_FL));
        fflush(stdout);
        if (getenv("CT_EXPECT_INLINE") && ((!strcmp(stage,"inline-live") && !(flags & FS_INLINE_DATA_FL)) ||
            (!strcmp(stage,"inline-converted") && (flags & FS_INLINE_DATA_FL)))) {
            fprintf(stderr,"inline layout checkpoint disagrees\n"); exit(1);
        }
    }
    char data[8192], output[4096];
    for (int i=0;i<100;i++) {
        int fd=open(path,O_RDONLY);
        if (fd<0) { perror(path); exit(2); }
        ssize_t size=read(fd,data,sizeof(data)-1);
        close(fd);
        if (size<0) { perror("snapshot read"); exit(2); }
        data[size]=0;
        if (strstr(data,"\"valid\":true") && strstr(data,"\"namespace_valid\":true")) {
            snprintf(output,sizeof(output),"%s-%s.json",prefix,stage);
            fd=open(output,O_CREAT|O_WRONLY|O_TRUNC,0600);
            if (fd<0 || write(fd,data,size)!=size) { perror("snapshot save"); exit(2); }
            close(fd); return;
        }
        usleep(50000);
    }
    fprintf(stderr,"checkpoint failed: %s\n",stage); exit(1);
}
static void emit(const char *phase, unsigned long id, long nr, const char *name,
                 long ret, int error)
{
    char text[256];
    int n = snprintf(text, sizeof(text),
        "CT_SYSCALL phase=%s id=%s-%ld-%lu nr=%ld name=%s ret=%ld errno=%d\n",
        phase, getenv("CT_CALL_PREFIX") ? getenv("CT_CALL_PREFIX") : "finite",
        (long)getpid(), id, nr, name, ret, error);
    if (write(marker, text, n) != n) { perror("trace_marker"); exit(2); }
}
static long call(const char *name, long nr, long a, long b, long c, long d, long e, long f)
{
    unsigned long id = ++sequence;
    emit("BEGIN", id, nr, name, 0, 0);
    errno = 0;
    long ret = syscall(nr, a, b, c, d, e, f);
    int error = ret < 0 ? errno : 0;
    emit("END", id, nr, name, ret, error);
    printf("id=%ld-%lu name=%s nr=%ld ret=%ld errno=%d\n", (long)getpid(), id, name, nr, ret, error);
    fflush(stdout);
    errno = error;
    return ret;
}
#define A(x) ((long)(x))
#define CALL(name,nr,a,b,c,d,e,f) call(name,nr,A(a),A(b),A(c),A(d),A(e),A(f))
static long require(long ret, const char *what)
{
    if (ret < 0) { perror(what); exit(1); }
    return ret;
}
static void mkdir_(const char *path) { require(CALL("mkdir", SYS_mkdir, path,0700,0,0,0,0), path); }
static void unlink_(const char *path) { require(CALL("unlink",SYS_unlink,path,0,0,0,0,0),path); }
static void rmdir_(const char *path) { require(CALL("rmdir",SYS_rmdir,path,0,0,0,0,0),path); }
static int open_(const char *path) { return require(CALL("openat",SYS_openat,AT_FDCWD,path,O_CREAT|O_RDWR|O_TRUNC,0600,0,0),path); }
static void close_(int fd) { require(CALL("close",SYS_close,fd,0,0,0,0,0),"close"); }
static void rename_(const char *old, const char *new, unsigned flags)
{
    require(CALL("renameat2",SYS_renameat2,AT_FDCWD,old,AT_FDCWD,new,flags,0),"renameat2");
}
static void normal(const char *dir, int pressure)
{
    if (chdir(dir)) { perror("chdir"); exit(1); }
    mkdir_("p"); mkdir_("q"); mkdir_("p/child"); mkdir_("q/victim");
    /* Cross-parent directory replacement removes victim dots and changes '..'. */
    rename_("p/child", "q/victim", 0);
    mkdir_("p/exchange");
    rename_("p/exchange", "q/victim", RENAME_EXCHANGE);
    rmdir_("p/exchange"); rmdir_("q/victim");
    int fd = open_("p/a");
    char data[4096]; memset(data, 0x5a, sizeof(data));
    require(CALL("pwrite64",SYS_pwrite64,fd,data,sizeof(data),0,0,0),"pwrite");
    require(CALL("fallocate",SYS_fallocate,fd,0,0,getenv("CT_ALLOC_BYTES") ? atol(getenv("CT_ALLOC_BYTES")) : 1024*1024,0,0),"allocate");
    require(CALL("fallocate",SYS_fallocate,fd,FALLOC_FL_PUNCH_HOLE|FALLOC_FL_KEEP_SIZE,4096,4096,0,0),"punch");
    /* Exercise extent removal/insertion paths with aligned, initialized ranges. */
    require(CALL("fallocate-collapse",SYS_fallocate,fd,FALLOC_FL_COLLAPSE_RANGE,65536,65536,0,0),"collapse");
    require(CALL("fallocate-insert",SYS_fallocate,fd,FALLOC_FL_INSERT_RANGE,65536,65536,0,0),"insert");
    require(CALL("fallocate-zero",SYS_fallocate,fd,FALLOC_FL_ZERO_RANGE,131072,65536,0,0),"zero");
    require(CALL("ftruncate",SYS_ftruncate,fd,65536,0,0,0,0),"truncate");
    require(CALL("fsync",SYS_fsync,fd,0,0,0,0,0),"fsync");
    require(CALL("link",SYS_link,"p/a","p/b",0,0,0,0),"link");
    int victim = open_("p/c"); close_(victim);
    rename_("p/b","p/c",0);
    long ret = CALL("renameat2-noreplace",SYS_renameat2,AT_FDCWD,"p/a",AT_FDCWD,"p/c",RENAME_NOREPLACE,0);
    if (ret != -1 || errno != EEXIST) { fprintf(stderr,"expected EEXIST\n"); exit(1); }
    require(CALL("symlink",SYS_symlink,"a","p/s",0,0,0,0),"symlink");
    unlink_("p/s"); unlink_("p/c"); unlink_("p/a");
    checkpoint("open-unlinked");
    /* Stable namespace must handle open-but-unlinked files; close releases inode. */
    close_(fd);
    int tmp = require(CALL("openat-tmpfile",SYS_openat,AT_FDCWD,"p",O_TMPFILE|O_RDWR,0600,0,0),"tmpfile");
    require(CALL("pwrite64",SYS_pwrite64,tmp,data,sizeof(data),0,0,0),"tmp write");
    checkpoint("tmpfile-unlinked");
    require(CALL("linkat-tmpfile",SYS_linkat,tmp,"",AT_FDCWD,"p/tmp",AT_EMPTY_PATH,0),"tmp link");
    unlink_("p/tmp"); close_(tmp);
    int white = open_("p/white"); close_(white);
    ret = CALL("renameat2-whiteout",SYS_renameat2,AT_FDCWD,"p/white",AT_FDCWD,"q/moved",RENAME_WHITEOUT,0);
    if (ret == 0) { unlink_("p/white"); unlink_("q/moved"); }
    else if (errno == EOPNOTSUPP || errno == EINVAL) { unlink_("p/white"); }
    else { perror("whiteout"); exit(1); }
    /* On an inline_data image this starts inline, then converts to block dir. */
    mkdir_("p/inline");
    for (int i=0;i<28;i++) {
        char path[128]; snprintf(path,sizeof(path),"p/inline/entry-%02d-abcdefghijklmnop",i);
        int entry = open_(path); close_(entry);
        if (i==0) checkpoint("inline-live");
        if (i==27) checkpoint("inline-converted");
    }
    for (int i=0;i<28;i++) {
        char path[128]; snprintf(path,sizeof(path),"p/inline/entry-%02d-abcdefghijklmnop",i);
        unlink_(path);
    }
    rmdir_("p/inline");
    if (pressure) {
        fd = open_("p/enospc");
        ret = CALL("fallocate-enospc",SYS_fallocate,fd,0,0,1024LL*1024*1024,0,0);
        if (ret != -1 || errno != ENOSPC) { fprintf(stderr,"expected ENOSPC\n"); exit(1); }
        close_(fd); unlink_("p/enospc");
    }
    ret = CALL("unlink-missing",SYS_unlink,"p/missing",0,0,0,0,0);
    if (ret != -1 || errno != ENOENT) { fprintf(stderr,"expected ENOENT\n"); exit(1); }
    rmdir_("p"); rmdir_("q");
}

static void bigalloc_partial(void)
{
	char data[65536];
    int fd;
    memset(data, 0x6b, sizeof(data));

    fd = open_("ba-pending");
	require(CALL("pwrite64-ba-pending-a",SYS_pwrite64,fd,data,sizeof(data),0,0,0),"bigalloc pending write a");
    require(CALL("fallocate-ba-punch-pending",SYS_fallocate,fd,
                 FALLOC_FL_PUNCH_HOLE|FALLOC_FL_KEEP_SIZE,4096,4096,0,0),
            "bigalloc punch pending");
	require(CALL("fallocate-ba-punch-pending-again",SYS_fallocate,fd,
		     FALLOC_FL_PUNCH_HOLE|FALLOC_FL_KEEP_SIZE,4096,4096,0,0),
		    "bigalloc punch pending again");
	require(CALL("ftruncate-ba-pending",SYS_ftruncate,fd,65536,0,0,0,0),"bigalloc truncate pending");
    require(CALL("fsync-ba-pending",SYS_fsync,fd,0,0,0,0,0),"bigalloc fsync pending");
    checkpoint("bigalloc-pending-punch");
    close_(fd);
    unlink_("ba-pending");

    fd = open_("ba-shared-cluster");
    require(CALL("pwrite64-ba-shared-a",SYS_pwrite64,fd,data,4096,4096,0,0),"bigalloc shared write a");
    require(CALL("pwrite64-ba-shared-b",SYS_pwrite64,fd,data,4096,4096,16384,0),"bigalloc shared write b");
    require(CALL("fallocate-ba-shared-punch",SYS_fallocate,fd,
                 FALLOC_FL_PUNCH_HOLE|FALLOC_FL_KEEP_SIZE,4096,4096,0,0),
            "bigalloc shared punch");
    require(CALL("fsync-ba-shared",SYS_fsync,fd,0,0,0,0,0),"bigalloc fsync shared");
    checkpoint("bigalloc-shared-cluster");
    require(CALL("ftruncate-ba-shared",SYS_ftruncate,fd,8192,0,0,0,0),"bigalloc truncate shared");
    require(CALL("fsync-ba-truncated",SYS_fsync,fd,0,0,0,0,0),"bigalloc fsync truncated");
    checkpoint("bigalloc-truncate-partial");
    close_(fd);
    unlink_("ba-shared-cluster");

    fd = open_("ba-boundary");
    require(CALL("pwrite64-ba-boundary-left",SYS_pwrite64,fd,data,4096,61440,0,0),"bigalloc boundary write left");
    require(CALL("pwrite64-ba-boundary-right",SYS_pwrite64,fd,data,4096,65536,0,0),"bigalloc boundary write right");
    require(CALL("fsync-ba-boundary",SYS_fsync,fd,0,0,0,0,0),"bigalloc fsync boundary");
    require(CALL("fallocate-ba-boundary-punch",SYS_fallocate,fd,
                 FALLOC_FL_PUNCH_HOLE|FALLOC_FL_KEEP_SIZE,61440,4096,0,0),
            "bigalloc boundary punch");
    checkpoint("bigalloc-boundary-punch");
    close_(fd);
    unlink_("ba-boundary");
    checkpoint("bigalloc-partial-cleanup");
}

int main(int argc, char **argv)
{
    if (argc != 3) { fprintf(stderr,"usage: chain-workload DIR single|concurrent\n"); return 2; }
    marker = open("/sys/kernel/tracing/trace_marker", O_WRONLY|O_CLOEXEC);
    if (marker < 0) { perror("trace_marker"); return 2; }
    if (!strcmp(argv[2],"single")) {
        checkpoints=1;
        normal(argv[1],1);
        if (getenv("CT_BIGALLOC")) bigalloc_partial();
    }
    else if (!strcmp(argv[2],"concurrent")) {
        pid_t children[2];
        for (int i=0;i<2;i++) {
            char path[4096]; snprintf(path,sizeof(path),"%s/worker%d",argv[1],i);
            if (mkdir(path,0700)) { perror("worker mkdir"); return 1; }
            children[i] = fork();
            if (children[i] < 0) { perror("fork"); return 1; }
            if (!children[i]) { sequence=0; normal(path,0); _exit(0); }
        }
        for (int i=0;i<2;i++) {
            int status; if (waitpid(children[i],&status,0)<0 || !WIFEXITED(status) || WEXITSTATUS(status)) return 1;
            char path[4096]; snprintf(path,sizeof(path),"%s/worker%d",argv[1],i);
            rmdir_(path);
        }
    } else return 2;
    close(marker);
    return 0;
}
