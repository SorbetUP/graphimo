"""Persistent local Graphimo application service."""

__all__ = ["GraphimoService", "ProjectSession", "ProjectSessionManager"]


def __getattr__(name: str):
    if name == "GraphimoService":
        from .server import GraphimoService

        return GraphimoService
    if name in {"ProjectSession", "ProjectSessionManager"}:
        from .sessions import ProjectSession, ProjectSessionManager

        return {"ProjectSession": ProjectSession, "ProjectSessionManager": ProjectSessionManager}[
            name
        ]
    raise AttributeError(name)
