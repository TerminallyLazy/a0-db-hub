"""Plugin-owned dependency management for db_hub.

Third-party DB client libraries install into `usr/plugins/db_hub/deps/` (a
pip `--target` dir), never into the framework venv, so uninstall can remove
exactly what this plugin added. Only *third-party packages* resolve from
`deps/`; plugin-local code always imports via `usr.plugins.db_hub...`.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import importlib
import json
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

PLUGIN = "db_hub"

# Installed at plugin install time (common, small wheels).
BASE_DEPS = [
    "asyncpg==0.31.0",
    "qdrant-client==1.19.1",
    "neo4j==6.4.0",
]

# Installed lazily on first use of the owning driver (heavy wheels).
OPTIONAL_DEPS = {
    "snowflake": ["snowflake-connector-python>=3.10"],
    "lancedb": ["lancedb==0.39.0"],
    "sqlalchemy": ["sqlalchemy>=2.0"],
    "redis": ["redis>=5.0"],
    "mongodb": ["pymongo>=4.0"],
    "pinecone": ["pinecone>=3.0"],
    "chromadb": ["chromadb==1.5.9"],
    "milvus": ["pymilvus>=2.4"],
    "duckdb": ["duckdb==1.5.6"],
    "bigquery": ["google-cloud-bigquery>=3.0"],
    "weaviate": ["weaviate-client>=4.0"],
}

MANIFEST_NAME = ".manifest.json"
_path_ready = False
_INSTALL_LOCK = threading.RLock()


def deps_dir() -> str:
    from helpers import files
    from helpers.plugins import find_plugin_dir

    plugin_dir = find_plugin_dir(PLUGIN) or os.path.join("usr", "plugins", PLUGIN)
    return files.get_abs_path(plugin_dir, "deps")


def ensure_deps_path() -> str:
    """Add third-party packages after the framework's site packages.

    This is not an isolated interpreter: shared dependencies must match the
    framework. Never shadow its numpy, tokenizers, or OpenTelemetry modules.
    """
    global _path_ready
    path = deps_dir()
    if not _path_ready or path not in sys.path:
        while path in sys.path:
            sys.path.remove(path)  # repair the previous prepend policy
        site.addsitedir(path)
    _path_ready = True
    return path


def manifest_path() -> str:
    from helpers import files

    return files.get_abs_path(deps_dir(), MANIFEST_NAME)


def read_manifest() -> dict:
    from helpers import files

    path = manifest_path()
    if not files.exists(path):
        return {}
    try:
        return json.loads(files.read_file(path) or "{}")
    except Exception:
        return {}


def framework_versions() -> dict[str, str]:
    """Only the owning interpreter's packages, never plugin target directories."""
    paths = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib"), *site.getsitepackages()}
    return {canonicalize_name(d.metadata["Name"]): d.version
            for d in importlib.metadata.distributions(path=[p for p in paths if p])
            if d.metadata.get("Name")}


def _requested_specs(specs, manifest):
    known = BASE_DEPS + [s for values in OPTIONAL_DEPS.values() for s in values]
    available = {canonicalize_name(Requirement(s).name): s for s in known}
    # Legacy manifests recorded only direct package names. Preserve those extras
    # while upgrading their specification to this release's tested version.
    chosen = {name: available[name] for name in
              (canonicalize_name(n) for n in manifest.get("packages", {})) if name in available}
    for spec in [*BASE_DEPS, *specs]:
        req = Requirement(spec)
        if req.url:
            raise ValueError("Database driver dependencies must be registry packages.")
        chosen[canonicalize_name(req.name)] = spec
    return sorted(chosen.values())


