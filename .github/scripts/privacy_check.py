"""Fail the build if a tracked file looks like it contains personal data.

The repo is public: real data belongs only in private/ (gitignored). This scan uses generic patterns only - it must
never contain anyone's real name, email or address itself.
"""
import re
import subprocess
import sys

# Files that are allowed to mention patterns below (the scan itself, and binary templates).
SKIP = {".github/scripts/privacy_check.py"}
BINARY = (".pdf", ".png", ".jpg", ".ico", ".exe")

CHECKS = [
    # A real Windows user folder. Placeholders like C:\Users\<username>\ are fine.
    ("personal Windows path", re.compile(r"C:\\{1,2}Users\\{1,2}(?!<)(?!Public\b)(?!Default\b)[A-Za-z0-9._-]+", re.I)),
    # Email addresses, except obvious placeholders / bot addresses.
    ("email address", re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?!example\.(com|org|net)\b)(?!users\.noreply\.github\.com\b)"
        r"(?!anthropic\.com\b)(?!noreply\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # US phone numbers like (555) 123-4567 or 555-123-4567.
    ("phone number", re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}|\b\d{3}-\d{3}-\d{4}\b")),
    # Filled-in secrets in env-style lines (KEY=value) - example files must leave them empty.
    ("secret value", re.compile(r"^[ \t]*[A-Z0-9_]*(PASSWORD|TOKEN|API_KEY|SECRET|KEY)[A-Z0-9_]*[ \t]*=[ \t]*['\"]?[^'\"\s#$<{]", re.M)),
]
ENV_ONLY = {"secret value"}  # only checked in .env-style files (code has variables named SECRET etc.)

ALLOWED_EMAIL = re.compile(r"(you|your\.name|name|user|someone|me)@", re.I)


def main() -> int:
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    problems = []

    # private/ must only contain its placeholder in git.
    tracked_private = [f for f in files if f.startswith("private/") and f != "private/.gitkeep"]
    for f in tracked_private:
        problems.append(f"{f}: file in private/ is tracked (real data must stay local)")

    for path in files:
        if path in SKIP or path.lower().endswith(BINARY):
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        is_env = ".env" in path.rsplit("/", 1)[-1]
        for label, rx in CHECKS:
            if label in ENV_ONLY and not is_env:
                continue
            for m in rx.finditer(text):
                if label == "email address" and ALLOWED_EMAIL.match(m.group(0)):
                    continue
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{path}:{line}: possible {label}: {m.group(0)[:60]!r}")

    if problems:
        print("Possible personal data found (the repo is public - move it to private/ or use a placeholder):")
        print("\n".join(problems))
        return 1
    print(f"privacy check passed ({len(files)} tracked files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
