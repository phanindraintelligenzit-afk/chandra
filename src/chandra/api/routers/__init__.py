"""Routers split out of the monolithic ``fastapi_app`` module."""

from src.chandra.api.routers.catalog import router as catalog_router
from src.chandra.api.routers.governance import router as governance_router
from src.chandra.api.routers.jobs import router as jobs_router

__all__ = ["catalog_router", "governance_router", "jobs_router"]
