"""Application services: accounts, projects, documents, assets and jobs.

Routes stay thin; every rule that matters — who may write, what a write does to
history, how a job moves between states — lives here so the CLI, the API and the
worker all behave the same way.
"""
