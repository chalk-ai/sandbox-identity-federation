#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.8", "chalkpy[runtime]==2.155.2"]
# ///
"""Print the workload identity subject each agent's sandbox federates as.

    ./print_subjects.py --agents-env agents.env

Launches one short-lived sandbox per agent with that agent's service token and
chalk_identity=True, and prints the `sub` claim of the OIDC token minted inside it, e.g.
v1:env:<environment>:service-token:<id>. setup_gcp.sh binds IAM roles to these subjects.
Uses the API host and environment selected by your existing `chalk login` or CHALK_* settings.
"""

import argparse

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
    from chalkcompute import ConnectClient
    from chalk.client import ChalkClient

    with ConnectClient() as connection:
        api_server = connection.api_server
        env_id = connection.environment_id
    image = chalkcompute.Image.debian_slim("3.12").pip_install(["chalkcompute>=2.13.8", "chalkpy[runtime]==2.155.2"])
    for agent, key in AGENTS.items():
        client = ChalkClient(
            client_id=creds[f"DEMO_AGENT_{key}_CLIENT_ID"],
            client_secret=creds[f"DEMO_AGENT_{key}_CLIENT_SECRET"],
            api_server=api_server,
            environment=env_id,
        )
        sandbox = chalkcompute.Sandbox(image=image, chalk_identity=True, chalk_client=client).run()
        try:
            print(f"{agent}\t{sandbox.exec('python', '-c', PROBE).stdout_text.strip()}")
        finally:
            sandbox.terminate()


if __name__ == "__main__":
    main()
