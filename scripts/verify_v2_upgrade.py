"""Opt-in old-binary -> V2 -> consistent backup rollback on empty private PG DBs."""

import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from vks_api.service import Service
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from scripts.backup_restore import backup, restore

BASE = "b8926b0d2665034f24d85a534ec258d544398990"
OLD = """
import json,os
from pathlib import Path
from vks_api.service import Service,DomainError
from vks_api.documents import DocumentService
mode=os.environ['VKS_UPGRADE_PHASE']
service=Service(os.environ['VKS_UPGRADE_STORE'],os.environ['VKS_UPGRADE_URL'])
_,owner=service.login(service.bootstrap_token)
if mode=='seed':
 p=service.create_project(owner,{'key':'charge-demo','name':'Synthetic upgrade'},'project')
 job=service.demo_import(owner,p['id'],'import');service.process_job(job['id'])
 w=service.workspace(owner,p['id'])
 for row in w['relations']:
  service.review_relation(owner,row['id'],{'decision':'confirm','reason':'Synthetic','evidence_ids':row['evidence_ids']},row['row_version'])
 candidate=service.request_release(owner,p['id'],{'analysis_set_id':w['selected_analysis_set']['id']},'precheck')
 ticket=service.confirm_release(owner,candidate['candidate_id'])
 release=service.publish(owner,candidate['candidate_id'],{k:ticket[k] for k in ('human_confirmation_ref','expected_manifest_sha256')}|{'change_note':'V1 frozen fixture'},'publish')
 token=service.create_token(owner,p['id'],{'name':'old-private-token','scopes':['knowledge:read']})['token']
 meta={'pid':p['id'],'aid':w['selected_analysis_set']['id'],'release_id':release['id'],'manifest_sha256':release['manifest_sha256'],'document':w['documents'][0],'old_token':token}
 Path(os.environ['VKS_UPGRADE_META']).write_text(json.dumps(meta))
else:
 meta=json.loads(Path(os.environ['VKS_UPGRADE_META']).read_text())
 w=service.workspace(owner,meta['pid'],release_id=meta['release_id'])
 assert w['selected_release']['manifest_sha256']==meta['manifest_sha256']
 try: service.authenticate(meta['old_token'],'token')
 except DomainError: pass
 else: raise AssertionError('Old credentials survived rollback restore')
 assert all(row['kind']!='knowledge_task' for row in w['jobs'])
doc=meta['document']
value=DocumentService(service).read_revision(owner,doc['entity_id'],doc['entity_revision_id'])
print(json.dumps({'document_revision_id':doc['entity_revision_id'],'content_sha256':value['content_sha256'],'section_revision_ids':[r['entity_revision_id'] for r in value['sections']]}))
service.engine.dispose()
"""


def run(directory):
    root = Path(directory).absolute()
    if ".vks" not in root.parts or root.exists():
        raise ValueError("Use a new private .vks/ exercise directory and dedicated empty databases")
    root.mkdir(parents=True, mode=0o700)
    old = root / "baseline"
    old.mkdir()
    archive = subprocess.check_output(["git", "archive", BASE])
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        tar.extractall(old, filter="data")
    source, target = os.environ["VKS_TEST_UPGRADE_SOURCE_URL"], os.environ["VKS_TEST_UPGRADE_TARGET_URL"]
    tools = os.environ["VKS_TEST_PG_TOOLS_CONTAINER"]
    environment = {k: v for k, v in os.environ.items() if not k.startswith("VKS_")}
    environment.update(
        PYTHONPATH=str(old / "services/api") + os.pathsep + str(old),
        VKS_AUTO_MIGRATE="1",
        VKS_TASK_MODE="external",
        VKS_UPGRADE_META=str(root / "private-meta.json"),
    )

    def old_binary(phase, store, url):
        result = subprocess.run(
            [sys.executable, "-c", OLD],
            cwd=old,
            env=dict(environment, VKS_UPGRADE_PHASE=phase, VKS_UPGRADE_STORE=str(store), VKS_UPGRADE_URL=url),
            capture_output=True,
        )
        (root / (phase + "-private-stderr.log")).write_bytes(result.stderr)
        if result.returncode:
            raise RuntimeError("Old-binary exercise failed; see private phase log")
        return json.loads(result.stdout)

    original_store = root / "original"
    before = old_binary("seed", original_store, source)
    meta = json.loads((root / "private-meta.json").read_bytes())
    os.environ["VKS_AUTO_MIGRATE"] = "1"
    service = Service(original_store, source)
    _, owner = service.login(service.bootstrap_token)
    doc = meta["document"]
    upgraded = DocumentService(service).read_revision(owner, doc["entity_id"], doc["entity_revision_id"])
    assert before["content_sha256"] == upgraded["content_sha256"]
    assert before["section_revision_ids"] == [r["entity_revision_id"] for r in upgraded["sections"]]
    snapshot = backup(original_store, root / "pre-upgrade.zip", database_url=source, pg_tools_container=tools)
    clone = KnowledgeService(service).clone(owner, meta["pid"], meta["aid"], "V2 draft", "upgrade-clone")
    tasks = KnowledgeTaskService(service)
    job = tasks.create(
        owner, meta["pid"], TaskInput(analysis_set_id=clone["id"], task_kind="project_overview").model_dump(), "v2-task"
    )
    service.process_job(job["id"])
    assert tasks.get(owner, job["id"])["state"] == "succeeded"
    assert (
        service.workspace(owner, meta["pid"], release_id=meta["release_id"])["selected_release"]["manifest_sha256"]
        == meta["manifest_sha256"]
    )
    service.engine.dispose()
    restored = root / "rolled-back"
    restore(
        snapshot["path"], restored, expected_sha256=snapshot["sha256"], database_url=target, pg_tools_container=tools
    )
    rolled_back = old_binary("rollback", restored, target)
    assert rolled_back == before
    result = dict(
        status="pass",
        baseline_commit=BASE,
        baseline_archive_sha256=sha256(archive).hexdigest(),
        original_release_manifest_sha256=meta["manifest_sha256"],
        historical_document=before,
        v2_task_succeeded=True,
        rollback_old_binary_read_identical=True,
        old_credentials_revoked=True,
        backup_sha256=snapshot["sha256"],
        scope="isolated synthetic PostgreSQL; no production instance changed",
    )
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    run(parser.parse_args().directory)
