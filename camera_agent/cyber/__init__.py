"""Defensive, allowlist-scoped cyber assessment pipeline.

The package is intentionally limited to discovery, service identification,
vulnerability matching, non-destructive validation and reporting.  It does
not contain exploit code, credential attacks or arbitrary command execution.
"""

from .config import CyberConfig
from .models import CameraEvent, CyberEvent, PipelineReport
from .scope_guard import OutOfScopeTargetError, ScopeGuard

__all__ = [
    "CameraEvent",
    "CyberConfig",
    "CyberEvent",
    "OutOfScopeTargetError",
    "PipelineReport",
    "ScopeGuard",
]
