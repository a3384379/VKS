"""Release-scoped prototype references and explicitly captured preview derivatives."""

from base64 import b64encode
from dataclasses import dataclass
from io import BytesIO
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, Header, Query, Request
from PIL import Image
from pydantic import Field, model_validator
from sqlalchemy import select

from packages.analyzers.vks_ingest import BundleError, safe_path

from .interactive import InteractiveKnowledgeService
from .knowledge_locator import (
    PrototypeBindingLocator,
    PrototypePageLocator,
    binding_reference,
    page_reference,
    saved_scope,
)
from .models import Artifact, Job, Revision, now, uid
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input
from .prototype_read import missing, page_state
from .service import Actor, DomainError, canonical, sha

MAX_IMAGE_BYTES = 160 * 1024
MAX_PAGE_OFFSET = 100_000
VIEWPORT = {"width": 1280, "height": 800}
GENERATOR = "vks-isolated-chromium-v2"
PREVIEW_PREFIX = "studio-previews/"
PageRef = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PreviewLocator = Annotated[PrototypePageLocator | PrototypeBindingLocator, Field(discriminator="kind")]


class PreviewRead(Input):
    release_id: UUID
    locator: PreviewLocator
    max_width: int = Field(default=960, ge=160, le=1280, strict=True)


class PreviewCapture(Input):
    analysis_set_id: UUID | None = None
    release_id: UUID | None = None
    package_artifact_id: UUID
    page_ref: PageRef
    binding_id: UUID | None = None

    @model_validator(mode="after")
    def exact_baseline(self):
        if (self.analysis_set_id is None) == (self.release_id is None):
            raise ValueError("请选择一个准确的草稿或保存版本")
        return self


class PreviewScope(Input):
    project_id: UUID
    release_id: UUID
    manifest_sha256: str
    saved_at: str
    release_label: str


class PreviewImage(Input):
    sha256: str
    source_image_sha256: str
    mime_type: Literal["image/png"] = "image/png"
    width: int
    height: int
    generated_at: str
    generator: str
    source: Literal["registered_raster", "isolated_capture"]
    viewport: dict
    device_pixel_ratio: float


class PreviewResult(Input):
    status: Literal["available", "preview_unavailable"]
    scope: PreviewScope
    locator: PreviewLocator
    state: Literal["default", "binding_unverified", "verified_binding"] = "default"
    page_label: str | None = None
    studio_url: str | None = None
    reason: Literal["page_not_registered", "no_exact_preview", "image_budget_exceeded"] | None = None
    message: str | None = None
    image: PreviewImage | None = None


