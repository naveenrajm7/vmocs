/*
 * Copyright (C) 2026 Naveenraj Muthuraj
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Minimal vmocs SPANK plugin.
 *
 * Hooks used:
 *   slurm_spank_init          — register --vm-image and --vm-save options
 *   slurm_spank_init_post_opt — propagate template name into job env (allocator)
 *   slurm_spank_task_init     — prepend vmocs run to the Slurm task argv
 *   slurm_spank_exit          — vmocs stop <jobid>         (best-effort cleanup)
 *
 * Plugin args (in plugstack.conf):
 *   vmocs_path=/usr/local     — prefix that contains bin/vmocs (default: PATH)
 *
 * Compile:
 *   make -C plugins/slurm
 *
 * Install:
 *   install -m755 spank_vmocs.so /usr/lib64/slurm/
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <dirent.h>
#include <fcntl.h>

#include <stdint.h>
#include <slurm/spank.h>

SPANK_PLUGIN(vmocs, 1);

static int  vm_enabled          = 0;
static char vm_template[256]    = "";
static char vm_save_path[1024]  = "";
static char vm_attach[16]       = "auto";

/* -------------------------------------------------------------------------
 * Option handler — called when --vm-image is seen
 * ---------------------------------------------------------------------- */

static int opt_vm_image(int val, const char *optarg, int remote)
{
    vm_enabled = 1;
    strncpy(vm_template, optarg, sizeof(vm_template) - 1);
    vm_template[sizeof(vm_template) - 1] = '\0';
    return ESPANK_SUCCESS;
}

static int opt_vm_save(int val, const char *optarg, int remote)
{
    strncpy(vm_save_path, optarg, sizeof(vm_save_path) - 1);
    vm_save_path[sizeof(vm_save_path) - 1] = '\0';
    return ESPANK_SUCCESS;
}

static int opt_vm_attach(int val, const char *optarg, int remote)
{
    if (strcmp(optarg, "auto") != 0 && strcmp(optarg, "none") != 0) {
        slurm_error("vmocs: --vm-attach must be 'auto' or 'none'");
        return ESPANK_BAD_ARG;
    }
    strncpy(vm_attach, optarg, sizeof(vm_attach) - 1);
    vm_attach[sizeof(vm_attach) - 1] = '\0';
    return ESPANK_SUCCESS;
}

static struct spank_option vmocs_options[] = {
    {
        "vm-image",
        "TEMPLATE",
        "[vmocs] Boot a VM with the specified vmocs template name",
        1,                              /* has_arg */
        0,                              /* val (unused) */
        (spank_opt_cb_f) opt_vm_image
    },
    {
        "vm-save",
        "PATH",
        "[vmocs] Flatten VM disk into a new qcow2 image when the job ends",
        1,                              /* has_arg */
        0,                              /* val (unused) */
        (spank_opt_cb_f) opt_vm_save
    },
    {
        "vm-attach",
        "MODE",
        "[vmocs] Guest attachment mode: auto (default) or none",
        1,
        0,
        (spank_opt_cb_f) opt_vm_attach
    },
    SPANK_OPTIONS_TABLE_END
};

/* -------------------------------------------------------------------------
 * Helpers
 * ---------------------------------------------------------------------- */

/* Return the vmocs binary path from plugin args, or plain "vmocs" for PATH. */
static const char *vmocs_bin(int ac, char **av)
{
    static char buf[512];
    int i;
    for (i = 0; i < ac; i++) {
        if (strncmp(av[i], "vmocs_path=", 11) == 0) {
            snprintf(buf, sizeof(buf), "%s/bin/vmocs", av[i] + 11);
            return buf;
        }
    }
    return "vmocs";
}

/* Return the configured vmocs config path, or NULL if none was supplied. */
static const char *vmocs_conf_path(int ac, char **av)
{
    int i;
    for (i = 0; i < ac; i++) {
        if (strncmp(av[i], "vmocs_conf=", 11) == 0)
            return av[i] + 11;
    }
    return NULL;
}

