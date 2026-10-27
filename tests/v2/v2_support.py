def published(service, actor, project):
    workspace = service.workspace(actor, project["id"])
    for row in workspace["relations"]:
        service.review_relation(
            actor,
            row["id"],
            dict(decision="confirm", reason="Synthetic frozen baseline", evidence_ids=row["evidence_ids"]),
            row["row_version"],
        )
    candidate = service.request_release(
        actor, project["id"], dict(analysis_set_id=workspace["selected_analysis_set"]["id"]), "precheck"
    )
    ticket = service.confirm_release(actor, candidate["candidate_id"])
    release = service.publish(
        actor, candidate["candidate_id"], dict(ticket, change_note="Synthetic frozen V2 baseline"), "publish"
    )
    return release, workspace
