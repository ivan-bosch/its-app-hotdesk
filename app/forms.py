"""Parsing helpers for form/query input shared by main.py and admin.py."""

from datetime import date

from fastapi import HTTPException


def parse_optional_int(value: str) -> int | None:
    """Form fields arrive as strings; "" (or non-numeric) means "not set"."""
    return int(value) if value.isdigit() else None


def parse_day(value: str) -> date:
    """Parse an ISO date from user input, turning garbage into a 400 instead
    of an unhandled ValueError 500."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {value!r}") from None
