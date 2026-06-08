"""Shared Jinja2 templates instance (used by main.py and ui.py)."""

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
