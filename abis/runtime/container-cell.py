"""Container entry point: one process lifetime for preparation and scanning."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


def record(out, stage, code, reason="", state=None):
    (out / "exit_code").write_text(str(code) + "\n")
    event = {"stage": stage, "exit_code": code, "reason": reason,
             "at": datetime.now(timezone.utc).isoformat(), "state": state}
    (out / "execution-status.json").write_text(json.dumps(event, indent=2) + "\n")
    with (out / "execution-history.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")


def execute(plan_path, config_name, source, out, runtime, pipeline, phase="all"):
    stage = "preparation"
    try:
        plan = json.loads(plan_path.read_text())
        if phase != "pipeline":
            record(out, stage, 10, "Preparation has not completed", "environment_preparing")
            code = subprocess.run([plan["container"]["interpreter"],
                str(runtime / "prepare-dependencies.py"), "--plan", str(plan_path),
                "--source", str(source), "--output-root", str(out),
                "--environment", "/work/project-venv", "--execute"], check=False).returncode
            if code:
                record(out, stage, 10, f"Preparation exited {code}; pipeline was not started")
                return 10
            if phase == "prepare":
                record(out, "preparation", 0, "Dependencies prepared", "environment_prepared")
                return 0
        result = json.loads((out / plan["evidence"]["directory"] / "result.json").read_text())
        if result.get("state") != "prepared":
            raise ValueError("Pipeline requires a successful preparation receipt")
        prepared = json.loads((out / plan["evidence"]["directory"] / "environment.json").read_text())
        env = dict(os.environ, **prepared["native_environment"])
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1",
                   PIP_DISABLE_PIP_VERSION_CHECK="1", UV_OFFLINE="1")
        receipt = out / plan["evidence"]["directory"] / "environment.json"
        env["PIP_CONSTRAINT"] = str(receipt.parent / "resolved-constraints.txt")
        env["VIRTUAL_ENV"] = prepared["venv"]
        env["PATH"] = str(Path(prepared["python"]).parent) + os.pathsep + env.get("PATH", "")
        config = json.loads((runtime.parent / "cells" / (config_name + ".json")).read_text())
        config.update(repo=str(source), out=str(out))
        resolved = out / (config_name + ".json")
        resolved.write_text(json.dumps(config, indent=2) + "\n")
        stage = "pipeline"
        record(out, stage, 11, "Pipeline has not completed", "building")
        code = subprocess.run([plan["container"]["interpreter"], str(pipeline),
            "--config", str(resolved), "--prepared-environment", str(receipt)], env=env, cwd=source, check=False).returncode
        record(out, stage, code)
        return code
    except Exception as exc:
        code = 10 if stage == "preparation" else 11
        record(out, stage, code, str(exc))
        return code


if __name__ == "__main__":
    sys.exit(execute(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]),
        Path("/out"), Path("/abis/runtime"), Path("/abis/bin/abis-pipeline.pyz"),
        sys.argv[4] if len(sys.argv) > 4 else "all"))
