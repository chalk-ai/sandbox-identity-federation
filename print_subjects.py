#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.5"]
# ///
"""Print the workload identity subject each agent's sandbox federates as.

    ./print_subjects.py --agents-env agents.env

Launches one short-lived sandbox per agent with that agent's service token and
chalk_identity=True, and prints the `sub` claim of the OIDC token minted inside it, e.g.
v1:env:<environment>:service-token:<id>. setup_gcp.sh binds IAM roles to these subjects.
Uses CHALK_API_SERVER and CHALK_ENVIRONMENT_ID from the environment.
"""

import argparse
import os
import types

import chalkcompute

AGENTS = {"release-agent": "RELEASE", "support-agent": "SUPPORT", "scratch-agent": "SCRATCH"}

PROBE = r"""
import base64, json, chalkcompute
tok = chalkcompute.ConnectClient().get_workload_identity_token("subject-probe")
payload = tok.split(".")[1]
payload += "=" * (-len(payload) % 4)
print(json.loads(base64.urlsafe_b64decode(payload))["sub"])
"""


def read_env_file(path: str) -> dict[str, str]:
    values = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents-env", required=True)
    args = parser.parse_args()
    creds = read_env_file(args.agents_env)
    api_server = os.environ["CHALK_API_SERVER"]
    env_id = os.environ["CHALK_ENVIRONMENT_ID"]
    image = chalkcompute.Image.debian_slim("3.12").pip_install(["chalkcompute==2.13.5"])
    for agent, key in AGENTS.items():
        client = types.SimpleNamespace(
            _client_id=creds[f"DEMO_AGENT_{key}_CLIENT_ID"],
            _client_secret=creds[f"DEMO_AGENT_{key}_CLIENT_SECRET"],
            _api_server=api_server,
            _primary_environment=env_id,
        )
        sandbox = chalkcompute.Sandbox(image=image, chalk_identity=True, chalk_client=client).run()
        try:
            print(f"{agent}\t{sandbox.exec('python', '-c', PROBE).stdout_text.strip()}")
        finally:
            sandbox.terminate()


if __name__ == "__main__":
    main()
