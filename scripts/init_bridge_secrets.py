"""Create private bridge settings for a new, single-user Linux installation."""
import argparse
import json
import os
from pathlib import Path
import secrets

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="A new directory outside the repository")
    args = parser.parse_args()
    path = args.directory.expanduser().resolve()
    repo = Path(__file__).resolve().parents[1]
    if path.is_relative_to(repo):
        parser.error("Choose a directory outside the repository")
    os.umask(0o077)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)  # Exclusive creation protects prior credentials.
    workspace = path / "workspace"
    workspace.mkdir()
    token = secrets.token_urlsafe(36)
    (path / "core.env").write_text("XIXI_DSH_BRIDGE_TOKEN=" + token + "\n", encoding="ascii")
    bridge = {"root": str(path / "ledger"), "cwd": str(workspace),
              "coreUrl": "http://127.0.0.1:18910", "port": 18766, "token": token}
    (path / "bridge.json").write_text(json.dumps(bridge, indent=2) + "\n", encoding="utf-8")
    (path / "onebot.token").write_text(secrets.token_urlsafe(36) + "\n", encoding="ascii")
    print("Created bridge.json, core.env and onebot.token in the specified private directory.")

if __name__ == "__main__":
    main()
