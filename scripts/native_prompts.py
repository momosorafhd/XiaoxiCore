"""Check, apply or revert the pinned DSH runtime patches in an offline Linux install."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

PATCHES = Path(__file__).resolve().parents[1] / "integrations" / "native-prompts"

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def configure(install, action="check"):
    install = Path(install).resolve(strict=True)
    manifest = json.loads((PATCHES / "manifest.json").read_text(encoding="utf8"))
    # Serialize concurrent runs without leaving a lock file in the DSH tree.
    fd = os.open(install, os.O_RDONLY | os.O_DIRECTORY)
    plan = []
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            states = {}
            for row in manifest["files"]:
                name = row["target"]
                target = install / name
                if target.resolve(strict=True) != target:
                    raise ValueError(f"Symlinked DSH target: {name}. Use a standalone copy.")
                package = json.loads((target.parents[1] / "package.json").read_text(encoding="utf8"))
                if package["version"] != manifest["version"]:
                    raise ValueError(f"Unsupported version for {name}")
                current = digest(target)
                before, after = row["original_sha256"], row["patched_sha256"]
                if current not in {before, after}:
                    raise ValueError(f"Unknown contents: {name}")
                states[name] = "patched" if current == after else "original"
                expected = before if action == "revert" else after
                if action == "check" or current == expected:
                    continue
                copy = scratch / name
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, copy)
                args = ["git", "apply", "--whitespace=nowarn"]
                if action == "revert":
                    args.append("--reverse")
                subprocess.run([*args, str(PATCHES / row["patch"])], cwd=scratch, check=True)
                if digest(copy) != expected:
                    raise ValueError(f"Patched checksum mismatch: {name}")
                plan.append((copy, target, current))
            for copy, target, current in plan:
                if digest(target) != current:
                    raise ValueError(f"Target changed concurrently: {target.name}")
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                    stage = Path(temp.name)
                try:
                    shutil.copy2(target, stage)
                    shutil.copyfile(copy, stage)
                    os.replace(stage, target)
                finally:
                    stage.unlink(missing_ok=True)
                states[str(target.relative_to(install))] = "original" if action == "revert" else "patched"
        return states
    finally:
        os.close(fd)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "revert"))
    parser.add_argument("install", type=Path, help="DSH installation root containing node_modules")
    args = parser.parse_args()
    print(json.dumps(configure(args.install, args.action), indent=2))
