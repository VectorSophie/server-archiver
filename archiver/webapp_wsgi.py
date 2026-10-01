"""WSGI entry point for a production server (gunicorn etc.) -- `archive
serve` uses create_app(config) directly instead, since it already has
a Config object from argument parsing. gunicorn's module:app syntax
needs a plain module-level `app`, so this loads config.json itself."""
from pathlib import Path

from archiver.config import load_config
from archiver.webapp import create_app

HERE = Path(__file__).parent.parent
config = load_config(HERE / "config.json")
app = create_app(config)
