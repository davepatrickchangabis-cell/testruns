#!/usr/bin/env python3
"""Read-only target-platform probe; run with the plan's container interpreter.

Produces evidence even on a mismatch, and exits nonzero before a build can run.
The host executor must separately verify the pulled image digest and native CPU;
this script cannot prove image identity or that it is not running under emulation.
Scanner/tool/tracer qualification is separate from this platform probe.
"""
import argparse
import ctypes
import json
import os
import platform
import re
import subprocess
import sys
import sysconfig
from pathlib import Path


def observe():
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    libc, version = "unknown", "unknown"
    try:
        get_version = ctypes.CDLL(None).gnu_get_libc_version
        get_version.restype = ctypes.c_char_p
        version = get_version().decode("ascii")
        libc = "glibc"
    except (AttributeError, OSError):
        # musl's ldd reports its version on stderr and may exit nonzero when
        # called without a file. Do not use that return code as identification.
        try:
            result = subprocess.run(["ldd", "--version"], capture_output=True,
                                    text=True, timeout=10, check=False)
            banner = result.stdout + result.stderr
            match = re.search(r"Version\s+(\d+\.\d+(?:\.\d+)?)", banner)
            if "musl" in banner.lower() and match:
                libc, version = "musl", match.group(1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    python_abi = "unknown"
    if platform.python_implementation() == "CPython":
        python_abi = "cp%d%d%s" % (sys.version_info.major, sys.version_info.minor,
                                  "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else "")
    return {"os": platform.system().lower(), "arch": arch, "libc": libc,
            "libc_version": version, "libc_baseline": ".".join(version.split(".")[:2]),
            "python_abi": python_abi, "python_version": platform.python_version(),
            "interpreter": os.path.realpath(sys.executable)}


def compare(plan, observed):
    refusals = []
    expected = plan["platform"]
    for key in ("os", "arch", "libc", "libc_baseline", "python_abi"):
        if observed[key] == "unknown":
            refusals.append("%s: runtime identification failed" % key)
        elif observed[key] != expected[key]:
            refusals.append("%s: expected %s, observed %s" % (key, expected[key], observed[key]))
    interpreter = plan["container"]["interpreter"]
    if not isinstance(interpreter, str) or not interpreter.startswith("/"):
        refusals.append("interpreter: expected an absolute container path")
    elif observed["interpreter"] != os.path.realpath(interpreter):
        refusals.append("interpreter: running Python does not match the plan")
    return refusals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    observed = observe()
    try:
        plan = json.loads(Path(args.plan).read_text())
        if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
            raise ValueError("unsupported plan schema version")
        refusals = compare(plan, observed)
    except (OSError, ValueError, KeyError, TypeError) as error:
        refusals = ["Invalid environment plan: %s" % error]
    report = {"probe_version": 1, "passed": not refusals,
              "observed": observed, "refusals": refusals}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    return 1 if refusals else 0


if __name__ == "__main__":
    sys.exit(main())
