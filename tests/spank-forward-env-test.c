/*
 * Context-level integration tests for repeatable --vm-forward-env handling.
 *
 * Include the plugin so its private serialization helpers can be exercised,
 * then provide a minimal SPANK implementation that models the protected
 * local-to-remote task environment.
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../plugins/slurm/spank_vmocs.c"

static spank_context_t mock_context;
static char mock_control[MAX_FORWARD_ENV_SERIALIZED];
static char mock_task_control[MAX_FORWARD_ENV_SERIALIZED];
static char mock_argv[MAX_PREFIX_ARGS][MAX_ENV_NAME];
static int mock_argc;
static int mock_unset;
static char mock_log[4096];

static void fail(const char *message)
{
    fprintf(stderr, "FAIL: %s\n", message);
    exit(1);
}

static void require(int condition, const char *message)
{
    if (!condition)
        fail(message);
}

static void reset_plugin(void)
{
    vm_enabled = 0;
    vm_template[0] = '\0';
    vm_save_path[0] = '\0';
    vm_resume_path[0] = '\0';
    strcpy(vm_attach, "auto");
    clear_forward_env_names();
    mock_control[0] = '\0';
    mock_task_control[0] = '\0';
    mock_argc = 0;
    mock_unset = 0;
    mock_log[0] = '\0';
    unsetenv(FORWARD_ENV_TASK_VAR);
}

static int forwarded_arg_count(void)
{
    int count = 0;
    for (int i = 0; i + 1 < mock_argc; i++) {
        if (strcmp(mock_argv[i], "--forward-env") == 0)
            count++;
    }
    return count;
}

static int has_forwarded_arg(const char *name)
{
    for (int i = 0; i + 1 < mock_argc; i++) {
        if (strcmp(mock_argv[i], "--forward-env") == 0 &&
            strcmp(mock_argv[i + 1], name) == 0)
            return 1;
    }
    return 0;
}

static void run_remote_with_only_last_replayed(const char *last)
{
    strcpy(mock_task_control, mock_control);
    clear_forward_env_names();
    require(opt_vm_forward_env(0, last, 1) == ESPANK_SUCCESS,
            "remote replay rejected final option");
    mock_context = S_CTX_REMOTE;
    require(slurm_spank_task_init((spank_t)1, 0, NULL) == ESPANK_SUCCESS,
            "remote task initialization failed");
}

static void test_direct_srun(void)
{
    reset_plugin();
    mock_context = S_CTX_LOCAL;
    require(opt_vm_image(0, "base-ubuntu", 0) == ESPANK_SUCCESS,
            "direct image option failed");
    require(opt_vm_forward_env(0, "FIRST", 0) == ESPANK_SUCCESS,
            "direct first option failed");
    require(opt_vm_forward_env(0, "MIDDLE", 0) == ESPANK_SUCCESS,
            "direct middle option failed");
    require(opt_vm_forward_env(0, "LAST", 0) == ESPANK_SUCCESS,
            "direct last option failed");
    require(slurm_spank_init_post_opt((spank_t)1, 0, NULL) == ESPANK_SUCCESS,
            "direct serialization failed");
    strcpy(mock_control, getenv(FORWARD_ENV_TASK_VAR));
    require(strcmp(mock_control, "FIRST,MIDDLE,LAST") == 0,
            "direct serialization lost an occurrence");

    run_remote_with_only_last_replayed("LAST");
    require(forwarded_arg_count() == 3,
            "direct remote argv did not recover three names");
    require(has_forwarded_arg("FIRST") && has_forwarded_arg("MIDDLE") &&
            has_forwarded_arg("LAST"),
            "direct remote argv lost first, middle, or last name");
    require(mock_unset, "private transport variable remained in task env");
}

static void test_nested_sbatch_srun(void)
{
    reset_plugin();

    /* sbatch allocator loads the plugin before its host script invokes srun. */
    mock_context = S_CTX_ALLOCATOR;
    require(slurm_spank_init_post_opt((spank_t)1, 0, NULL) == ESPANK_SUCCESS,
            "empty sbatch allocator context failed");

    mock_context = S_CTX_LOCAL;
    require(opt_vm_image(0, "base-ubuntu", 0) == ESPANK_SUCCESS,
            "nested image option failed");
    require(opt_vm_forward_env(0, "BATCH_A", 0) == ESPANK_SUCCESS,
            "nested first option failed");
    require(opt_vm_forward_env(0, "BATCH_B", 0) == ESPANK_SUCCESS,
            "nested second option failed");
    require(opt_vm_forward_env(0, "SLURM_JOB_ID", 0) == ESPANK_SUCCESS,
            "nested Slurm option failed");
    require(slurm_spank_init_post_opt((spank_t)1, 0, NULL) == ESPANK_SUCCESS,
            "nested srun serialization failed");
    strcpy(mock_control, getenv(FORWARD_ENV_TASK_VAR));

    run_remote_with_only_last_replayed("SLURM_JOB_ID");
    require(forwarded_arg_count() == 3 &&
            has_forwarded_arg("BATCH_A") && has_forwarded_arg("BATCH_B") &&
            has_forwarded_arg("SLURM_JOB_ID"),
            "nested sbatch+srun recovery lost a name");
}

