"""Reconstruct Flask folders in the historical flat checkout without overwrites.

The live deployment may already have a newer, correctly arranged layout. This
helper is for local development; it never copies databases, environment files,
credentials or arbitrary repository files into the public static directory.
"""
from pathlib import Path
import shutil


def prepare(root: Path) -> tuple[int, list[str]]:
    candidates = [(p, root / "templates" / p.name) for p in root.glob("*.html")]
    public_names = ("style.css", "app.js", "rtc.js", "sp_logo.png", "brand_identity.png")
    candidates += [(root / name, root / "static" / name) for name in public_names]
    candidates.append((root / "platform.json", root / "seed_data" / "platform.json"))
    copied, conflicts = 0, []
    for source, destination in candidates:
        if not source.is_file():
            continue
        if destination.exists():
            if not destination.is_file() or source.read_bytes() != destination.read_bytes():
                conflicts.append(str(destination.relative_to(root)))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied += 1
    return copied, conflicts


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    copied, conflicts = prepare(root)
    print(f"Prepared {copied} missing Flask files. Existing files were preserved.")
    if conflicts:
        print("Review these differing existing files before running the full host:")
        for path in conflicts:
            print(f"  {path}")
        raise SystemExit(1)
