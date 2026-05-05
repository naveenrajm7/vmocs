--  Copyright (C) 2026 Naveenraj Muthuraj
--  SPDX-License-Identifier: GPL-3.0-or-later
--
--  vmocs SPANK plugin — wires vmocs into the Slurm job lifecycle.
--
--  Usage:
--    srun --vm-image ubuntu-base -c2 --mem=4G ./my_script.sh
--
--  Install:
--    cp vm-setup.lua /etc/slurm/lua.d/vm-setup.lua
--    cp vmocs.conf   /etc/slurm/plugstack.conf.d/vmocs.conf
--
--  Optional plugin arg:
--    vmocs_path=/usr/local   — directory containing bin/vmocs (default: use PATH)
--
--  Based on pcocc's vm-setup.lua (CEA/DAM/DIF), simplified for single-VM-per-task.

local posix = require "posix"

local vm_enabled = false
local vm_option  = nil

local debug = SPANK.log_debug
local error = SPANK.log_error


-- ---------------------------------------------------------------------------
-- helpers
-- ---------------------------------------------------------------------------

function do_and_log_output(cmd)
    debug("lua/vmocs: executing %s", cmd)
    local f = assert(io.popen(cmd .. " 2>&1"))
    for line in f:lines() do
        error("lua/vmocs: %s", line)
    end
    f:close()
end

function setenv(name, val)
    local r, msg = posix.setenv(name, tostring(val))
    if r ~= 0 then
        SPANK.log_error("lua/vmocs: failed to set %s: %s", name, msg)
        return 1
    end
    return 0
end

-- Replicate an integer Slurm item (e.g. S_JOB_ID) into an env var.
function replicate_var(spank, slurm_name, env_name)
    local val, msg = spank:get_item(slurm_name)
    if val == nil then
        SPANK.log_error("lua/vmocs: failed to get item %s: %s", slurm_name, msg)
        return 1
    end
    return setenv(env_name, math.floor(val))
end

-- Replicate a Slurm job-environment variable; silently skip if not set.
function try_replicate_env(spank, slurm_name, env_name)
    local val = spank:getenv(slurm_name)
    if val ~= nil then
        setenv(env_name, val)
    end
end

-- Replicate the Slurm vars vmocs needs. Must be called in the remote context
-- because standard SLURM_* vars are not automatically set in slurmstepd.
function replicate_slurm_vars(spank)
    local r = 0
    r = r + replicate_var(spank, "S_JOB_ID", "SLURM_JOB_ID")
    try_replicate_env(spank, "SLURM_CPUS_PER_TASK", "SLURM_CPUS_PER_TASK")
    try_replicate_env(spank, "SLURM_MEM_PER_NODE",  "SLURM_MEM_PER_NODE")
    try_replicate_env(spank, "SLURM_MEM_PER_CPU",   "SLURM_MEM_PER_CPU")
    return r
end

-- Resolve the vmocs binary path.
function vmocs_bin()
    for i, arg in pairs(SPANK.args) do
        local path = arg:match("^vmocs_path=(.*)")
        if path then return path .. "/bin/vmocs" end
    end
    return "vmocs"
end

-- Compute guest memory in MB from Slurm env vars, with a small headroom
-- so QEMU's own overhead fits within the cgroup limit alongside the guest.
function guest_memory_mb()
    local cores = tonumber(os.getenv("SLURM_CPUS_PER_TASK")) or 1

    local total
    local mem_node = tonumber(os.getenv("SLURM_MEM_PER_NODE"))
    if mem_node then
        total = mem_node
    else
        local mem_cpu = tonumber(os.getenv("SLURM_MEM_PER_CPU"))
        if mem_cpu then
            total = mem_cpu * cores
        end
    end

    if not total then
        -- No memory constraint found; let vmocs use its default.
        return nil
    end

    -- Subtract 5% or 256 MB, whichever is larger, for QEMU overhead.
    local headroom = math.max(256, math.floor(total * 0.05))
    return math.max(512, total - headroom)
end


-- ---------------------------------------------------------------------------
-- SPANK hooks
-- ---------------------------------------------------------------------------

function option_handler(val, optarg, is_remote)
    vm_enabled = true
    vm_option  = optarg
    return SPANK.SUCCESS
end

function slurm_spank_init(spank)
    spank:register_option({
        name    = "vm-image",
        usage   = "Boot a VM with the specified vmocs template name",
        cb      = "option_handler",
        has_arg = 1,
        arginfo = "template_name",
    })
    return SPANK.SUCCESS
end

function slurm_spank_init_post_opt(spank)
    if not vm_enabled then return SPANK.SUCCESS end

    if spank.context == "allocator" then
        -- Persist the template name into the job environment so it survives
        -- across salloc → srun boundaries.
        local rc, msg = spank:job_control_setenv("VMOCS_TEMPLATE", vm_option, 1)
        if rc == nil then
            SPANK.log_error("lua/vmocs: failed to propagate VMOCS_TEMPLATE: %s", msg)
            return SPANK.FAILURE
        end

    elseif spank.context == "remote" then
        local r = replicate_slurm_vars(spank)
        if r ~= 0 then return SPANK.FAILURE end
    end

    return SPANK.SUCCESS
end

-- Runs as the job user inside the Slurm cgroup.
-- QEMU is launched here via 'vmocs launch' (blocking, no --detach).
-- QEMU inherits the cgroup automatically via fork/exec.
-- Slurm considers the task alive until this function returns.
function slurm_spank_task_init(spank)
    if not vm_enabled then return SPANK.SUCCESS end

    local r = replicate_slurm_vars(spank)
    if r ~= 0 then return SPANK.FAILURE end

    local cores  = os.getenv("SLURM_CPUS_PER_TASK") or "1"
    local jobid  = os.getenv("SLURM_JOB_ID")
    local mem_mb = guest_memory_mb()

    local cmd = vmocs_bin()
              .. " launch " .. vm_option
              .. " --cores " .. cores
              .. " --job-id " .. jobid
    if mem_mb then
        cmd = cmd .. " --memory " .. tostring(mem_mb)
    end

    do_and_log_output(cmd)
    return SPANK.SUCCESS
end

-- Called after the task exits. Ensures QEMU is stopped and the runtime
-- dir is cleaned up even if the guest shut down naturally mid-job.
function slurm_spank_exit(spank)
    if not vm_enabled then return SPANK.SUCCESS end

    if spank.context == "remote" then
        local r = replicate_slurm_vars(spank)
        if r ~= 0 then return SPANK.FAILURE end

        local jobid = os.getenv("SLURM_JOB_ID")
        do_and_log_output(vmocs_bin() .. " stop " .. jobid)
    end

    return SPANK.SUCCESS
end
