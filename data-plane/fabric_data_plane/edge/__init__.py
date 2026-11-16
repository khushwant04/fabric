"""Fabric edge gateway: one inference URL in front of every stamp (ADR 0021).

Run with ``python -m fabric_data_plane.edge``. It shares the data-plane image and reuses
its token verification; it holds no model hosts and meters nothing.
"""
