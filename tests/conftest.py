"""Unit tests must never reach a real game: point the link at ports nothing listens on unless a test (or a live
script run by hand) sets its own test slot."""
import os

os.environ.setdefault("FACTORIO_UDP_PORT", "34599")
os.environ.setdefault("FACTORIO_UDP_TIMEOUT", "0.2")
os.environ.setdefault("FACTORIO_RCON_PORT", "27099")
