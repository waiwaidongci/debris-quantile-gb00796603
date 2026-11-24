"""Compatibility shim: team planning lives in ``src.services.domain``."""
from src.services.domain import *  # noqa: F401,F403
import src.services.domain as _domain


def __getattr__(name):
    return getattr(_domain, name)
