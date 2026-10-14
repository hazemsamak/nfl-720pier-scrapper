"""
State management module.
Provides StateManager as a backward-compatible wrapper over DatabaseManager (SQLite).
"""
import logging
from src.database import DatabaseManager

logger = logging.getLogger(__name__)


class StateManager(DatabaseManager):
    """
    Persistent state manager backed by SQLite.
    Inherits all capabilities from DatabaseManager while preserving backward compatibility.
    """
    pass

