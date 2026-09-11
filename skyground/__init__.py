"""Skyground Video Studio.

`skyground.core` and `skyground.config` only use the Python standard library so
that the editorial CLI (`studio.py validate`, `sync`, `build-source`, `render`)
keeps working on a machine without installed dependencies. Everything under
`skyground.db`, `skyground.api`, `skyground.services` and `skyground.worker`
belongs to the cloud application and requires `requirements.txt`.
"""

__version__ = "0.2.0"
