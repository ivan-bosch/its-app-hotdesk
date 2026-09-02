"""Shared Jinja2 template environment for both the employee app (main.py) and
the admin panel (admin.py) — one instance so globals/filters only need to be
configured once."""

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")
