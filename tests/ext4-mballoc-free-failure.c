// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/falloc.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#define CLUSTER_BYTES 65536
static const char *fault_path="/sys/module/ext4/parameters/ext4_fault_mode";
static int write_fault(int v){char b[16]; int n=snprintf(b,sizeof(b),"%d\n",v); int f=open(fault_path,O_WRONLY|O_CLOEXEC); if(f<0)return -1; ssize_t w=write(f,b,n); close(f); return w==n?0:-1;}
static int alloc_file(const char *p, int keep){int f=open(p,O_CREAT|O_EXCL|O_RDWR|O_CLOEXEC,0600); if(f<0)return -1; int r=fallocate(f,FALLOC_FL_KEEP_SIZE,0,2*CLUSTER_BYTES); if(r<0){close(f);return -1;} if(fsync(f)<0){close(f);return -1;} if(!keep){if(ftruncate(f,0)<0)r=-1; if(fsync(f)<0)r=-1; close(f); if(unlink(p)<0)r=-1;} else {close(f);} return r;}
int main(int argc,char **argv){const char *base=argc>1?argv[1]:"/mnt/ext4-runtime/free"; char c[512],x[512]; snprintf(c,sizeof(c),"%s.control",base); snprintf(x,sizeof(x),"%s.injected",base); if(alloc_file(c,0)<0){perror("control");return 2;} int f=open(x,O_CREAT|O_EXCL|O_RDWR|O_CLOEXEC,0600); if(f<0){perror("open");return 3;} if(fallocate(f,FALLOC_FL_KEEP_SIZE,0,2*CLUSTER_BYTES)<0||fsync(f)<0){perror("allocate");return 4;} if(write_fault(16)<0){perror("arm");return 5;} errno=0; int rc=ftruncate(f,0); int e=errno; if(write_fault(0)<0){perror("disarm");return 6;} int sr=fsync(f); printf("injected_ftruncate_rc=%d errno=%d (%s) fsync_rc=%d\n",rc,e,strerror(e),sr); close(f); printf("injected_path=%s\n",x); return (rc==0 && e==0)?0:7;}
