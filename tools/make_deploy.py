"""Build a clean, self-contained copy of Invoice Flow for GitHub and Render: only what is needed to run and deploy it.

    python tools/make_deploy.py ../../invoice-flow-deploy

Copies from an explicit list (nothing by wildcard from the project root), so local invoices (runtime/), secrets (.env),
the virtual environment, caches, tests and tools can't come along by accident. Refuses to write into a folder that
already exists: delete or rename the old copy first.
"""
import re
import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
TEST_ONLY = "# --- for the tests and tools only"   # requirements.txt lines from here on stay out of the copy


def build(out):
    out = Path(out).resolve()
    if out.exists():
        sys.exit(f"{out} already exists - delete or rename it first")
    files = ["run.py", "Dockerfile", "render.yaml", ".env.example", "README.md",
             "data/vendors.csv", "data/purchase_orders.csv",
             "static/index.html", "static/app.css", "static/app.js", "static/theme.js"]
    files += [f"app/{p.name}" for p in sorted((SRC / "app").glob("*.py"))]
    files += [f"static/fonts/{name}" for name in sorted(set(re.findall(r"url\(fonts/([^)]+)\)", (SRC / "static/app.css").read_text(encoding="utf-8"))))]
    files += [f"samples/synthetic/{p.name}" for p in sorted((SRC / "samples/synthetic").iterdir()) if p.suffix in (".pdf", ".json")]
    for rel in files:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC / rel, out / rel)

    reqs = (SRC / "requirements.txt").read_text(encoding="utf-8")
    (out / "requirements.txt").write_text(reqs.split(TEST_ONLY)[0], encoding="utf-8")
    (out / ".gitignore").write_text("# secrets and local data: never commit these\n.env\nruntime/\n"
                                    "# environments and caches\n.venv/\nvenv/\n__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    (out / ".dockerignore").write_text(".env\n.venv\nvenv\nruntime\n__pycache__\n*.pyc\n.git\nREADME.md\n", encoding="utf-8")
    _adjust_readme(out / "README.md")

    copied = sorted(p for p in out.rglob("*") if p.is_file())
    print(f"wrote {out}: {len(copied)} files, {sum(p.stat().st_size for p in copied) / 1024 / 1024:.1f} MB")


def _adjust_readme(path):
    """The copy has no tests, tools or demo file, and no secrets: say what it is, drop what isn't there."""
    s = path.read_text(encoding="utf-8")
    s = s.replace("## Run it\n", "## This folder\n\nThe deployable copy of Invoice Flow: the app, its settings, the sample data and nothing else (no tests, "
                  "tools, local invoices or secrets). It is complete: `pip install -r requirements.txt` and `python run.py` run it, and "
                  "`Dockerfile` / `render.yaml` deploy it. To use a Gemini or Claude key locally, copy `.env.example` to `.env` and fill it in; "
                  "`.env` is git-ignored, so it isn't uploaded with git or GitHub Desktop.\n\n## Run it\n", 1)
    s = re.sub(r"Tests: `python -m pytest.*?\n\n", "", s, flags=re.S)
    s = re.sub(r"## Shareable demo\n\n.*?\n\n(?=## )", "", s, flags=re.S)
    s = s.replace("push with git or GitHub Desktop, not by dragging the folder into GitHub's web page, which ignores `.gitignore` and would publish `.env`.",
                  "This folder holds no secrets, so it can also go up through GitHub's web page (Add file > Upload files; it's under that page's "
                  "100-file limit). If you later add a `.env` or run the app here (which creates `runtime/`), push with git or GitHub Desktop instead: "
                  "the web page ignores `.gitignore` and would publish them.")
    s = s.replace("(`data/`, `tools/make_samples.py`)", "(`data/`, `samples/`)")
    path.write_text(s, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build(sys.argv[1])