class PreviewReadEnvelope(PrototypeEnvelope[PreviewResult]):
    # This REST transport field is converted to a single MCP ImageContent.
    image_base64: str | None = Field(default=None, max_length=((MAX_IMAGE_BYTES + 2) // 3) * 4)


class PublicPreviewPage(Input):
    page_ref: PageRef
    label: str
    format: str
    package_artifact_id: UUID
    locator: PrototypePageLocator | None = None
    studio_url: str | None = None


class StudioPreviewPage(PublicPreviewPage):
    route: str
    frame_path: list[str]


class PreviewPageList(Input):
    items: list[StudioPreviewPage]
    total: int
    next_offset: int | None


@dataclass(frozen=True)
class PreviewTarget:
    package: dict
    page: dict | None
    binding: dict | None
    raster: dict | None = None

    def source(self, pid):
        target = self.binding["target"] if self.binding and self.page else None
        return {
            "project_id": pid,
            "package_artifact_id": self.package["id"],
            "package_key": self.package.get("package_key"),
            "package_revision_id": self.package.get("revision_id"),
            "package_sha256": self.package["sha256"],
            "page_ref": self.page["page_ref"],
            "state": "verified_binding" if target else "default",
            **({"target_sha256": sha(canonical(target))} if target else {}),
        }


def _page_ref(package, *identity):
    return sha(
        canonical(
            [
                package["id"],
                package.get("package_key"),
                package.get("revision_id"),
                package["sha256"],
                *identity,
            ]
        )
    )


def _registered_route(package, route, frames):
    if package["format"] == "html":
        return route == "/" and frames == []
    if package["format"] not in ("charge-prototype", "static-site") or frames != ["actualPage"]:
        return False
    if (
        not isinstance(route, str)
        or not route.startswith("/")
        or len(route) > 1000
        or any(char in route for char in ":?#%")
    ):
        return False
    try:
        path = safe_path(route[1:])
    except BundleError:
        return False
    return path.lower().endswith((".html", ".htm"))


def _scope_args(form):
    return (
        str(form.analysis_set_id) if form.analysis_set_id else None,
        str(form.release_id) if form.release_id else None,
    )


def _preview_namespace(pid):
    # Separate derivative artifacts from imported source snapshots and exports.
    return str(uuid5(NAMESPACE_URL, f"urn:vks:studio-preview:{pid}"))


def _preview_prefix(source):
    return f"{PREVIEW_PREFIX}{GENERATOR}/{sha(canonical(source))}/"


def require_idle_preview(s, pid):
    active = s.scalar(
        select(Job.id)
        .where(
            Job.project_id == pid,
            Job.kind == "prototype_preview",
            Job.state.in_(("queued", "running", "cancelling", "retry_wait")),
        )
        .limit(1)
    )
    if active:
        raise DomainError("VKS_STATE_CONFLICT", "已有预览正在生成，请等待完成", 409)


class PrototypePreviews:
    def __init__(self, service):
        self.service = service
        self.links = InteractiveKnowledgeService(service)
        self.reader = self.links.reader

    def _package_visible(self, s, actor, pid, package):
        from .reading import artifact_policy

        artifact = s.get(Artifact, package["id"])
        if not artifact or artifact.project_id != pid or artifact.sha256 != package["sha256"]:
            return False
        policy = artifact_policy(s, artifact)
        return not policy.get("offline_archive_invalid") and self.reader.reading.policy_visible(
            artifact.kind,
            artifact.sensitivity,
            policy,
            *self.reader.reading.permissions(s, actor, pid),
        )

    def _html_pages(self, s, actor, pid, choices, package_id=None, *, package_key=None):
        from .bulk_association import inventory

        packages = sorted(
            self.links._packages(s, actor, pid, choices, kind="prototype", package_id=package_id),
            key=lambda row: row["id"],
        )
        for package in packages:
            if package_key and package_key not in (package["id"], package.get("package_key")):
                continue
            if not self._package_visible(s, actor, pid, package):
                continue
            root = s.get(Revision, package["revision_id"]) if package.get("revision_id") else None
            try:
                targets = inventory(self.service, s, package, root)["targets"]
            except DomainError:
                # A malformed old package is not a registered page.
                continue
            reported = {}
            duplicates = set()
            for target in targets:
                if target["selector"] != "body":
                    continue
                route, frames = target["route"], target["frame_path"]
                if not _registered_route(package, route, frames):
                    continue
                key = (route, tuple(frames))
                if key in reported:
                    duplicates.add(key)
                reported[key] = {
                    "page_ref": _page_ref(package, "html", route, frames),
                    "label": target["label"],
                    "route": route,
                    "frame_path": frames,
                    "format": package["format"],
                    "package_artifact_id": package["id"],
                }
            for key, page in reported.items():
                if key not in duplicates:
                    yield PreviewTarget(package, page, None)

    def _raster_pages(self, s, actor, pid, choices, package_id=None):
        from .reading import artifact_policy, prototype_surface

        rows = self.reader._entities(s, actor, pid, choices, {"page"})
        for entity, revision in sorted(rows, key=lambda row: row[1].id):
            if package_id and revision.artifact_id != package_id:
                continue
            if not revision.metadata_json.get("prototype_manifest"):
                continue
            state = page_state(revision)
            image = prototype_surface(s, pid, revision.artifact_id, state)
            origin = s.get(Artifact, revision.artifact_id)
            if (
                not image
                or not origin
                or not self.reader.reading.policy_visible(
                    image.kind,
                    image.sensitivity,
                    artifact_policy(s, image),
                    *self.reader.reading.permissions(s, actor, pid),
                )
            ):
                continue
            package = {"id": origin.id, "sha256": origin.sha256}
            page = {
                "page_ref": _page_ref(package, "raster", revision.id, state["stateKey"]),
                "label": revision.name,
                "format": "raster",
                "package_artifact_id": origin.id,
            }
            yield PreviewTarget(
                package,
                page,
                None,
                {
                    "page_key": entity.stable_key,
                    "page_revision_id": revision.id,
                    "state_key": state["stateKey"],
                    "image": image.sha256,
                    "viewport": state["viewport"],
                    "created_at": image.created_at,
                },
            )

    def _public_page(self, pid, release, resolved):
        page = {k: v for k, v in resolved.page.items() if k not in ("route", "frame_path")}
        if release:
            page.update(
                page_reference(
                    self.service.studio_base_url,
                    pid,
                    release.id,
                    resolved.package,
                    page["page_ref"],
                    raster=resolved.raster is not None,
                )
            )
        return page

    def project_pages(self, actor, pid, release_id, *, query="", offset=0, limit=20):
        from itertools import chain, islice

        with self.reader.reading.db() as s:
            _, release, choices, _ = self.reader.scope(s, actor, pid, None, release_id)
            needle = query.strip().casefold()
            matched = (
                row
                for row in chain(self._html_pages(s, actor, pid, choices), self._raster_pages(s, actor, pid, choices))
                if needle in row.page["label"].casefold()
            )
            pages = list(islice(matched, offset, offset + limit + 1))
            next_offset = offset + limit if len(pages) > limit and offset + limit <= MAX_PAGE_OFFSET else None
            return [self._public_page(pid, release, page) for page in pages[:limit]], next_offset

    def studio_pages(self, actor, pid, aid, rid, package, page_ref, route, frame_path, offset, limit):
        with self.reader.reading.db() as s:
            _, release, choices, _ = self.reader.scope(s, actor, pid, aid, rid)
            found = [
                row
                for row in self._html_pages(s, actor, pid, choices, package_key=package)
                if (not page_ref or row.page["page_ref"] == page_ref)
                and (route is None or (row.page["route"] == route and row.page["frame_path"] == frame_path))
            ]
            if (page_ref or route is not None) and len(found) != 1:
                raise missing()
            batch = found[offset : offset + limit]
            return {
                "items": [
                    {
                        **self._public_page(pid, release, row),
                        "route": row.page["route"],
                        "frame_path": row.page["frame_path"],
                    }
                    for row in batch
                ],
                "total": len(found),
                "next_offset": offset + len(batch) if offset + len(batch) < len(found) else None,
            }

    def _binding(self, s, actor, pid, analysis, release, choices, binding_id, package_id):
        relations = self.links._binding_relations(s, analysis, release)
        edge = next(
            (row for row in relations if row["id"] == binding_id and row["relation_type"] == "has_business_point"), None
        )
        if not edge or edge["excluded"]:
            raise missing()
        # Use the existing projection and its permission and citation fences.
        rows = self.links._project_bindings(
            s,
            actor,
            pid,
            choices,
            [
                edge,
                *(
                    row
                    for row in relations
                    if row["relation_type"] == "supported_by_section"
                    and row["from_revision_id"] == edge["to_revision_id"]
                ),
            ],
        )
        binding = next((row for row in rows if row["id"] == binding_id and row["package_id"] == package_id), None)
        if not binding:
            raise missing()
        return binding

    def _resolve(self, s, actor, pid, aid, rid, package_id, page_ref=None, binding_id=None):
        analysis, release, choices, _ = self.reader.scope(s, actor, pid, aid, rid)
        if binding_id:
            binding = self._binding(s, actor, pid, analysis, release, choices, binding_id, package_id)
            packages = self.links._packages(s, actor, pid, choices, kind="prototype", package_id=package_id)
            package = next(iter(packages), None)
            if not package or not self._package_visible(s, actor, pid, package):
                raise missing()
            target = binding["target"]
            candidates = [
                row
                for row in self._html_pages(s, actor, pid, choices, package_id)
                if (row.page["route"], row.page["frame_path"]) == (target["route"], target["frame_path"])
                and (page_ref is None or row.page["page_ref"] == page_ref)
            ]
            return release, PreviewTarget(package, candidates[0].page if len(candidates) == 1 else None, binding)
        for rows in (
            self._html_pages(s, actor, pid, choices, package_id),
            self._raster_pages(s, actor, pid, choices, package_id),
        ):
            for row in rows:
                if row.page["page_ref"] == page_ref:
                    return release, row
        raise missing()

    def enqueue(self, actor, pid, form, key):
        aid, rid = _scope_args(form)
        with self.service.db() as s:
            self.service.require(s, actor, pid, "capture:run")
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "请在 Studio 中主动更新预览", 403)
            from .models import Project

            s.scalar(select(Project).where(Project.id == pid).with_for_update())
            _, target = self._resolve(
                s,
                actor,
                pid,
                aid,
                rid,
                str(form.package_artifact_id),
                form.page_ref,
                str(form.binding_id) if form.binding_id else None,
            )
            if not target.page or target.raster:
                raise DomainError("VKS_INVALID_INPUT", "此目标没有可采集的登记 HTML 页面")
            payload = {**form.model_dump(mode="json"), "source": target.source(pid)}

            def submit():
                require_idle_preview(s, pid)
                return self.service.job_dto(self.service._new_job(s, actor, pid, "prototype_preview", payload))

            return self.service.idem(s, actor, pid, "prototype.preview.capture", key, payload, submit)

    def read(self, actor, pid, request):
        locator = request.locator
        if str(locator.project_id) != pid or locator.release_id != request.release_id:
            raise DomainError("VKS_INVALID_SCOPE", "预览定位器与项目或保存版本不一致")
        rid = str(request.release_id)
        package_id = str(locator.package_artifact_id)
        binding_id = str(locator.binding_id) if isinstance(locator, PrototypeBindingLocator) else None
        asset_facts = None
        with self.reader.reading.db() as s:
            release, target = self._resolve(
                s,
                actor,
                pid,
                None,
                rid,
                package_id,
                locator.page_ref if isinstance(locator, PrototypePageLocator) else None,
                binding_id,
            )
            base = self.service.studio_base_url
            ref = (
                binding_reference(base, pid, rid, target.package, target.binding)
                if binding_id
                else page_reference(base, pid, rid, target.package, target.page["page_ref"], raster=bool(target.raster))
            )
            if ref["locator"] != locator.model_dump(mode="json", exclude_none=True):
                raise missing()
            result = {
                **ref,
                "scope": saved_scope(self.service.release_dto(release)),
                "page_label": target.page["label"] if target.page else None,
                "state": "binding_unverified" if binding_id else "default",
            }
            if not target.page:
                return _unavailable(result, "page_not_registered")
            if not target.raster:
                source = target.source(pid)
                asset = s.scalar(
                    select(Artifact)
                    .where(
                        Artifact.project_id == pid,
                        Artifact.snapshot_id == _preview_namespace(pid),
                        Artifact.relative_path.startswith(_preview_prefix(source)),
                        Artifact.kind == "screenshot",
                        Artifact.media_type == "image/png",
                    )
                    .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                    .limit(1)
                )
                if not asset or asset.metadata_json != {
                    "source": source, "generator": GENERATOR, "viewport": VIEWPORT, "dpr": 1
                }:
                    return _unavailable(result, "no_exact_preview")
                from .reading import artifact_policy

                if not self.reader.reading.policy_visible(
                    asset.kind, asset.sensitivity, artifact_policy(s, asset),
                    *self.reader.reading.permissions(s, actor, pid),
                ):
                    return _unavailable(result, "no_exact_preview")
                raw = _checked_object(self.service, asset, 6 * 1024 * 1024)
                asset_facts = {
                    "source_image_sha256": asset.sha256,
                    "generated_at": asset.created_at,
                    "generator": asset.metadata_json["generator"],
                    "source": "isolated_capture",
                    "viewport": asset.metadata_json["viewport"],
                    "device_pixel_ratio": asset.metadata_json["dpr"],
                }
        if target.raster:
            from .prototype import prototype_image

            spec = target.raster
            raw = prototype_image(
                self.service,
                actor,
                pid,
                spec["page_key"],
                spec["state_key"],
                spec["page_revision_id"],
                None,
                rid,
            )
            asset_facts = {
                "source_image_sha256": spec["image"],
                "generated_at": spec["created_at"],
                "generator": "registered-prototype-manifest",
                "source": "registered_raster",
                "viewport": {key: spec["viewport"][key] for key in ("width", "height")},
                "device_pixel_ratio": spec["viewport"]["devicePixelRatio"],
            }
        rendered = _thumbnail(raw, request.max_width)
        if not rendered:
            return _unavailable(result, "image_budget_exceeded")
        image, width, height = rendered
        return {
            **result,
            "status": "available",
            "state": "verified_binding" if binding_id else "default",
            "image": {**asset_facts, "sha256": sha(image), "width": width, "height": height, "mime_type": "image/png"},
        }, image


def _unavailable(result, reason):
    return {
        **result,
        "status": "preview_unavailable",
        "reason": reason,
        "message": "当前目标没有可提供的准确预览。可通过可用的 Studio 链接查看原型。",
    }, None


def _checked_object(service, artifact, limit):
    # Object keys are content hashes. Bound the read independently of recorded size.
    if artifact.object_key != artifact.sha256 or not 0 < artifact.size_bytes <= limit:
        raise DomainError("VKS_INTEGRITY_ERROR", "预览来源制品校验失败", 409)
    path = service.data_dir / "objects" / artifact.object_key
    try:
        with path.open("rb") as stream:
            raw = stream.read(artifact.size_bytes + 1)
    except OSError:
        raise missing() from None
    if len(raw) != artifact.size_bytes or sha(raw) != artifact.sha256:
        raise DomainError("VKS_INTEGRITY_ERROR", "预览来源制品校验失败", 409)
    return raw


def _thumbnail(raw, max_width):
    try:
        with Image.open(BytesIO(raw), formats=["PNG"]) as source:
            if source.width * source.height > 24_000_000 or getattr(source, "is_animated", False):
                raise ValueError("invalid preview PNG")
            image = source.convert("RGB")
        image.thumbnail((max_width, 960), Image.Resampling.LANCZOS)
        while True:
            buf = BytesIO()
            image.save(buf, format="PNG", optimize=True)
            if buf.tell() <= MAX_IMAGE_BYTES:
                return buf.getvalue(), image.width, image.height
            if max(image.size) <= 160:
                return None
            image.thumbnail(
                (max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
                Image.Resampling.LANCZOS,
            )
    except (OSError, ValueError, Image.DecompressionBombError):
        raise DomainError("VKS_INTEGRITY_ERROR", "预览不是可验证的单帧 PNG", 409) from None


def _job_target(service, s, job, actor):
    payload = job.input_json
    requested = PreviewCapture.model_validate({key: payload[key] for key in PreviewCapture.model_fields})
    aid, rid = _scope_args(requested)
    previews = PrototypePreviews(service)
    _, target = previews._resolve(
        s,
        actor,
        job.project_id,
        aid,
        rid,
        str(requested.package_artifact_id),
        requested.page_ref,
        str(requested.binding_id) if requested.binding_id else None,
    )
    if not target.page or target.raster or target.source(job.project_id) != payload["source"]:
        raise DomainError("VKS_VERSION_CONFLICT", "预览来源已变化，请从当前原型重新发起", 409)
    return target


def prepare_preview(service, jid, actor, cancelled):
    from services.browser_runner.preview import render_preview
    from services.browser_runner.runner import CaptureError

    from .reading import ReadingService

    with ReadingService(service).db() as s:
        job = s.get(Job, jid)
        service.require(s, actor, job.project_id, "capture:run")
        target = _job_target(service, s, job, actor)
        raw = _checked_object(service, s.get(Artifact, target.package["id"]), 80 * 1024 * 1024)
    try:
        captured = render_preview(raw, target.page, target.binding, VIEWPORT, cancelled)
    except CaptureError as exc:
        raise DomainError(exc.code, exc.message, 409) from None
    cancelled()
    try:
        with Image.open(BytesIO(captured), formats=["PNG"]) as image:
            if image.size != (VIEWPORT["width"], VIEWPORT["height"]) or getattr(image, "is_animated", False):
                raise ValueError("invalid capture dimensions")
            image.verify()
        if len(captured) > 6 * 1024 * 1024:
            raise ValueError("invalid capture size")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise DomainError("VKS_INTEGRITY_ERROR", "浏览器没有返回有效预览图", 409) from None
    return captured


def finish_preview(service, s, job, actor, raw):
    target = _job_target(service, s, job, actor)
    source = target.source(job.project_id)
    original = s.get(Artifact, target.package["id"])
    digest = sha(raw)
    artifact = Artifact(
        id=uid(),
        project_id=job.project_id,
        snapshot_id=_preview_namespace(job.project_id),
        relative_path=_preview_prefix(source) + job.id + ".png",
        sha256=digest,
        size_bytes=len(raw),
        kind="screenshot",
        media_type="image/png",
        sensitivity=original.sensitivity,
        object_key=service._put_object(raw),
        metadata_json={"source": source, "generator": GENERATOR, "viewport": VIEWPORT, "dpr": 1},
    )
    s.add(artifact)
    service.audit(s, actor, job.project_id, "prototype.preview.capture", job.id)
    return {"page_ref": target.page["page_ref"], "state": source["state"], "sha256": digest, "captured_at": now()}


def install_prototype_previews(app, service, actor, envelope):
    import json

    previews = PrototypePreviews(service)
    prefix = "/api/v3/projects/{pid}/interactive/previews"
    ActorDep = Annotated[Actor, Depends(actor)]

    @app.get(prefix + "/pages", response_model=PrototypeEnvelope[PreviewPageList], response_model_exclude_none=True)
    def pages(
        pid: str,
        request: Request,
        a: ActorDep,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        package: Annotated[str | None, Query(max_length=96)] = None,
        page_ref: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
        route: Annotated[str | None, Query(max_length=1000)] = None,
        frame_path: Annotated[str | None, Query(max_length=2000)] = None,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        frames = None
        if frame_path is not None:
            try:
                frames = json.loads(frame_path)
                if (
                    not isinstance(frames, list)
                    or len(frames) > 8
                    or any(not isinstance(part, str) or not 0 < len(part) <= 160 for part in frames)
                ):
                    raise ValueError
            except (TypeError, ValueError, RecursionError):
                raise DomainError("VKS_INVALID_INPUT", "页面 frame 标识无效") from None
        if (route is None) != (frames is None) or (page_ref and route is not None):
            raise DomainError("VKS_INVALID_INPUT", "请选择一种页面定位方式")
        return envelope(
            request,
            previews.studio_pages(
                a,
                pid,
                analysis_set_id,
                release_id,
                package,
                page_ref,
                route,
                frames,
                offset,
                limit,
            ),
        )

    @app.post(prefix + "/read", response_model=PreviewReadEnvelope, response_model_exclude_none=True)
    def read(pid: str, form: PreviewRead, request: Request, a: ActorDep):
        result, image = previews.read(a, pid, form)
        return {**envelope(request, result), **({"image_base64": b64encode(image).decode("ascii")} if image else {})}

    @app.post(prefix + "/capture", status_code=202, response_model=PrototypeEnvelope[dict])
    def capture(pid: str, form: PreviewCapture, request: Request, a: ActorDep, idempotency_key: str = Header(...)):
        return envelope(request, previews.enqueue(a, pid, form, idempotency_key))
