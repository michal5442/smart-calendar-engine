"""
Comp Calendar Exercise - Python Implementation

Global Smart Calendar Engine
סינוג לוח השנה הגלובלי החכם
"""

from .app import find_available_slots, find_available_slots_details
from .repository import SQLiteCalendarRepository

__version__ = "2.0.0"
__all__ = ["find_available_slots", "find_available_slots_details", "SQLiteCalendarRepository"]
