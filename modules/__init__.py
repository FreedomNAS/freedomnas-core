"""
FreedomNAS pluggable modules — implement FreedomNASModule and register with Orchestrator.
"""

from .storage import StorageModule
from .network import NetworkModule
from .shares import SharesModule
from .users import UsersModule
from .updates import UpdatesModule

__all__ = [
    "StorageModule",
    "NetworkModule",
    "SharesModule",
    "UsersModule",
    "UpdatesModule",
]
