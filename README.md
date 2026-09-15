# ABIS run repository

Pipeline bundles pushed by an ABIS Enterprise Server. One commit per
run, on a branch named `abis/run/<run id>`. Contains no target source:
each cell checks the target out itself, at a pinned commit, inside the job.
