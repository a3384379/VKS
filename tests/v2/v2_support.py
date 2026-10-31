def published(service, actor, project):
    workspace = service.workspace(actor, project["id"])
    for row in workspace["relations"]:
        service.set_relation_state(
            actor,
            row["id"],
            {
                "excluded": dict(
                    decision="confirm", reason="Synthetic frozen baseline", evidence_ids=row["evidence_ids"]
                )["decision"]
                == "reject"
            },
            row["row_version"],
        )
    candidate = service.save_snapshot(
        actor, project["id"], dict(analysis_set_id=workspace["selected_analysis_set"]["id"]), "precheck"
    )
    ticket = None
    release = candidate
    return release, workspace