/* Fork/exec an argv vector, wait for it, and return its exit code. */
static int run_argv_and_wait(char *const argv[])
{
    pid_t pid;
    int   status;

    slurm_verbose("vmocs: executing %s", argv[0]);

    pid = fork();
    if (pid == 0) {
        execvp(argv[0], argv);
        _exit(127);
    }
    if (pid < 0) {
        slurm_error("vmocs: fork failed");
        return -1;
    }
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

/* Subtract QEMU overhead: 5% or 256 MB, whichever is larger. */
static long apply_headroom(long total_mb)
{
    long headroom = total_mb / 20;   /* 5% */
    if (headroom < 256) headroom = 256;
    long result = total_mb - headroom;
    return result < 512 ? 512 : result;
}

/* Derive guest memory in MB from Slurm env vars.  Returns 0 if not set. */
static long get_memory_mb(spank_t sp)
{
    char buf[64];
    long cores = 1;

    if (spank_getenv(sp, "SLURM_CPUS_PER_TASK", buf, sizeof(buf)) == ESPANK_SUCCESS)
        cores = atol(buf);

    if (spank_getenv(sp, "SLURM_MEM_PER_NODE", buf, sizeof(buf)) == ESPANK_SUCCESS) {
        long v = atol(buf);
        if (v > 0) return apply_headroom(v);
    }

    if (spank_getenv(sp, "SLURM_MEM_PER_CPU", buf, sizeof(buf)) == ESPANK_SUCCESS) {
        long v = atol(buf);
        if (v > 0) return apply_headroom(v * cores);
    }

    return 0;   /* no memory constraint found; vmocs will use its default */
}

/*
 * Scan /dev/vfio/ for numeric group entries accessible in this job's cgroup.
 * For each accessible group, enumerate every BDF under
 * /sys/kernel/iommu_groups/<N>/devices/ and append "--pci <BDF>" to buf.
 *
 * The cgroup device whitelist is the source of truth: open() succeeds only
 * for groups Slurm allocated to this job; all others return EPERM.
 * Passing all BDFs in the group (not just the display-class function) is
 * required for consumer GPUs where GPU and audio share one IOMMU group.
 */
static void collect_pci_args(char *buf, size_t buflen)
{
    DIR           *vfio_dir;
    struct dirent *ent;

    buf[0] = '\0';

    vfio_dir = opendir("/dev/vfio");
    if (!vfio_dir)
        return;

    while ((ent = readdir(vfio_dir)) != NULL) {
        char           dev_path[320];
        char           grp_path[384];
        DIR           *grp_dir;
        struct dirent *dev_ent;
        char          *endp;
        int            fd;

        /* Skip non-numeric entries ("vfio", ".", "..") */
        strtol(ent->d_name, &endp, 10);
        if (*endp != '\0' || endp == ent->d_name)
            continue;

        /* Try to open — EPERM means Slurm's cgroup blocks it */
        snprintf(dev_path, sizeof(dev_path), "/dev/vfio/%s", ent->d_name);
        fd = open(dev_path, O_RDWR);
        if (fd < 0)
            continue;
        close(fd);

        /* Append --pci <BDF> for every device in this IOMMU group */
        snprintf(grp_path, sizeof(grp_path),
                 "/sys/kernel/iommu_groups/%s/devices", ent->d_name);
        grp_dir = opendir(grp_path);
        if (!grp_dir)
            continue;

        while ((dev_ent = readdir(grp_dir)) != NULL) {
            size_t used;
            if (dev_ent->d_name[0] == '.')
                continue;
            used = strlen(buf);
            snprintf(buf + used, buflen - used, " --pci %s", dev_ent->d_name);
        }
        closedir(grp_dir);
    }

    closedir(vfio_dir);
}


/* -------------------------------------------------------------------------
 * SPANK hooks
 * ---------------------------------------------------------------------- */

int slurm_spank_init(spank_t sp, int ac, char **av)
{
    int i, rc;
    for (i = 0; vmocs_options[i].name; i++) {
        rc = spank_option_register(sp, &vmocs_options[i]);
        if (rc != ESPANK_SUCCESS)
            return rc;
    }
    return ESPANK_SUCCESS;
}

int slurm_spank_init_post_opt(spank_t sp, int ac, char **av)
{
    if (!vm_enabled) return ESPANK_SUCCESS;

    /*
     * Persist the template name into the job environment so it is available
     * even when the user runs salloc then srun separately.
     */
    if (spank_context() == S_CTX_ALLOCATOR)
        spank_job_control_setenv(sp, "VMOCS_TEMPLATE", vm_template, 1);

    return ESPANK_SUCCESS;
}

/*
 * Runs as the job user, inside the Slurm cgroup, immediately before execve.
 * Prepend the vmocs supervisor to the original task argv. Slurm will exec the
 * supervisor as the task; it launches QEMU, SSHs the original command into the
 * guest, and returns the guest command's status.
 */
int slurm_spank_task_init(spank_t sp, int ac, char **av)
{
    uint32_t jobid = 0;
    uint32_t ntasks = 0;
    char     buf[64];
    char     pci_args[512];
    char     cores_arg[32];
    char     memory_arg[32];
    char     jobid_arg[32];
    char    *saveptr = NULL;
    char    *token;
    const char *prefix[64];
    const char *conf_path;
    int      prefix_count = 0;
    long     cores  = 1;
    long     mem_mb;

    if (!vm_enabled) return ESPANK_SUCCESS;

    spank_get_item(sp, S_JOB_ID, &jobid);
    if (spank_get_item(sp, S_JOB_TOTAL_TASK_COUNT, &ntasks) == ESPANK_SUCCESS &&
        ntasks != 1) {
        slurm_error("vmocs: seamless guest sessions currently require --ntasks=1");
        return ESPANK_ERROR;
    }

    if (spank_getenv(sp, "SLURM_CPUS_PER_TASK", buf, sizeof(buf)) == ESPANK_SUCCESS)
        cores = atol(buf);

    mem_mb = get_memory_mb(sp);
    collect_pci_args(pci_args, sizeof(pci_args));
    snprintf(cores_arg, sizeof(cores_arg), "%ld", cores);
    snprintf(memory_arg, sizeof(memory_arg), "%ld", mem_mb);
    snprintf(jobid_arg, sizeof(jobid_arg), "%u", jobid);

    prefix[prefix_count++] = vmocs_bin(ac, av);
    conf_path = vmocs_conf_path(ac, av);
    if (conf_path) {
        prefix[prefix_count++] = "--config";
        prefix[prefix_count++] = conf_path;
    }
    prefix[prefix_count++] = "run";
    prefix[prefix_count++] = vm_template;
    prefix[prefix_count++] = "--cores";
    prefix[prefix_count++] = cores_arg;
    prefix[prefix_count++] = "--job-id";
    prefix[prefix_count++] = jobid_arg;
    prefix[prefix_count++] = "--attach";
    prefix[prefix_count++] = vm_attach;
    if (mem_mb > 0) {
        prefix[prefix_count++] = "--memory";
        prefix[prefix_count++] = memory_arg;
    }
    if (vm_save_path[0]) {
        prefix[prefix_count++] = "--save";
        prefix[prefix_count++] = vm_save_path;
    }

    token = strtok_r(pci_args, " ", &saveptr);
    while (token && prefix_count < 62) {
        prefix[prefix_count++] = token;
        token = strtok_r(NULL, " ", &saveptr);
    }
    prefix[prefix_count++] = "--";

    return spank_prepend_task_argv(sp, prefix_count, prefix);
}

/*
 * Called after the task exits.  Ensures QEMU is stopped and the runtime
 * dir is cleaned up even if the guest shut down naturally mid-job.
 * Best-effort: never fail the job on cleanup error.
 */
int slurm_spank_exit(spank_t sp, int ac, char **av)
{
    uint32_t jobid = 0;
    char     jobid_arg[32];
    char    *stop_argv[8];
    const char *conf_path;
    int      argc = 0;

    if (!vm_enabled)                          return ESPANK_SUCCESS;
    if (spank_context() != S_CTX_REMOTE)     return ESPANK_SUCCESS;

    spank_get_item(sp, S_JOB_ID, &jobid);
    snprintf(jobid_arg, sizeof(jobid_arg), "%u", jobid);
    stop_argv[argc++] = (char *)vmocs_bin(ac, av);
    conf_path = vmocs_conf_path(ac, av);
    if (conf_path) {
        stop_argv[argc++] = "--config";
        stop_argv[argc++] = (char *)conf_path;
    }
    stop_argv[argc++] = "stop";
    stop_argv[argc++] = jobid_arg;
    stop_argv[argc++] = "--if-exists";
    stop_argv[argc] = NULL;
    run_argv_and_wait(stop_argv);

    return ESPANK_SUCCESS;
}
