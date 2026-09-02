"""li-search — a standalone LinkedIn people-search tool.

Deliberately separate from project/: it shares no code, no state, no venv and no
config with the fire chain. It sources PEOPLE from licensed/indexed people-data
APIs. It never scrapes linkedin.com and never drives an authenticated LinkedIn
session. See compliance.py for the enforced line.
"""
__version__ = "1.0.0"
