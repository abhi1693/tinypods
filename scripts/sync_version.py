"""Synchronize generated package metadata with the repository VERSION file."""

import argparse
import re
import tomllib
from pathlib import Path


def synchronize(root: Path, *, check: bool) -> list[Path]:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("VERSION must contain a release version such as 0.0.1")
    workspace = root / "pyproject.toml"
    config = tomllib.loads(workspace.read_text(encoding="utf-8"))
    members = config["tool"]["uv"]["workspace"]["members"]
    projects = [workspace] + sorted(
        directory / "pyproject.toml" for pattern in members for directory in root.glob(pattern)
    )
    changed: list[Path] = []
    for project in projects:
        source = project.read_text(encoding="utf-8")
        metadata = tomllib.loads(source)
        if metadata["project"]["version"] == version:
            continue
        updated, count = re.subn(r'(?m)^version = "[^"]+"$', f'version = "{version}"', source)
        if count != 1:
            raise ValueError(f"Expected one project version in {project}")
        changed.append(project)
        if not check:
            project.write_text(updated, encoding="utf-8")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if metadata needs updating.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        changed = synchronize(args.root, check=args.check)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    if args.check and changed:
        parser.exit(1, "Project versions differ from VERSION; run scripts/sync_version.py\n")


if __name__ == "__main__":
    main()
