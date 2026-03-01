import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/mcp-memory/data"))
PORT = int(os.environ.get("PORT", "8766"))
