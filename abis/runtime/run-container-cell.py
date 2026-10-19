"""Host-side Docker orchestration. Executes only bundled, qualified profiles."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import uuid


def read(path):
    return json.loads(path.read_text())


def run(workspace, name, phase="all"):
    out = workspace / "out"
    out.mkdir(exist_ok=True)
    container = "abis-" + uuid.uuid4().hex
    stage = "validation"
    keep = False
    def status(reason):
        (out / "exit_code").write_text("11\n")
        (out / "execution-status.json").write_text(json.dumps({
            "stage": stage, "exit_code": 11, "reason": reason}, indent=2) + "\n")
    def docker(*args):
        result = subprocess.run(["docker", *args], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, check=False)
        with (out / "container.log").open("a") as log:
            log.write(result.stdout)
        if result.returncode:
            raise RuntimeError(f"docker {args[0]} exited {result.returncode}; see container.log")
        return result.stdout
    status("Container execution has not completed")
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", name):
            raise ValueError("Unsafe configuration name")
        abis = workspace / "abis"
        metadata = read(abis / "run.json")
        cell = next(c for c in read(abis / "matrix.json")["include"] if c["config_name"] == name)
        relative = f"abis/environments/{name}.json"
        if cell["environment_path"] != relative:
            raise ValueError("Environment path mismatch")
        data = (workspace / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != cell["environment_sha256"]:
            raise ValueError("Environment digest mismatch")
        plan = json.loads(data)
        expected = {"content_key": cell["content_key"], "source_commit": metadata["target"]["sha"],
                    "preparation_id": metadata["prepare_id"], "module_digest": metadata["module"]["digest"]}
        if plan["identity"] != expected:
            raise ValueError("Environment identity mismatch")
        profile = next(p for p in read(abis / "runtime/environment-profiles.json")
                       if p["id"] == plan["container"]["profile_id"])
        if profile["qualification"] != "qualified":
            raise ValueError("Runtime qualification pending for " + profile["id"])
        if any(plan["platform"][k] != profile[k] for k in ("os", "arch", "libc", "libc_baseline")):
            raise ValueError("Profile target mismatch")
        for key, value in {"image": profile["image"], "platform": profile["oci_platform"],
                           "interpreter": profile["interpreters"][plan["platform"]["python_abi"]]}.items():
            if plan["container"][key] != value:
                raise ValueError("Profile container mismatch: " + key)
        arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine().lower())
        if platform.system() != "Linux" or arch != profile["arch"]:
            raise ValueError("Native Linux runner architecture mismatch")
        if cell["runner"] != profile["runner"] or plan["runner"] != profile["runner"]:
            raise ValueError("Runner selection mismatch")
        repo = metadata["target"]["repo"]
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", repo) or repo in ("abis", "out"):
            raise ValueError("Unsafe target directory")
        source = workspace / repo
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if head != metadata["target"]["sha"]:
            raise ValueError("Source checkout does not match pinned commit")
        (out / "environment-plan.json").write_bytes(data)
        stage = "image"
        status("Image provisioning has not completed")
        image = profile["image"]
        if not re.search(r"@sha256:[a-f0-9]{64}$", image):
            raise ValueError("Image is not digest pinned")
        docker("pull", "--platform", profile["oci_platform"], image)
        info = json.loads(docker("image", "inspect", image))[0]
        (out / "container-image.json").write_text(json.dumps(info, indent=2) + "\n")
        if (info["Os"] != "linux" or "linux/" + info["Architecture"] != profile["oci_platform"]
                or image not in info["RepoDigests"] or info["Id"] != profile["registry_verification"]["config_digest"]):
            raise ValueError("Pulled image digest/platform mismatch")
        stage = "container"
        status("Container has not completed")
        # Written before create so the workflow's always cleanup also handles cancellation.
        (workspace / ".abis-container-name").write_text(container)
        docker("create", "--name", container, "--init", "--platform", profile["oci_platform"],
               "--mount", f"type=bind,src={source},dst=/work/{repo}",
               "--mount", f"type=bind,src={abis},dst=/abis,readonly",
               "--mount", f"type=bind,src={out},dst=/out",
               "--entrypoint", plan["container"]["interpreter"], image,
               *(["-c", "import time; time.sleep(21600)"] if phase == "provision" else
                 ["/abis/runtime/container-cell.py", f"/abis/environments/{name}.json", name, f"/work/{repo}"]))
        if phase == "provision":
            docker("start", container)
            (out / "execution-status.json").write_text(json.dumps({
                "stage": "provisioning", "exit_code": 11,
                "reason": "Container provisioned; dependency preparation did not complete",
                "state": "environment_preparing"}, indent=2) + "\n")
            keep = True
            return 0
        # docker start --attach returns the container exit status, including findings.
        with (out / "container-output.log").open("w") as log:
            subprocess.run(["docker", "start", "--attach", container], stdout=log,
                           stderr=subprocess.STDOUT, check=False)
        inspected = json.loads(docker("inspect", container))[0]["State"]
        (out / "container-state.json").write_text(json.dumps(inspected, indent=2) + "\n")
        result = read(out / "execution-status.json")
        if (result["stage"] not in ("preparation", "pipeline") or inspected["Running"]
                or inspected.get("OOMKilled") or inspected["ExitCode"] != result["exit_code"]):
            raise ValueError("Container did not report a complete preparation/pipeline result")
        return result["exit_code"]
    except Exception as exc:
        status(str(exc))
        return 11
    finally:
        if not keep and (workspace / ".abis-container-name").exists():
            try:
                docker("rm", "--force", container)
            except Exception as exc:
                (out / "cleanup-error.txt").write_text(str(exc))


def execute_phase(workspace, name, phase):
    """Docker exec stages retain the provisioned container and project venv."""
    out = workspace / "out"
    stage = "preparation" if phase == "prepare" else "pipeline"
    try:
        if phase not in ("prepare", "pipeline") or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", name):
            raise ValueError("Invalid execution phase/configuration")
        container = (workspace / ".abis-container-name").read_text()
        if not re.fullmatch(r"abis-[a-f0-9]{32}", container):
            raise ValueError("Invalid container identifier")
        plan = read(out / "environment-plan.json")
        original = workspace / "abis/environments" / (name + ".json")
        if original.read_bytes() != (out / "environment-plan.json").read_bytes():
            raise ValueError("Environment plan changed after provisioning")
        repo = read(workspace / "abis/run.json")["target"]["repo"]
        if phase == "pipeline":
            receipt = read(out / "execution-status.json")
            if receipt.get("stage") != "preparation" or receipt.get("exit_code") != 0:
                raise ValueError("Preparation did not succeed; pipeline was not started")
        with (out / (phase + "-container.log")).open("w") as log:
            result = subprocess.run(["docker", "exec", container, plan["container"]["interpreter"],
                "/abis/runtime/container-cell.py", f"/abis/environments/{name}.json", name,
                f"/work/{repo}", phase], stdout=log, stderr=subprocess.STDOUT, check=False)
        receipt = read(out / "execution-status.json")
        if receipt.get("stage") != stage or result.returncode != receipt.get("exit_code"):
            raise ValueError("Container phase did not report a matching result")
        return result.returncode
    except Exception as exc:
        code = 10 if phase == "prepare" else 11
        (out / "exit_code").write_text(str(code) + "\n")
        (out / "execution-status.json").write_text(json.dumps({"stage": stage,
            "exit_code": code, "reason": str(exc)}, indent=2) + "\n")
        return code


if __name__ == "__main__":
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    name = os.environ["ABIS_CONFIG_NAME"]
    phase = sys.argv[1] if len(sys.argv) > 1 else "all"
    sys.exit(execute_phase(workspace, name, phase) if phase in ("prepare", "pipeline") else run(workspace, name, phase))
