"""Replay cibuildwheel's Linux build of one identifier, inside its own container.

The order and the commands are cibuildwheel's (platforms/linux.py,
build_in_container): the identifier's interpreter first on PATH, the
environment option, before_all, before_build, the frontend with cibuildwheel's
own flags, the repair command. Every value comes from cibw-plan.json, which
cibuildwheel itself resolved on the runner.

It runs from the directory it is started in: the checkout for the traced
build, the reduced bundle for the rebuild. cibuildwheel's container path for the
project is /project and its placeholder is {project}; both mean that directory.

Then it writes the install tree meson's own records describe
(meson-info/intro-installed.json, filtered by the install tags meson-python is
given), which is what meson-python packs into the wheel and what the meson
adapter attributes file by file. The repaired wheel is copied to the output
directory, where step 6 measured it.
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile

try:
    import tomllib
except ImportError:          # Python < 3.11
    tomllib = None


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "build"
    if phase != "build":
        print("[cibw] nothing to run for phase %s" % phase, flush=True)
        return
    with open(os.environ["ABIS_CIBW_PLAN"]) as f:
        plan = json.load(f)
    project = os.getcwd()
    package = os.path.normpath(os.path.join(project, plan["package_dir"]))

    def here(value):
        return re.sub(r"(?<![\w./-])/project(?=/|$|[\"'\s:;])", project, value)

    def fill(cmd, **more):
        # cibuildwheel's prepare_command: these names and no others.
        for k, v in dict(project=project, package=package, **more).items():
            cmd = cmd.replace("{" + k + "}", v)
        return cmd

    env = dict(os.environ)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_ROOT_USER_ACTION"] = "ignore"
    env["PATH"] = plan["python_bin"] + os.pathsep + env.get("PATH", "")
    for a in plan["environment"]:
        value = here(a["value"])
        if a["kind"] == "bash":
            value = subprocess.run(["bash", "-c", 'printf "%s" ' + value], env=env,
                                   capture_output=True, text=True, check=True).stdout
        env[a["name"]] = value
    env["CIBUILDWHEEL_BUILD_IDENTIFIER"] = plan["identifier"]

    def run(cmd, label):
        print("[cibw] %s: %s" % (label, cmd if isinstance(cmd, str) else " ".join(cmd)), flush=True)
        rc = subprocess.call(["sh", "-c", cmd] if isinstance(cmd, str) else cmd, env=env, cwd=project)
        if rc:
            print("[cibw] %s exited %d" % (label, rc), flush=True)
            sys.exit(rc)

    if plan["before_all"]:
        run(fill(plan["before_all"]), "before_all")
    if plan["before_build"]:
        run(fill(plan["before_build"]), "before_build")

    built = tempfile.mkdtemp(prefix="cibw-built-")
    repaired = tempfile.mkdtemp(prefix="cibw-repaired-")
    flags = [fill(x) for x in plan["extra_flags"]]
    frontend = plan["build_frontend"]
    if frontend == "pip":
        run(["python", "-m", "pip", "wheel", package, "--wheel-dir=" + built, "--no-deps", *flags], "build")
    elif frontend in ("build", "build[uv]"):
        uv = ["--installer=uv"] if frontend == "build[uv]" and "--no-isolation" not in flags and "-n" not in flags else []
        run(["python", "-m", "build", package, "--wheel", "--outdir=" + built, *flags, *uv], "build")
    elif frontend == "uv":
        run(["uv", "build", "--python=" + plan["python_bin"] + "/python", package, "--wheel",
             "--out-dir=" + built, *flags], "build")
    else:
        sys.exit("[cibw] unknown build frontend %r" % frontend)
    wheels = glob.glob(os.path.join(built, "*.whl"))
    if len(wheels) != 1:
        sys.exit("[cibw] the build wrote %d wheels, not one" % len(wheels))
    if plan["repair_command"]:
        run(fill(plan["repair_command"], wheel=wheels[0], dest_dir=repaired), "repair")
    else:
        shutil.move(wheels[0], repaired)
    out = glob.glob(os.path.join(repaired, "*.whl"))
    if len(out) != 1:
        sys.exit("[cibw] the repair step wrote %d wheels, not one" % len(out))
    wheelhouse = os.path.join(project, plan["output_dir"])
    os.makedirs(wheelhouse, exist_ok=True)
    shutil.copy2(out[0], wheelhouse)
    print("[cibw] wheel: %s" % os.path.join(plan["output_dir"], os.path.basename(out[0])), flush=True)

    # The install tree, from meson's own records.
    build = os.path.join(project, plan["build_dir"])
    dest = os.path.join(project, plan["install_dir"])
    with open(os.path.join(build, "meson-info", "intro-installed.json")) as f:
        installed = json.load(f)
    tags = None
    if tomllib is not None and os.path.isfile(os.path.join(package, "pyproject.toml")):
        with open(os.path.join(package, "pyproject.toml"), "rb") as f:
            args = ((tomllib.load(f).get("tool") or {}).get("meson-python") or {}).get("args") or {}
        for a in args.get("install") or []:
            if a.startswith("--tags="):
                tags = set(a[len("--tags="):].split(","))
    tag_of = {}
    if tags is not None:
        with open(os.path.join(build, "meson-info", "intro-install_plan.json")) as f:
            for group in json.load(f).values():
                for src, info in group.items():
                    tag_of[src] = info.get("tag")
    shutil.rmtree(dest, ignore_errors=True)
    copied = 0
    for src, target in installed.items():
        if tags is not None and tag_of.get(src) not in tags:
            continue
        landed = os.path.join(dest, os.path.splitdrive(target.replace("\\", "/"))[1].lstrip("/"))
        if os.path.isdir(src):
            shutil.copytree(src, landed, dirs_exist_ok=True, symlinks=True)
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(landed), exist_ok=True)
            shutil.copy2(src, landed)
        else:
            continue
        copied += 1
    print("[cibw] install tree: %d entries from intro-installed.json into %s (tags %s)"
          % (copied, os.path.relpath(dest, project), ",".join(sorted(tags)) if tags else "all"), flush=True)


if __name__ == "__main__":
    main()
