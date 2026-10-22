#!/usr/bin/env python3
"""Connect the library's skill folders using symlinks."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "check", "remove"])
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sources = sorted(p.parent for p in (root / "skills").rglob("SKILL.md"))
    target = args.target.expanduser().absolute()
    names = [p.name for p in sources]
    if len(names) != len(set(names)):
        parser.error("Duplicate skill folder names in the library")
    plan = []
    for source in sources:
        link = target / source.name
        own = link.is_symlink() and link.resolve() == source.resolve()
        exists = link.exists() or link.is_symlink()
        if exists and not own:
            parser.error(f"Conflict: {link}; existing item preserved")
        if args.action == "check" and not own:
            parser.error(f"Missing connection: {link}")
        plan.append((source, link, own))
    for source, link, own in plan:
        if args.action == "install" and not own:
            print(f"LINK {link} -> {source}")
            if not args.dry_run:
                target.mkdir(parents=True, exist_ok=True)
                link.symlink_to(source, target_is_directory=True)
        elif args.action == "remove" and own:
            print(f"UNLINK {link}")
            if not args.dry_run:
                link.unlink()
        else:
            print(f"OK {link}" if own else f"ABSENT {link}")


if __name__ == "__main__":
    main()
