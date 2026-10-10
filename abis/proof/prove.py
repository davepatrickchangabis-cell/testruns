import json, os, subprocess, sys
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")
spec = json.loads(os.environ["ABIS_SPEC"])
ws = os.environ["GITHUB_WORKSPACE"]
tmp = os.path.join(os.environ["RUNNER_TEMP"], "abis")
os.makedirs(tmp, exist_ok=True)
proof = os.path.join(ws, "abis", "proof")
env = dict(os.environ)
env.update({"ABIS_PYTHON": sys.executable, "ABIS_RECIPE": os.path.join(proof, "recipe.py"),
            "ABIS_RECIPE_STEPS": os.path.join(proof, "steps.json"), "PYTHONIOENCODING": "utf-8"})
cmd = [sys.executable, os.path.join(ws, "abis", "bin", "abis-buildscan.pyz"),
       "--repo", os.path.join(ws, spec["target"]), "--repo-id", spec["repo_id"],
       "--repo-name", spec["repo_name"], "--config-name", spec["config_name"],
       "--external-config", os.path.join(proof, "config.json"),
       "--commit", spec["commit"], "--no-cache", "--out", os.path.join(tmp, "scan")]
print("$ " + " ".join(cmd), flush=True)
with open(os.path.join(tmp, "buildscan.log"), "w", encoding="utf-8") as log:
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw in p.stdout:
        line = raw.decode("utf-8", "replace")
        sys.stdout.write(line)
        log.write(line)
    rc = p.wait()
with open(os.path.join(tmp, "exit_code"), "w") as f:
    f.write(str(rc))
print("abis-buildscan exited %d" % rc, flush=True)
sys.exit(rc)
