"""Build a source-only Plugin Hub ZIP; never package local plugin state."""
from pathlib import Path
import argparse
import hashlib
import json
import zipfile

ROOT_FILES = {"plugin.yaml", "default_config.yaml", "hooks.py", "README.md", "LICENSE", "thumbnail.jpg", ".gitignore"}
SOURCE_DIRS = {"api", "extensions", "helpers", "prompts", "tools", "webui", "tests", "docs", "scripts", "memory_modules"}
EXTENSIONS = {".py", ".md", ".mjs", ".js", ".ts", ".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".yaml", ".yml", ".json"}


def source_files(root):
    root = Path(root).resolve()
    result = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if str(rel) not in ROOT_FILES and rel.parts[0] not in SOURCE_DIRS:
            continue
        if any(p.startswith(".") or p in {"__pycache__", "node_modules", "deps", "dist"} for p in rel.parts) and str(rel) != ".gitignore":
            continue
        if path.is_symlink():
            raise ValueError(f"Source package must not contain symlinks: {rel}")
        if not path.is_file() or path.suffix not in EXTENSIONS and str(rel) not in ROOT_FILES:
            continue
        if path.name in {"config.json", "secrets.json"} or path.name.startswith("secrets."):
            continue
        result.append(path)
    for required in ("plugin.yaml", "README.md", "LICENSE", "hooks.py", "thumbnail.jpg"):
        if root / required not in result:
            raise ValueError(f"Missing release file: {required}")
    return result


def build(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    paths = source_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            name = path.relative_to(root).as_posix()
            data = path.read_bytes()
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
            manifest[name] = hashlib.sha256(data).hexdigest()
    receipt = {"archive": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "files": manifest}
    output.with_suffix(".manifest.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    receipt = build(Path(__file__).resolve().parents[1], args.output)
    print(json.dumps({"archive": str(args.output), "sha256": receipt["sha256"], "file_count": len(receipt["files"])}))
