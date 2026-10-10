"""Run a step 6 recipe's run steps, in order, the way GitHub Actions runs them.

abis-buildscan runs this as the cell's build_cmd ("build") and test_cmd
("test"), each twice: in the checkout (the traced build) and in the reduced
bundle (the rebuild). So every working-directory is relative to the directory
it starts in, and nothing is written inside it: the step scripts and their
GITHUB_ENV and GITHUB_PATH files live under RUNNER_TEMP, where they cannot be
mistaken for build output. What the build steps added to the environment is
handed to the test steps through one file there, as Actions would.
"""
import json, os, shutil, subprocess, sys, tempfile


def executable(name):
    if name == "bash" and os.name == "nt":
        git_bash = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe")
        if os.path.isfile(git_bash):
            return git_bash      # CreateProcess would find WSL's bash in System32 first
    return shutil.which(name) or name


def command(shell, path):
    comspec = os.environ.get("ComSpec", "cmd.exe")
    if shell == "bash":
        return [executable("bash"), "--noprofile", "--norc", "-eo", "pipefail", path]
    if shell == "sh":
        return [executable("sh"), "-e", path]
    if shell in ("pwsh", "powershell"):
        return [executable(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", ". '" + path.replace("'", "''") + "'"]
    if shell == "cmd":
        return [comspec, "/D", "/E:ON", "/V:OFF", "/S", "/C", 'CALL "' + path + '"']
    if shell == "python":
        return [sys.executable, path]
    if shell == "msys2 {0}":
        return [comspec, "/D", "/S", "/C", 'msys2 "' + path + '"']
    raise SystemExit("[recipe] unknown shell " + repr(shell))


def script(shell, body):
    if shell in ("pwsh", "powershell"):
        text = ("$ErrorActionPreference = 'stop'\n" + body
                + "\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\n")
        return ".ps1", text.encode("utf-8-sig")
    if shell == "cmd":
        return ".cmd", body.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    if shell == "python":
        return ".py", body.encode("utf-8")
    return ".sh", body.encode("utf-8")


def text_of(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")      # Windows PowerShell's >> writes UTF-16
    return raw.decode("utf-8-sig", errors="replace")


def read_env(path):
    out, lines, i = {}, text_of(path).splitlines(), 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            name, delim = line.split("<<", 1)
            buf = []
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            i += 1
            out[name] = "\n".join(buf)
        elif "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "build"
    with open(os.environ["ABIS_RECIPE_STEPS"], encoding="utf-8") as f:
        steps = [s for s in json.load(f) if s.get("phase", "build") == phase]
    root = os.getcwd()
    temp = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    carried = os.path.join(temp, "abis-recipe-env.json")
    base = tempfile.mkdtemp(prefix="abis-recipe-", dir=temp)
    env = dict(os.environ)
    if phase != "build" and os.path.isfile(carried):
        with open(carried, encoding="utf-8") as f:
            env.update(json.load(f))
    for n, step in enumerate(steps, 1):
        work = os.path.join(base, "s%d" % n)
        os.makedirs(work)
        ext, data = script(step["shell"], step["run"])
        path = os.path.join(work, "step" + ext)
        with open(path, "wb") as f:
            f.write(data)
        files = {k: os.path.join(work, k.lower()) for k in ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
        for p in files.values():
            open(p, "w").close()
        step_env = dict(env)
        step_env.update(step.get("env") or {})
        step_env.update(files)
        cwd = os.path.join(root, *step["workdir"].split("/")) if step.get("workdir") else root
        print("[recipe] %d/%d %s (%s) in %s" % (n, len(steps), step["name"], step["shell"], cwd), flush=True)
        rc = subprocess.call(command(step["shell"], path), cwd=cwd, env=step_env)
        if rc != 0:
            print("[recipe] step %d %r exited %d" % (n, step["name"], rc), flush=True)
            sys.exit(rc)
        env.update(read_env(files["GITHUB_ENV"]))
        for line in reversed(text_of(files["GITHUB_PATH"]).splitlines()):
            if line.strip():
                env["PATH"] = line.strip() + os.pathsep + env.get("PATH", "")
    if phase == "build":
        with open(carried, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in env.items() if os.environ.get(k) != v}, f)
    print("[recipe] %s: all %d step(s) passed" % (phase, len(steps)), flush=True)


if __name__ == "__main__":
    main()
