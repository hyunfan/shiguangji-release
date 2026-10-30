"""Local HTTP interface: request checks, responses, file transfer and route tables.

Route handlers receive the application module (``app.py``) as ``env`` and look up its
singletons (TASKS, CLOUD, storage_service) and collaborators at call time, so the root
entry script stays the single composition point and its attributes remain patchable.
"""
