#!/usr/bin/env python3
"""Inspect or prepare dependencies in an already selected target container.

Default mode only inspects source manifests. --execute installs into a new,
explicit project venv and can install native packages in the container. It must
be called only after the server's schema/profile/citation gates. This script
does not enable pending profiles or invoke the target build.
"""
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time

from dependency_support import (PreparationError, contained, inspect_dependencies,
                                requirement, requirements_file, target_names)


# The hook is called by the project interpreter, in a subprocess whose output
# and deadline are managed like every other preparation command. Calling a
# dependency hook can execute backend code, but never calls build_wheel/sdist.
BACKEND_HOOK = r'''
import importlib, json, pathlib, sys, tomllib
source, output = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2])
build = tomllib.loads((source / "pyproject.toml").read_text())["build-system"]
paths = [(source / p).resolve() for p in build.get("backend-path", [])]
if any(not p.is_relative_to(source) for p in paths):
    raise ValueError("backend-path escapes project")
sys.path = [p for p in sys.path if p and pathlib.Path(p).resolve() != source]
sys.path[:0] = [str(p) for p in paths]
module, _, obj = build["build-backend"].partition(":")
backend = importlib.import_module(module)
if paths and not any(pathlib.Path(backend.__file__).resolve().is_relative_to(p) for p in paths):
    raise ValueError("backend module was not loaded from backend-path")
for part in obj.split(".") if obj else []:
    backend = getattr(backend, part)
hook = getattr(backend, "get_requires_for_build_wheel", None)
value = hook(config_settings=None) if hook else []
if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
    raise ValueError("backend dependency hook must return a list of strings")
output.write_text(json.dumps(value))
'''


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


