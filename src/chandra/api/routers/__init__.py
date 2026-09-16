"""Routers split out of the monolithic ``fastapi_app`` module."""

from src.chandra.api.routers.governance import router as governance_router

__all__ = ["governance_router"]
