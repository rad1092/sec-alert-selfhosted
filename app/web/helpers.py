from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.security import template_defaults

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def render_template(request: Request, template_name: str, **context: Any):
    merged_context = template_defaults(request, **context)
    merged_context["request"] = request
    return templates.TemplateResponse(request, template_name, merged_context)
