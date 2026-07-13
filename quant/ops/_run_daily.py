"""launchd-Entry: führt die tägliche Daten-Pipeline (daily.sh) aus."""
import os, subprocess
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
subprocess.run(["/bin/zsh", os.path.join(REPO, "quant", "ops", "daily.sh")],
               cwd=REPO, check=False)
