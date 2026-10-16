import os
import shutil
import subprocess
from pathlib import Path


class HostProbe:
    def read(self, path: str) -> str | None:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        cleaned = text.replace("\x00", "").strip()
        return cleaned or None

    def list(self, path: str) -> list[str] | None:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def link(self, path: str) -> str | None:
        try:
            return Path(os.readlink(path)).name
        except OSError:
            return None

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, argv: list[str], timeout: int) -> tuple[str | None, str | None]:
        name = Path(argv[0]).name
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, f"{name} timed out"
        except FileNotFoundError:
            return None, f"{name} is not available"
        except OSError as exc:
            return None, f"{name}: {exc.strerror or exc}"
        stdout = completed.stdout or ""
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            reason = detail[0] if detail else f"exit {completed.returncode}"
            if len(reason) > 200:
                reason = reason[:200]
            note = f"{name}: {reason}"
            if stdout.strip():
                return stdout, note
            return None, note
        return stdout, None
