/*
 * Copyright (C) 2026 Naveenraj Muthuraj
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Minimal vmocs SPANK plugin.
 *
 * Hooks used:
 *   slurm_spank_init          — register --vm-image and --vm-save options
 *   slurm_spank_init_post_opt — propagate template name into job env (allocator)
 *   slurm_spank_task_init     — vmocs launch <template> ... (blocking, job user)
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

#include <stdint.h>
#include <slurm/spank.h>

SPANK_PLUGIN(vmocs, 1);

static int  vm_enabled          = 0;
static char vm_template[256]    = "";
static char vm_save_path[1024]  = "";

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

static struct spank_option vmocs_options[] = {
    {
        "vm-image",
        "TEMPLATE",
        "Boot a VM with the specified vmocs template name",
        1,                              /* has_arg */
        0,                              /* val (unused) */
        (spank_opt_cb_f) opt_vm_image
    },
    {
        "vm-save",
        "PATH",
        "Flatten VM disk into a new qcow2 image when the job ends",
        1,                              /* has_arg */
        0,                              /* val (unused) */
        (spank_opt_cb_f) opt_vm_save
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

/* Return "--config <path>" fragment from vmocs_conf= arg, or "" if not set. */
static const char *vmocs_conf_arg(int ac, char **av)
{
    static char buf[576];
    int i;
    for (i = 0; i < ac; i++) {
        if (strncmp(av[i], "vmocs_conf=", 11) == 0) {
            snprintf(buf, sizeof(buf), "--config %s", av[i] + 11);
            return buf;
        }
    }
    return "";
}

/* Fork /bin/sh -c cmd, wait for it, return exit code. */
static int run_and_wait(const char *cmd)
{
    pid_t pid;
    int   status;

    slurm_verbose("vmocs: %s", cmd);

    pid = fork();
    if (pid == 0) {
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
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
 * Runs as the job user, inside the Slurm cgroup.
 * 'vmocs launch' blocks until QEMU exits so Slurm sees the task as alive.
 * QEMU inherits the cgroup automatically via fork/exec inside vmocs.
 */
int slurm_spank_task_init(spank_t sp, int ac, char **av)
{
    uint32_t jobid = 0;
    char     buf[64];
    char     cmd[1024];
    long     cores  = 1;
    long     mem_mb;

    if (!vm_enabled) return ESPANK_SUCCESS;

    spank_get_item(sp, S_JOB_ID, &jobid);

    if (spank_getenv(sp, "SLURM_CPUS_PER_TASK", buf, sizeof(buf)) == ESPANK_SUCCESS)
        cores = atol(buf);

    mem_mb = get_memory_mb(sp);

    if (mem_mb > 0) {
        snprintf(cmd, sizeof(cmd),
                 "%s %s launch %s --cores %ld --memory %ld --job-id %u",
                 vmocs_bin(ac, av), vmocs_conf_arg(ac, av),
                 vm_template, cores, mem_mb, jobid);
    } else {
        snprintf(cmd, sizeof(cmd),
                 "%s %s launch %s --cores %ld --job-id %u",
                 vmocs_bin(ac, av), vmocs_conf_arg(ac, av),
                 vm_template, cores, jobid);
    }

    return run_and_wait(cmd) == 0 ? ESPANK_SUCCESS : ESPANK_ERROR;
}

/*
 * Called after the task exits.  Ensures QEMU is stopped and the runtime
 * dir is cleaned up even if the guest shut down naturally mid-job.
 * Best-effort: never fail the job on cleanup error.
 */
int slurm_spank_exit(spank_t sp, int ac, char **av)
{
    uint32_t jobid = 0;
    char     cmd[1536];

    if (!vm_enabled)                          return ESPANK_SUCCESS;
    if (spank_context() != S_CTX_REMOTE)     return ESPANK_SUCCESS;

    spank_get_item(sp, S_JOB_ID, &jobid);
    if (vm_save_path[0])
        snprintf(cmd, sizeof(cmd), "%s %s stop %u --save %s",
                 vmocs_bin(ac, av), vmocs_conf_arg(ac, av), jobid, vm_save_path);
    else
        snprintf(cmd, sizeof(cmd), "%s %s stop %u",
                 vmocs_bin(ac, av), vmocs_conf_arg(ac, av), jobid);
    run_and_wait(cmd);

    return ESPANK_SUCCESS;
}
