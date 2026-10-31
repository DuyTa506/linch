"""Principal roles use canonical JSON in durable workflow call identities."""

import json


def test_principal_roles_are_canonical_in_workflow_call_fingerprint() -> None:
    from linch import Principal, RunOptions
    from linch.ext.workflow.context import _run_options_fingerprint

    options = RunOptions(principal=Principal("tenant", "user", frozenset({"reader", "editor"})))

    payload = json.loads(_run_options_fingerprint(options))

    assert payload["principal"] == {
        "tenant": "tenant",
        "user": "user",
        "roles": ["editor", "reader"],
    }
