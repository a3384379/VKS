"""Load one current header for a ready personal-task batch."""

import json

from sqlalchemy import func, select
from sqlalchemy.orm import defer

from .ai_gateway import ModelPolicy
from .materials import StudioWorkspace
from .models import Analysis, InputEpoch, Job, Project, Release, Selection


def load(session, scope):
    pid, aid, jid = scope
    choices = (
        select(func.json_group_object(Selection.entity_id, Selection.entity_revision_id))
        .where(Selection.analysis_set_id == aid)
        .scalar_subquery()
    )
    row = session.execute(
        select(Project, Analysis, Job, StudioWorkspace, ModelPolicy, InputEpoch, Release, choices)
        .select_from(Project)
        .join(Analysis, (Analysis.id == aid) & (Analysis.project_id == Project.id))
        .join(Job, (Job.id == jid) & (Job.project_id == Project.id))
        .outerjoin(StudioWorkspace, StudioWorkspace.project_id == Project.id)
        .outerjoin(ModelPolicy, ModelPolicy.project_id == Project.id)
        .join(InputEpoch, InputEpoch.id == 1)
        .outerjoin(Release, Release.analysis_set_id == Analysis.id)
        .where(Project.id == pid)
        .options(defer(Job.input_json), defer(Job.result_json), defer(Release.manifest_json))
    ).first()
    if row is None:
        return
    project, analysis, job, workspace, policy, epoch, release, choices = row
    session.info["vks_task_header"] = (project, analysis, job, workspace)
    session.info.setdefault("vks_model_policies", {})[pid] = policy
    session.info.setdefault("vks_analysis_releases", {})[aid] = release
    session.info.setdefault("vks_selection_maps", {})[aid] = json.loads(choices or "{}")
    session.info["vks_source_token"] = epoch.token
