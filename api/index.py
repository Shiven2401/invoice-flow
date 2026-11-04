"""Vercel entry point: the same FastAPI app that `python run.py` serves, run as one Python function.

Vercel's disk is read-only apart from /tmp, and /tmp is temporary, so on Vercel the database, uploads and logs live
in /tmp/invoice-flow and are lost whenever Vercel recycles the instance (after an idle spell, and on every deploy).
For data that is kept, run the Dockerfile on a host with a persistent disk (see README, "Going public").
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("RUNTIME_DIR", "/tmp/invoice-flow")   # set before app.config is imported; Vercel's env vars win
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402  (Vercel serves this ASGI app)