def pip_install(specs: list[str], timeout_s: int = 600) -> None:
    """Resolve against the framework, then replace the complete private target.

    A failed resolution leaves the previous target intact. Rebuilding prevents
    mixed package versions left behind by repeated pip --target installs.
    No command installs into or upgrades the framework environment.
    """
    import fcntl  # Agent Zero's supported Linux/macOS framework runtimes
    target = Path(deps_dir())
    target.parent.mkdir(parents=True, exist_ok=True)
    with _INSTALL_LOCK, (target.parent / ".deps.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        requested = _requested_specs(specs, read_manifest())
        shared = framework_versions()
        with tempfile.TemporaryDirectory(prefix=".deps-install-", dir=target.parent) as work:
            work = Path(work)
            constraints = work / "framework.txt"
            text = "".join(f"{name}=={version}\n" for name, version in sorted(shared.items()))
            # Recent Arrow wheels require the NumPy 2 ABI without declaring a
            # resolver-visible NumPy floor. Keep the tested compatible Arrow
            # range when the framework still supplies NumPy 1.x.
            if 'numpy' in shared and Version(shared['numpy']) < Version('2'):
                text += 'pyarrow<24\n'
            constraints.write_text(text)
            staged = work / "packages"
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "--target", str(staged), "--constraint", str(constraints), *requested],
                text=True, capture_output=True, timeout=timeout_s,
            )
            if result.returncode:
                # pip output can contain authenticated registry/proxy URLs.
                raise RuntimeError("Database driver dependencies cannot be resolved against this Agent Zero environment. The previous installation was retained; no framework packages were changed.")
            installed = {canonicalize_name(d.metadata["Name"]): d.version
                         for d in importlib.metadata.distributions(path=[str(staged)]) if d.metadata.get("Name")}
            conflicts = [name for name, version in installed.items() if name in shared and version != shared[name]]
            if conflicts:
                raise RuntimeError("Driver installation would conflict with framework packages: " + ", ".join(conflicts))
            (staged / MANIFEST_NAME).write_text(json.dumps({"format": 2, "requests": requested,
                "packages": {Requirement(s).name: installed.get(canonicalize_name(Requirement(s).name), shared.get(canonicalize_name(Requirement(s).name))) for s in requested},
                "resolved": installed, "framework": shared}, indent=2))
            previous = target.parent / ".deps-previous"
            if previous.exists():
                shutil.rmtree(previous)
            if target.exists():
                target.rename(previous)
            try:
                staged.rename(target)
            except BaseException:
                if previous.exists():
                    previous.rename(target)
                raise
        importlib.invalidate_caches()
        ensure_deps_path()


def record_manifest(specs: list[str]) -> dict:
    from helpers import files

    ensure_deps_path()
    manifest = read_manifest()
    installed = manifest.get("packages", {})
    for spec in specs:
        name = spec.split(">")[0].split("<")[0].split("=")[0].split(";")[0].strip()
        try:
            installed[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed[name] = "unknown"
    manifest["packages"] = installed
    files.write_file(manifest_path(), json.dumps(manifest, indent=2))
    return manifest


def is_importable(module_name: str) -> bool:
    ensure_deps_path()
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, AttributeError):
        # find_spec raises (instead of returning None) when a dotted
        # parent package is missing entirely, e.g. "snowflake.connector".
        return False


def check_import(module_name: str) -> bool:
    ensure_deps_path()
    try:
        importlib.import_module(module_name)
        return True
    except ImportError as exc:
        raise RuntimeError(f"Database package '{module_name}' could not load. Use Install drivers to repair dependencies, then restart Agent Zero.") from exc


def ensure_driver_deps(driver_name: str) -> None:
    """Guarantee a driver's third-party package imports, installing lazily.

    Raises an actionable error (never a bare ImportError) when install fails.
    """
    from usr.plugins.db_hub.helpers import drivers as registry

    info = registry.get_driver_info(driver_name)
    module = info["import"]
    recorded = read_manifest()
    if recorded.get("format") == 2:
        shared = framework_versions()
        changed = [name for name, version in recorded.get("resolved", {}).items()
                   if name in shared and shared[name] != version]
        if changed:
            raise RuntimeError("Agent Zero's shared packages changed. Use Install drivers to rebuild compatible database dependencies, then restart Agent Zero.")
    if is_importable(module):
        check_import(module)
        return
    specs = list(OPTIONAL_DEPS.get(driver_name, info.get("pip", [])))
    if not specs:
        raise RuntimeError(
            f"db_hub driver '{driver_name}' needs the '{module}' package, "
            "which is not installed and has no known pip spec. "
            "Install it manually into the framework runtime."
        )
    try:
        pip_install(specs)
        record_manifest(specs)
    except Exception as exc:
        raise RuntimeError(
            f"db_hub driver '{driver_name}' needs package '{module}'. "
            f"Automatic install failed ({exc}). Install manually with: "
            f"{sys.executable} -m pip install --target {deps_dir()} {' '.join(specs)}"
        ) from exc
    if not is_importable(module):
        raise RuntimeError(
            f"db_hub installed {' '.join(specs)} but '{module}' still does not "
            "import. Check for a conflicting package in the framework runtime."
        )
    check_import(module)
