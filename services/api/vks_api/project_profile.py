"""Project business metadata without changing stable knowledge or access grants."""

from typing import Literal
from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, Project, Member
from .identity import UserIdentity


class ProjectProfile(Base):
    __tablename__ = "project_profile"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), primary_key=True)
    business_domain: Mapped[str] = mapped_column(String(160), default="")
    responsible_user_id: Mapped[str] = mapped_column(ForeignKey("user_identity.id"))
    visibility: Mapped[str] = mapped_column(String(24), default="project_members")
    default_analysis_language: Mapped[str] = mapped_column(String(24), default="java")


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_domain: str = Field(default="", max_length=160)
    responsible_user_id: str
    visibility: Literal["project_members"] = "project_members"
    default_analysis_language: Literal["java", "mixed", "source_anchors"] = "java"


class ProfileService:
    def __init__(self, service):
        self.service = service

    def _dto(self, s, project):
        p = s.get(ProjectProfile, project.id)
        identity = s.get(UserIdentity, p.responsible_user_id if p else project.owner_id)
        return {
            "project_id": project.id,
            "row_version": project.row_version,
            "business_domain": p.business_domain if p else "",
            "responsible_user_id": p.responsible_user_id if p else project.owner_id,
            "responsible_name": identity.display_name if identity else "项目管理员",
            "visibility": "project_members",
            "default_analysis_language": p.default_analysis_language if p else "java",
        }

    def get(self, actor, pid):
        with self.service.db() as s:
            p = self.service.require(s, actor, pid)
            return self._dto(s, p)

    def update(self, actor, pid, payload, version):
        from .service import DomainError

        with self.service.db() as s:
            p = self.service.require(s, actor, pid, "project:manage")
            self.service._version(p.row_version, version)
            identity = s.get(UserIdentity, payload["responsible_user_id"])
            member = s.get(Member, (pid, payload["responsible_user_id"]))
            if not identity or identity.status != "active" or not member or member.status != "active":
                raise DomainError("VKS_INVALID_INPUT", "负责人必须是项目中的有效成员")
            profile = s.get(ProjectProfile, pid)
            if profile:
                for k, v in payload.items():
                    setattr(profile, k, v)
            else:
                s.add(ProjectProfile(project_id=pid, **payload))
            p.row_version += 1
            s.flush()
            self.service.audit(s, actor, pid, "project.profile.update", pid)
            return self._dto(s, p)


def install_project_profile(app, service, actor, envelope, API="/api/v1"):
    profiles = ProfileService(service)

    @app.get(API + "/projects/{pid}/profile")
    def get(pid: str, request: Request, a=Depends(actor)):
        return envelope(request, profiles.get(a, pid))

    @app.put(API + "/projects/{pid}/profile")
    def update(pid: str, payload: ProfileInput, request: Request, a=Depends(actor), if_match: str = Header(...)):
        return envelope(request, profiles.update(a, pid, payload.model_dump(), if_match))
