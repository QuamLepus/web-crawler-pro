"""Minimal typed Python client for Web Intelligence OS REST API."""
from .client import Client, WebIntelligenceError, WorkflowStep

__all__ = ["Client", "WebIntelligenceError", "WorkflowStep"]
