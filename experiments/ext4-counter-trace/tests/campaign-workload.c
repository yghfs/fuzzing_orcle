/* A seeded, bounded ordinary syscall batch. Snapshot/rotation is controlled
 * externally at drained operation boundaries, never with SIGSTOP writers. */
#define main finite_workload_main
#include "chain-workload.c"
#undef main
#include <stdint.h>
#include <signal.h>

static uint64_t rng;
static void raw_filter(pid_t *children, int count)
{
    char filter[512];
    int used=snprintf(filter,sizeof(filter),"common_pid == %ld",(long)getpid());
    for (int i=0;i<count;i++)
        used+=snprintf(filter+used,sizeof(filter)-used," || common_pid == %ld",(long)children[i]);
    if (used >= (int)sizeof(filter)) exit(2);
    /* Enable exit first: filter setup must not create entries without exits. */
    const char *paths[]={"/sys/kernel/tracing/events/raw_syscalls/sys_exit/filter",
                         "/sys/kernel/tracing/events/raw_syscalls/sys_enter/filter"};
    for (int i=0;i<2;i++) {
        int fd=open(paths[i],O_WRONLY|O_CLOEXEC);
        if (fd<0 || write(fd,filter,used)!=used) { perror("raw syscall filter"); exit(2); }
        close(fd);
    }
}
static uint64_t next_random(void)
{
    rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
    return rng;
}
static void retain(const char *dir)
{
    char path[4096];
    if (snprintf(path,sizeof(path),"%s/retained",dir)>=(int)sizeof(path)) exit(2);
    if (access(path,F_OK)) mkdir_(path);
    char file[4096];
    if (snprintf(file,sizeof(file),"%s/data",path)>=(int)sizeof(file)) exit(2);
    int fd=open_(file);
    long size=(1+(next_random()%8))*1024*1024;
    require(CALL("fallocate-retained",SYS_fallocate,fd,0,0,size,0,0),"retain");
    require(CALL("fsync-retained",SYS_fsync,fd,0,0,0,0,0),"retain fsync");
    close_(fd);
    printf("retained_bytes=%ld\n",size);
}
int main(int argc, char **argv)
{
    if (argc!=6) { fprintf(stderr,"usage: campaign-workload ROOT single|concurrent SEED WORKERS MEMORY_MB\n"); return 2; }
    uint64_t seed=strtoull(argv[3],NULL,10);
    int workers=atoi(argv[4]), memory_mb=atoi(argv[5]);
    if (!seed || workers<1 || workers>8 || memory_mb<0 || memory_mb>256) return 2;
    char root[4096];
    if (!realpath(argv[1],root)) { perror("campaign root"); return 2; }
    raw_filter(NULL,0);
    marker=open("/sys/kernel/tracing/trace_marker",O_WRONLY|O_CLOEXEC);
    if (marker<0) { perror("trace_marker"); return 2; }
    checkpoints=0;
    rng=seed;
    char length[32];
    snprintf(length,sizeof(length),"%lu",(unsigned long)(1+next_random()%8)*1024*1024);
    setenv("CT_ALLOC_BYTES",length,1);
    void *pressure=NULL;
    if (memory_mb) {
        pressure=malloc((size_t)memory_mb*1024*1024);
        if (!pressure) { perror("pressure malloc"); return 1; }
        /* Volatile stores ensure actual resident-page pressure at -O2. */
        for (size_t offset=0;offset<(size_t)memory_mb*1024*1024;offset+=4096)
            ((volatile unsigned char *)pressure)[offset]=0x3c;
    }
    char trial[4096];
    if (snprintf(trial,sizeof(trial),"%s/trial-%llu",root,(unsigned long long)seed)>=(int)sizeof(trial)) return 2;
    mkdir_(trial);
    if (!strcmp(argv[2],"single")) {
        normal(trial,1);
    } else if (!strcmp(argv[2],"concurrent")) {
        pid_t children[8];
        int gate[2];
        if (pipe(gate)) { perror("worker gate"); return 1; }
        for (int i=0;i<workers;i++) {
            char sub[4096];
            if (snprintf(sub,sizeof(sub),"%s/worker%d",trial,i)>=(int)sizeof(sub)) return 2;
            mkdir_(sub);
            children[i]=fork();
            if (children[i]<0) { perror("fork"); return 1; }
            if (!children[i]) {
                char ready; close(gate[1]);
                if (read(gate[0],&ready,1)!=1) _exit(2);
                close(gate[0]);
                rng=seed+i+1; sequence=0; normal(sub,0); _exit(0);
            }
        }
        close(gate[0]);
        raw_filter(children,workers);
        for (int i=0;i<workers;i++) if (write(gate[1],"x",1)!=1) return 1;
        close(gate[1]);
        for (int i=0;i<workers;i++) {
            int status;
            if (waitpid(children[i],&status,0)<0 || !WIFEXITED(status) || WEXITSTATUS(status)) return 1;
            char sub[4096];
            if (snprintf(sub,sizeof(sub),"%s/worker%d",trial,i)>=(int)sizeof(sub)) return 2;
            rmdir_(sub);
        }
    } else return 2;
    require(CALL("chdir",SYS_chdir,root,0,0,0,0,0),"return root");
    rmdir_(trial);
    retain(root);
    free(pressure);
    printf("batch_complete seed=%llu mode=%s workers=%d memory_mb=%d\n",
           (unsigned long long)seed,argv[2],workers,memory_mb);
    close(marker);
    return 0;
}
