"""The image must carry every module the hub imports (a missing one stops the hub at start)."""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_hub_module_is_copied_into_the_image():
    copied = set()
    for line in open(os.path.join(HERE, "Dockerfile")):
        if line.startswith("COPY ") and "/app/relay/" in line and line.split()[-1] == "/app/relay/":
            copied |= set(line.split()[1:-1])
    modules = {f for f in os.listdir(HERE) if f.endswith(".py")}
    missing = sorted(m for m in modules - copied if m not in ("main.py",))
    assert not missing, f"add to the Dockerfile COPY line: {missing}"
