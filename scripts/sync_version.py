"""Synchronize generated package metadata with the repository VERSION file."""

import argparse
import re
import tomllib
from pathlib import Path


def synchronize(root: Path, *, check: bool) -> list[Path]:
    """Validate all workspace metadata before updating project version fields.

    Args:
        root: Workspace directory containing VERSION and project metadata.
        check: Whether to compare the generated artifact without writing it.

    Returns:
        Project paths whose package version differs from VERSION.

    Raises:
        ValueError: VERSION or project version metadata cannot be safely synchronized.
    """
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("VERSION must contain a release version such as 0.0.1")
    workspace = root / "pyproject.toml"
    config = tomllib.loads(workspace.read_text(encoding="utf-8"))
    members = config["tool"]["uv"]["workspace"]["members"]
    projects = [workspace] + sorted(
        directory / "pyproject.toml" for pattern in members for directory in root.glob(pattern)
    )
    updates: dict[Path, str] = {}
    for project in projects:
        source = project.read_text(encoding="utf-8")
        metadata = tomllib.loads(source)
        if metadata["project"]["version"] == version:
            continue
        section = re.search(r"(?ms)^\[project\][ \t]*(?:\#.*)?\n(?P<body>.*?)(?=^\[|\Z)", source)
        if section is None:
            raise ValueError(f"Missing project table in {project}")
        body, count = re.subn(
            "(?m)^(version[ \\t]*=[ \\t]*)([\"'])[^\\n]*?\\2([ \\t]*(?:\\#.*)?)$",
            lambda match: f"{match[1]}{match[2]}{version}{match[2]}{match[3]}",
            section["body"],
        )
        if count != 1:
            raise ValueError(f"Expected one project version in {project}")
        updates[project] = source[: section.start("body")] + body + source[section.end("body") :]
    # Validation errors must not leave earlier workspace members partly updated.
    if not check:
        for project, updated in updates.items():
            project.write_text(updated, encoding="utf-8")
    return list(updates)


def main() -> None:
    """Dispatch version synchronization and report invalid workspace metadata."""
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
