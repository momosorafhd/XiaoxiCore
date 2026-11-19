"""Start the XiaoxiCore platform."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "packages" / "xixi-core"))
from xixi_core.platform.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(main(args[1:] if args[:1] == ["platform"] else args))