class Executor:
    def __init__(self, plan, root, output, environment):
        self.plan = plan
        self.root, self.output, self.environment = map(lambda p: Path(p).resolve(), (root, output, environment))
        self.python = self.environment / "bin" / "python"
        self.deadline = time.monotonic() + plan["preparation"]["timeout_seconds"]
        self.commands = []
        self.env = {**os.environ, **plan["native"]["environment"],
                    "VIRTUAL_ENV": str(self.environment), "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "PYTHONNOUSERSITE": "1", "UV_PYTHON_DOWNLOADS": "never"}
        self.env["PATH"] = str(self.environment / "bin") + os.pathsep + self.env.get("PATH", "")
        self.constraint = self.output / "resolved-constraints.txt"
        self.resolutions = []
        self.active = "initialization"
        self.forbidden_names = target_names(self.root, {})

    def run(self, argv, cwd=None, extra_env=None):
        argv = [str(arg) for arg in argv]
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PreparationError("Preparation timeout exceeded")
        index = len(self.commands) + 1
        log = self.output / "logs" / ("%03d.log" % index)
        stderr = self.output / "logs" / ("%03d.stderr.log" % index)
        log.parent.mkdir(exist_ok=True)
        event = {"operation": self.active, "argv": argv, "cwd": str(cwd or self.root),
                 "log": str(log.relative_to(self.output)), "stderr": str(stderr.relative_to(self.output)), "exit_code": None}
        self.commands.append(event)
        started = time.monotonic()
        event["started_at"] = datetime.now(timezone.utc).isoformat()
        try:
            with log.open("w") as stream, stderr.open("w") as errors:
                child = subprocess.Popen(argv, cwd=cwd or self.root,
                    env={**self.env, **(extra_env or {})}, stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=errors, start_new_session=True)
                try:
                    code = child.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                    event["timed_out"] = True
                    raise PreparationError("Preparation command timed out: %s" % argv[0])
                event["exit_code"] = code
                if code:
                    raise PreparationError("%s failed (exit %s); see %s" % (self.active, code, event["log"]))
        except OSError as error:
            raise PreparationError("%s: cannot run %s: %s" % (self.active, argv[0], error)) from error
        finally:
            event["completed_at"] = datetime.now(timezone.utc).isoformat()
            event["duration_seconds"] = round(time.monotonic() - started, 3)
            write_json(self.output / "commands.json", self.commands)
        return log

    def freeze(self):
        log = self.run([self.python, "-m", "pip", "freeze", "--all"])
        self.constraint.write_text(log.read_text())

    def install(self, args, constraints=(), locked=False):
        report = self.output / ("pip-report-%03d.json" % (len(self.commands) + 1))
        argv = [self.python, "-m", "pip", "install", "--report", report]
        if locked:
            argv += ["--no-deps", "--require-hashes"]
        for path in constraints:
            argv += ["--constraint", contained(self.root, path)]
        if self.constraint.exists():
            argv += ["--constraint", self.constraint]
        self.run([*argv, *args])
        self.freeze()

    def install_dependency(self, item):
        source, kind = item["source"], item["source"]["kind"]
        if kind == "system":
            manager, packages = source["manager"], source["packages"]
            if manager == "apt":
                self.run(["apt-get", "update"])
                self.run(["apt-get", "install", "-y", "--", *packages], extra_env={"DEBIAN_FRONTEND": "noninteractive"})
            elif manager == "apk":
                self.run(["apk", "add", "--no-cache", "--", *packages])
            else:
                self.run(["dnf", "install", "-y", "--", *packages])
            return
        path = contained(self.root, source["path"])
        if kind == "requirements":
            self.install(["--requirement", path], source["constraints"])
        elif kind == "pyproject":
            requested = self.output / (item["id"] + "-requirements.txt")
            requested.write_text("\n".join(item["requirements"]) + "\n")
            if item["requirements"]:
                self.install(["--requirement", requested], source["constraints"])
            if "backend" in item:
                dynamic_path = self.output / (item["id"] + "-backend-requires.json")
                self.run([self.python, "-c", BACKEND_HOOK, path.parent, dynamic_path], cwd=path.parent)
                dynamic = json.loads(dynamic_path.read_text())
                values = [requirement(value, self.forbidden_names) for value in dynamic]
                item["dynamic_backend_requirements"] = values
                if values:
                    dynamic_file = self.output / (item["id"] + "-backend-requirements.txt")
                    dynamic_file.write_text("\n".join(values) + "\n")
                    self.install(["--requirement", dynamic_file], source["constraints"])
                # No target wheel is produced during preparation.
        else:
            self.install_lock(item, path)
        self.resolutions.append({"id": item["id"], "source_kind": kind,
                                 "upstream_lock": kind == "lockfile"})

    def install_lock(self, item, path):
        source = item["source"]
        fmt = source["format"]
        original = path.read_bytes()
        exported = self.output / (item["id"] + "-locked-requirements.txt")
        if fmt == "uv":
            self.run(["uv", "--version"])
            command = ["uv", "export", "--locked", "--no-default-groups", "--no-emit-project",
                       "--format", "requirements-txt", "--output-file", exported]
            for group in source["groups"]:
                command += ["--group", group]
            for extra in source["extras"]:
                command += ["--extra", extra]
            self.run(command, cwd=path.parent)
        elif fmt == "poetry":
            self.run(["poetry", "--version"])
            self.run(["poetry", "check", "--lock"], cwd=path.parent)
            command = ["poetry", "export", "--format", "requirements.txt", "--output", exported,
                       "--only", ",".join(["main", *source["groups"]])]
            for extra in source["extras"]:
                command += ["--extras", extra]
            self.run(command, cwd=path.parent)
        else:
            self.run(["uv", "--version"])
            command = ["uv", "pip", "install", "--python", self.python, "--requirement", path]
            if self.constraint.exists():
                command += ["--constraint", self.constraint]
            self.run(command)
            self.freeze()
        if path.read_bytes() != original:
            raise PreparationError("Lockfile changed during preparation: %s" % path)
        if fmt != "pylock":
            requirements_file(self.output, exported.name, {}, forbidden_names=self.forbidden_names)
            self.install(["--requirement", exported], locked=True)

    def execute(self, inspected):
        if self.environment.exists():
            raise PreparationError("Project environment must be new; refusing to reuse %s" % self.environment)
        if self.environment.is_relative_to(self.root) or self.root.is_relative_to(self.environment):
            raise PreparationError("Project environment must be outside the target source tree")
        if self.environment.is_relative_to(self.output) or self.output.is_relative_to(self.environment):
            raise PreparationError("Project environment must be outside the evidence directory")
        self.run([self.plan["container"]["interpreter"], "-m", "venv", self.environment])
        by_id = {item["id"]: item for item in inspected}
        for op in self.plan["preparation"]["operations"]:
            self.active = op["id"]
            cwd = contained(self.root, op["cwd"])
            if op["kind"] == "install-dependencies":
                for identity in op["dependency_ids"]:
                    self.install_dependency(by_id[identity])
            elif op["kind"] == "repository-script":
                self.run([contained(self.root, op["path"]), *op["args"]], cwd=cwd)
            else:
                self.run(op["argv"], cwd=cwd)
        self.active = "verify-dependencies"
        self.run([self.python, "-m", "pip", "check"])
        self.freeze()
        inventory = self.run([self.python, "-m", "pip", "list", "--format=json"])
        write_json(self.output / "python-packages.json", json.loads(inventory.read_text()))
        for manager in sorted({i["source"]["manager"] for i in inspected if i["source"]["kind"] == "system"}):
            command = {"apt": ["dpkg-query", "-W"], "apk": ["apk", "info", "-v"],
                       "dnf": ["rpm", "-qa"]}[manager]
            inventory = self.run(command)
            (self.output / (manager + "-packages.txt")).write_text(inventory.read_text())
        versions = {"interpreter": str(self.python), "compilers": []}
        for compiler in self.plan["native"].get("compilers", []):
            executable = shutil.which(compiler, path=self.env["PATH"])
            version = self.run([executable, "--version"]).read_text() if executable else None
            versions["compilers"].append({"requested": compiler, "executable": executable, "version": version})
        versions["python"] = self.run([self.python, "--version"]).read_text().strip()
        write_json(self.output / "tool-versions.json", versions)
        write_json(self.output / "environment.json", {"python": str(self.python),
            "venv": str(self.environment), "native_environment": self.plan["native"]["environment"],
            "inventory": str(self.output / "python-packages.json"),
            "inventory_sha256": hashlib.sha256((self.output / "python-packages.json").read_bytes()).hexdigest(),
            "target_names": self.forbidden_names,
            "native_managers": sorted({i["source"]["manager"] for i in inspected if i["source"]["kind"] == "system"}),
            "build_isolation": self.plan["preparation"]["build_isolation"],
            "isolated_build_inventory_required": self.plan["preparation"]["build_isolation"] == "observe-isolated"})


def preflight(plan, root):
    """Validate executable references even when invoked outside the server."""
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise PreparationError("Unsupported environment plan version")
    timeout = plan["preparation"]["timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 21600:
        raise PreparationError("Invalid preparation timeout")
    if plan["preparation"]["build_isolation"] not in ("observe-isolated", "prepared-no-isolation"):
        raise PreparationError("Unsupported build isolation policy")
    ids, installed, operation_ids = set(), set(), set()
    for dependency in plan["dependencies"]:
        identity = dependency["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", identity) or identity in ids:
            raise PreparationError("Invalid or duplicate dependency id")
        if dependency["purpose"] not in ("build", "test", "runtime"):
            raise PreparationError("Invalid dependency purpose")
        ids.add(identity)
    for operation in plan["preparation"]["operations"]:
        identity = operation["id"]
        if identity in operation_ids:
            raise PreparationError("Duplicate operation id")
        operation_ids.add(identity)
        contained(root, operation["cwd"])
        if operation["kind"] == "install-dependencies":
            for identity in operation["dependency_ids"]:
                if identity not in ids or identity in installed:
                    raise PreparationError("Unknown or repeated dependency installation")
                installed.add(identity)
        elif operation["kind"] == "repository-script":
            script = contained(root, operation["path"])
            if not script.is_file() or not any(c["path"] == operation["path"] for c in operation["citations"]):
                raise PreparationError("Repository script must exist and cite itself")
            for citation in operation["citations"]:
                contained(root, citation["path"]).read_text()
        elif operation["kind"] != "probe":
            raise PreparationError("Unknown preparation operation")
    if installed != ids:
        raise PreparationError("Missing dependency installation operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    # A fixed fallback keeps malformed-plan failures inspectable.
    output = Path(args.output_root).resolve() / "preparation"
    output.mkdir(parents=True, exist_ok=True)
    executor = None
    report = {"schema_version": 1, "state": "failed", "refusals": [],
              "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        raw = Path(args.plan).read_bytes()
        plan = json.loads(raw)
        output = contained(args.output_root, plan["evidence"]["directory"])
        output.mkdir(parents=True, exist_ok=True)
        report["plan_sha256"] = hashlib.sha256(raw).hexdigest()
        write_json(output / "plan.json", plan)
        preflight(plan, args.source)
        inspected, evidence = inspect_dependencies(plan, args.source)
        write_json(output / "inputs.json", evidence)
        write_json(output / "dependency-discovery.json", inspected)
        if args.execute:
            spec = importlib.util.spec_from_file_location("environment_probe", Path(__file__).with_name("environment-probe.py"))
            probe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(probe)
            observed = probe.observe()
            failures = probe.compare(plan, observed)
            write_json(output / "platform.json", {"observed": observed, "refusals": failures, "passed": not failures})
            if failures:
                raise PreparationError("Platform verification failed: " + "; ".join(failures))
            executor = Executor(plan, args.source, output, args.environment)
            executor.execute(inspected)
            write_json(output / "dependency-resolution.json", executor.resolutions)
            write_json(output / "dependency-discovery.json", inspected)
            report["state"] = "prepared"
        else:
            report["state"] = "inspected"
    except (PreparationError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        report["refusals"].append(str(error))
        if executor:
            report["failed_operation"] = executor.active
    if executor:
        write_json(output / "dependency-resolution.json", executor.resolutions)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "result.json", report)
    return 1 if report["state"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
