"""Manifest interpretation for runner-side preparation. Never builds the target."""
import hashlib
import re
import shlex
import tomllib
from pathlib import Path


class PreparationError(Exception):
    pass


def contained(root, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or ":" in relative or any(ord(c) < 32 for c in relative)
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise PreparationError("Expected a contained relative path: %r" % relative)
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise PreparationError("Path escapes source root: %s" % relative)
    return path


def read_file(root, relative, evidence):
    path = contained(root, relative)
    content = path.read_bytes()
    evidence[str(path.relative_to(Path(root).resolve()))] = hashlib.sha256(content).hexdigest()
    return content.decode("utf-8")


def requirement(value, forbidden_names=()):
    # packaging is an image-owned tool, not installed into the project to parse
    # its own manifests. Parsing preserves markers for pip in the target venv.
    from packaging.requirements import InvalidRequirement, Requirement
    try:
        parsed = Requirement(value)
    except (InvalidRequirement, TypeError) as error:
        raise PreparationError("Invalid dependency requirement: %r" % value) from error
    if parsed.url and not parsed.url.startswith("https://"):
        raise PreparationError("Local/VCS dependency preparation requires an explicit recipe: %s" % value)
    if normalized(parsed.name) in forbidden_names:
        raise PreparationError("Preparation must not install the target project: %s" % parsed.name)
    return value


def requirements_file(root, relative, evidence, stack=(), forbidden_names=()):
    """Validate includes and preserve hashes; refuse implicit target installs."""
    path = contained(root, relative)
    if path in stack:
        raise PreparationError("Cyclic requirements include: %s" % relative)
    if path.suffix == ".toml":
        raise PreparationError("TOML cannot be used as a pip requirements file")
    text = read_file(root, relative, evidence).replace("\\\n", " ")
    for raw in text.splitlines():
        line = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        include = re.fullmatch(r"(?:-r\s*|--requirement[ =])(.+)", line)
        constraint = re.fullmatch(r"(?:-c\s*|--constraint[ =])(.+)", line)
        if include or constraint:
            parts = shlex.split((include or constraint).group(1))
            if len(parts) != 1:
                raise PreparationError("Invalid requirements include: %s" % line)
            # Resolve from the including file, then enforce root containment.
            included = path.parent / parts[0]
            try:
                nested = str(included.resolve().relative_to(Path(root).resolve()))
            except ValueError as error:
                raise PreparationError("Requirements include escapes source root") from error
            requirements_file(root, nested, evidence, (*stack, path), forbidden_names)
        elif line == "--require-hashes":
            continue
        else:
            # Editable paths, pip options, and a bare '.' are not requirements.
            # Retain ordinary --hash values; pip enforces their correctness.
            cleaned = re.sub(r"\s+--hash(?:=|\s+)sha256:[a-fA-F0-9]{64}", "", line)
            requirement(cleaned, forbidden_names)
    return path


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def target_names(root, evidence):
    if not (Path(root) / "pyproject.toml").is_file():
        return []
    data = tomllib.loads(read_file(root, "pyproject.toml", evidence))
    project = data.get("project", {})
    if not isinstance(project, dict):
        raise PreparationError("project must be a TOML table")
    name = project.get("name")
    return [normalized(name)] if isinstance(name, str) else []


def group_requirements(groups, selected):
    named = {}
    for name, entries in groups.items():
        key = normalized(name)
        if key in named:
            raise PreparationError("Duplicate normalized dependency group: %s" % name)
        if not isinstance(entries, list):
            raise PreparationError("Dependency group must be an array: %s" % name)
        named[key] = entries

    def expand(name, stack):
        key = normalized(name)
        if key in stack or key not in named:
            raise PreparationError("Unknown or cyclic dependency group: %s" % name)
        result = []
        for entry in named[key]:
            if isinstance(entry, str):
                result.append(requirement(entry))
            elif isinstance(entry, dict) and set(entry) == {"include-group"} and isinstance(entry["include-group"], str):
                result.extend(expand(entry["include-group"], (*stack, key)))
            else:
                raise PreparationError("Unsupported dependency group entry: %r" % entry)
        return result

    return [value for name in selected for value in expand(name, ())]


def pyproject_dependencies(data, purpose, groups, extras):
    result = []
    if purpose == "build":
        build = data.get("build-system")
        if not isinstance(build, dict) or not isinstance(build.get("requires"), list):
            raise PreparationError("Explicit build-system.requires is required; no legacy backend is guessed")
        if not isinstance(build.get("build-backend"), str):
            raise PreparationError("Explicit build-system.build-backend is required")
        result.extend(requirement(value) for value in build["requires"])
    project = data.get("project", {})
    if not isinstance(project, dict):
        raise PreparationError("project must be a TOML table")
    if purpose == "runtime":
        if "dependencies" in project.get("dynamic", []):
            raise PreparationError("Dynamic runtime metadata requires an explicit resolved dependency manifest")
        values = project.get("dependencies", [])
        if not isinstance(values, list):
            raise PreparationError("project.dependencies must be an array")
        result.extend(requirement(value) for value in values)
    optional = project.get("optional-dependencies", {})
    if extras and "optional-dependencies" in project.get("dynamic", []):
        raise PreparationError("Dynamic extras require an explicit resolved manifest")
    for extra in extras:
        values = optional.get(extra)
        if not isinstance(values, list):
            raise PreparationError("Unknown extra: %s" % extra)
        result.extend(requirement(value) for value in values)
    result.extend(group_requirements(data.get("dependency-groups", {}), groups))
    return result


def inspect_dependencies(plan, root):
    """Parse selected manifests before the first installation. No backend hooks."""
    evidence, inspected = {}, []
    forbidden_names = target_names(root, evidence)
    for dependency in plan["dependencies"]:
        source = dependency["source"]
        item = {"id": dependency["id"], "purpose": dependency["purpose"], "source": source}
        for citation in dependency["citations"]:
            read_file(root, citation["path"], evidence)
        kind = source["kind"]
        if kind == "system":
            if source["manager"] not in ("apk", "apt", "dnf"):
                raise PreparationError("Unsupported package manager")
            for package in source["packages"]:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:=~<>/-]*", package):
                    raise PreparationError("Invalid system package specification: %s" % package)
        elif kind == "requirements":
            requirements_file(root, source["path"], evidence, forbidden_names=forbidden_names)
        elif kind == "pyproject":
            if Path(source["path"]).name != "pyproject.toml":
                raise PreparationError("pyproject source must name pyproject.toml")
            data = tomllib.loads(read_file(root, source["path"], evidence))
            item["requirements"] = pyproject_dependencies(data, dependency["purpose"], source["groups"], source["extras"])
            for value in item["requirements"]:
                requirement(value, forbidden_names)
            if dependency["purpose"] == "build":
                item["backend"] = data["build-system"]
                if source["backend_requirements"] != "include-dynamic":
                    raise PreparationError("Dynamic backend requirements must be included")
                for path in item["backend"].get("backend-path", []):
                    contained(contained(root, source["path"]).parent, path)
        elif kind == "lockfile":
            path = contained(root, source["path"])
            data = tomllib.loads(read_file(root, source["path"], evidence))
            fmt = source["format"]
            if fmt in ("uv", "poetry"):
                if path.name != fmt + ".lock":
                    raise PreparationError("Lockfile format/path mismatch")
                project_path = str((path.parent / "pyproject.toml").relative_to(Path(root).resolve()))
                read_file(root, project_path, evidence)
            elif fmt == "pylock":
                if not re.fullmatch(r"pylock(?:\.[A-Za-z0-9_-]+)?\.toml", path.name):
                    raise PreparationError("Expected a pylock TOML filename")
                if source["groups"] or source["extras"]:
                    raise PreparationError("pylock must already contain the selected groups/extras")
                if data.get("lock-version") != "1.0":
                    raise PreparationError("Unsupported pylock version")
                for package in data.get("packages", []):
                    if normalized(package["name"]) in forbidden_names:
                        raise PreparationError("pylock includes the target project")
                    if "directory" in package or "vcs" in package:
                        raise PreparationError("Local/VCS pylock entries need an explicit recipe")
                    for artifact in [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else []),
                                     *([package["archive"]] if "archive" in package else [])]:
                        if "path" in artifact or not artifact.get("url", "").startswith("https://"):
                            raise PreparationError("pylock artifacts must use explicit HTTPS URLs")
            else:
                raise PreparationError("Unsupported lockfile format")
        else:
            raise PreparationError("Unsupported dependency source: %s" % kind)
        for constraint in source.get("constraints", []):
            requirements_file(root, constraint, evidence)
        inspected.append(item)
    for citation in plan["native"]["citations"]:
        read_file(root, citation["path"], evidence)
    return inspected, evidence
