"""uzp_dash — контур-агностичный генератор дэшбордов УЗП.

Публичный API для управляющего ноутбука:
    from uzp_dash import generate_dashboard, list_dashboards, llm_check
"""
from .llm import configure as configure_llm, llm_check  # noqa: F401
from .registry import Context, dashboard, generate_dashboard, list_dashboards  # noqa: F401

__all__ = ["generate_dashboard", "list_dashboards", "dashboard", "Context",
           "llm_check", "configure_llm"]
