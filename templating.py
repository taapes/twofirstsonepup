"""Shared Jinja2 templates instance (used by main.py and ui.py)."""

import os

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
# APP_ENV (e.g. "test" on a Neon test branch) drives a banner in base.html so a
# test environment is never mistaken for production.
templates.env.globals["app_env"] = os.getenv("APP_ENV", "prod")