static void test_salloc_inheritance(void)
{
    reset_plugin();
    mock_context = S_CTX_ALLOCATOR;
    require(opt_vm_image(0, "base-ubuntu", 0) == ESPANK_SUCCESS,
            "allocator image option failed");
    require(opt_vm_forward_env(0, "ALLOC_A", 0) == ESPANK_SUCCESS,
            "allocator first option failed");
    require(opt_vm_forward_env(0, "ALLOC_B", 0) == ESPANK_SUCCESS,
            "allocator second option failed");
    require(slurm_spank_init_post_opt((spank_t)1, 0, NULL) == ESPANK_SUCCESS,
            "allocator serialization failed");
    strcpy(mock_control, getenv(FORWARD_ENV_TASK_VAR));

    /* srun inherits the private list but Slurm replays only the final option. */
    require(setenv(FORWARD_ENV_TASK_VAR, mock_control, 1) == 0,
            "failed to model allocator environment inheritance");
    clear_forward_env_names();
    require(opt_vm_forward_env(0, "ALLOC_B", 0) == ESPANK_SUCCESS,
            "local replay of allocator option failed");
    mock_context = S_CTX_LOCAL;
    require(slurm_spank_init_post_opt((spank_t)1, 0, NULL) == ESPANK_SUCCESS,
            "inherited list merge failed");
    require(strcmp(mock_control, "ALLOC_A,ALLOC_B") == 0,
            "salloc inheritance lost or duplicated a name");
}

static void test_validation_and_secrecy(void)
{
    char name[32];

    reset_plugin();
    require(opt_vm_forward_env(0, "DUP", 0) == ESPANK_SUCCESS &&
            opt_vm_forward_env(0, "DUP", 0) == ESPANK_SUCCESS &&
            vm_forward_env_count == 1,
            "duplicate names were not deduplicated");
    require(opt_vm_forward_env(0, "BAD-NAME", 0) == ESPANK_BAD_ARG,
            "invalid name was accepted");

    clear_forward_env_names();
    for (int i = 0; i < MAX_FORWARD_ENV; i++) {
        snprintf(name, sizeof(name), "NAME_%d", i);
        require(opt_vm_forward_env(0, name, 0) == ESPANK_SUCCESS,
                "one of sixteen names was rejected");
    }
    require(opt_vm_forward_env(0, "NAME_16", 0) == ESPANK_BAD_ARG,
            "seventeenth name was accepted");

    require(strstr(mock_control, "secret value with quotes and\nnewlines") == NULL,
            "an environment value entered the transport");
    require(strstr(mock_log, "secret value with quotes and\nnewlines") == NULL,
            "an environment value entered plugin logs");
}

int main(void)
{
    test_direct_srun();
    test_nested_sbatch_srun();
    test_salloc_inheritance();
    test_validation_and_secrecy();
    puts("PASS: repeatable --vm-forward-env context transport");
    return 0;
}

spank_context_t spank_context(void)
{
    return mock_context;
}

spank_err_t spank_option_register(spank_t sp, struct spank_option *opt)
{
    return ESPANK_SUCCESS;
}

spank_err_t spank_job_control_setenv(spank_t sp, const char *name,
                                     const char *value, int overwrite)
{
    return ESPANK_SUCCESS;
}

spank_err_t spank_getenv(spank_t sp, const char *name, char *buf, int len)
{
    if (strcmp(name, FORWARD_ENV_TASK_VAR) != 0 || !mock_task_control[0])
        return ESPANK_ENV_NOEXIST;
    if ((int)strlen(mock_task_control) >= len)
        return ESPANK_NOSPACE;
    strcpy(buf, mock_task_control);
    return ESPANK_SUCCESS;
}

spank_err_t spank_unsetenv(spank_t sp, const char *name)
{
    if (strcmp(name, FORWARD_ENV_TASK_VAR) == 0)
        mock_unset = 1;
    return ESPANK_SUCCESS;
}

spank_err_t spank_get_item(spank_t sp, spank_item_t item, ...)
{
    va_list ap;
    uint32_t *value;

    va_start(ap, item);
    value = va_arg(ap, uint32_t *);
    if (item == S_JOB_ID)
        *value = 1234;
    else if (item == S_JOB_TOTAL_TASK_COUNT)
        *value = 1;
    else {
        va_end(ap);
        return ESPANK_BAD_ARG;
    }
    va_end(ap);
    return ESPANK_SUCCESS;
}

spank_err_t spank_prepend_task_argv(spank_t sp, int argc,
                                    const char *argv[])
{
    if (argc > MAX_PREFIX_ARGS)
        return ESPANK_BAD_ARG;
    mock_argc = argc;
    for (int i = 0; i < argc; i++) {
        if (strlen(argv[i]) >= sizeof(mock_argv[i]))
            return ESPANK_NOSPACE;
        strcpy(mock_argv[i], argv[i]);
    }
    return ESPANK_SUCCESS;
}

void slurm_error(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    vsnprintf(mock_log, sizeof(mock_log), format, ap);
    va_end(ap);
}

void slurm_verbose(const char *format, ...)
{
    (void)format;
}
