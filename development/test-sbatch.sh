#!/bin/bash
#SBATCH --job-name=vmocs-test
#SBATCH --output=/tmp/vmocs-sbatch-%j.out
#SBATCH --error=/tmp/vmocs-sbatch-%j.err
#SBATCH --cpus-per-task=2
#SBATCH --mem=2G
#SBATCH --vm-image=base-ubuntu

echo "Slurm task running on host: $(hostname)"
echo "SLURM_JOB_ID=$SLURM_JOB_ID"
echo "This line runs after task_init returns (i.e. after VM exits)"
