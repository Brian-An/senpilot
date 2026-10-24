from pathlib import Path

import yaml


def test_blueprint_prompts_for_database_on_a_service() -> None:
    blueprint = yaml.safe_load(Path("render.yaml").read_text())
    for group in blueprint.get("envVarGroups", []):
        assert all(variable.get("sync") is not False for variable in group["envVars"])
    services = {service["name"]: service for service in blueprint["services"]}
    web_database = next(
        v for v in services["senpilot-api"]["envVars"] if v.get("key") == "DATABASE_URL"
    )
    assert web_database["sync"] is False
    worker_database = next(
        v for v in services["senpilot-worker"]["envVars"] if v.get("key") == "DATABASE_URL"
    )
    assert worker_database["fromService"] == {
        "type": "web",
        "name": "senpilot-api",
        "envVarKey": "DATABASE_URL",
    }
