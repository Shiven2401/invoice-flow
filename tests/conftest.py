"""Runs before any test module. run_all() wipes the database it finds, and app.config fixes
RUNTIME_DIR on first import, so the temp directory has to be chosen before anything imports app."""
import os
import tempfile

os.environ["RUNTIME_DIR"] = tempfile.mkdtemp(prefix="invflow_")
os.environ["STAGE_DELAY"] = "0"
os.environ["ANTHROPIC_API_KEY"] = ""  # a key in .env must not make the tests call Claude
os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"] = ""  # nor Gemini
