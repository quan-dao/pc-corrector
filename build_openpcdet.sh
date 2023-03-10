#!/bin/bash
#SBATCH --job-name=build_openpcdet
#SBATCH --partition=prepost
#SBATCH --qos=qos_gpu-dev
#SBATCH --output=./tools/idr_log/build_openpcdet_%j.out
#SBATCH --error=./tools/idr_log/build_openpcdet_%j.err
#SBATCH --time=00:20:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=12

module purge
conda deactivate

module load pytorch-gpu/py3/1.7.0+hvd-0.21.0

set -x
srun python setup.py develop --user
