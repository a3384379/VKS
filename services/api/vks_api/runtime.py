"""Public capability flags are implementation/configuration facts, not acceptance claims."""

import os
from sqlalchemy import select


def status(service, session=None, project_id=None):
    if session is None:
        with service.db() as s:
            return status(service, s, project_id)
    from .oauth import OAuthIssuer
    from .ai_gateway import ModelPolicy, endpoint_policy

    policy = session.scalar(select(ModelPolicy).where(ModelPolicy.project_id == project_id)) if project_id else None
    model_enabled = False
    if policy and policy.settings.get("enabled"):
        try:
            endpoint_policy(policy.settings)
            model_enabled = not policy.settings.get("secret_ref") or bool(os.getenv("VKS_MODEL_API_KEY"))
        except Exception:
            model_enabled = False
    return {
        "runner_enabled": os.getenv("VKS_RUNNER_ENABLED") == "1",
        "model_enabled": bool(model_enabled),
        "oauth_enabled": bool(session.scalar(select(OAuthIssuer.id).where(OAuthIssuer.active == True).limit(1))),
        "task_mode": os.getenv("VKS_TASK_MODE", "local"),
        "source_collection": "client-readonly",
    }
