"""Serve the haconnect TUI as a browser app (for an HA sidebar panel / iframe).

Run:  haconnect-serve        # then open http://<host>:8770
  or: python serve.py
The Textual UI renders in the browser; the app still talks to HA + HomeKit proxy
exactly as the terminal version does.
"""
import sys
import os

from textual_serve.server import Server

HOST = os.environ.get("HACONNECT_HOST", "0.0.0.0")
PORT = int(os.environ.get("HACONNECT_PORT", "8770"))

server = Server(
    command=f"{sys.executable} -m reconcile",
    host=HOST,
    port=PORT,
    title="HAConnect",
)

if __name__ == "__main__":
    server.serve()
