"""Controlled subprocess death at the owning workflow's active-pointer boundary."""

from __future__ import annotations

import copy
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from exacly import delivery_automation as delivery
from exacly import deployment_automation as deploy
from exacly import execution_store as store
from exacly import local_static_deployment as owner
from exacly import operational_observation as observer

Doc = dict[str, Any]
packet = json.load(sys.stdin)


def reader(configuration: Doc, policy: Doc, request: Doc, *, deadline: store.Deadline) -> bytes:
    deadline.check()
    return bytes.fromhex(packet["content"])


original = owner._replace


def killed(path: Path, value: Doc) -> None:
    if packet["after_cas"]:
        original(path, value)
    os.kill(os.getpid(), signal.SIGKILL)


action = sys.argv[1]
if action == "fingerprints":
    configuration = deploy.validate_configuration(packet["configuration"])
    policy = copy.deepcopy(packet["policy"])
    policy["observer_configuration_digest"] = observer.configuration_digest(configuration["observer"])
    preview = deploy.LocalStaticDeploymentAdapter(configuration, policy, artifact_reader=reader)
    print(
        json.dumps(
            {
                "observer_configuration_digest": policy["observer_configuration_digest"],
                "operation_profile_digest": preview.operation_profile_digest,
                "configuration_digest": preview.configuration_digest,
            }
        )
    )
    sys.exit(0)

approval = packet["approval"]
adapter = deploy.LocalStaticDeploymentAdapter(
    packet["configuration"],
    packet["policy"],
    approval_pin=delivery.ApprovalPin(approval["issuer"], approval["approval_id"], delivery.digest(approval)),
    artifact_reader=reader,
)
if action == "kill":
    owner._replace = killed
    result = adapter.invoke(packet["intent"], packet["payload"], approval, deadline=store.Deadline(30))
    print(json.dumps(result), file=sys.stderr)
    sys.exit(99)
if action == "reconcile":
    result = adapter.reconcile(packet["intent"], packet["payload"], deadline=store.Deadline(30))
    adapter.validate_provenance("reconcile", packet["intent"]["request"], packet["payload"], result["provenance"])
    adapter.validate_result("reconcile", packet["intent"]["request"], packet["payload"], result)
    print(json.dumps(result))
    sys.exit(0)
raise ValueError("unsupported fixed crash-fixture action")
