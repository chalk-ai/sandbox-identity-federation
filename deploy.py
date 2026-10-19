#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.8", "chalkpy[runtime]==2.155.2", "httpx"]
# ///
"""Deploy the sandbox identity forwarding demo as a Chalk ScalingGroup.

    ./deploy.py --agents-env agents.env --gcp-project my-project --gcp-project-number 123456789

Chalk credentials and API host for the deploy come from your existing `chalk login`
or the standard CHALK_* environment variables. agents.env holds one service token per agent
(see README.md); it is injected as a Chalk secret scoped to the scaling group.
"""

import argparse
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents-env", required=True)
    parser.add_argument("--gcp-project", required=True)
    parser.add_argument("--gcp-project-number", required=True)
    args = parser.parse_args()

    from chalkcompute import ConnectClient

    with ConnectClient() as connection:
        api_server = connection.api_server
        env_id = connection.environment_id
    # Sandboxes call the Chalk API at the host its OIDC issuer names, so that is the host
    # their network policy has to allow.
    issuer = httpx.get(f"{api_server}/.well-known/openid-configuration", timeout=30).json()["issuer"]

    import chalkcompute

    image = (
        chalkcompute.Image.debian_slim("3.12")
        .pip_install(["chalkcompute>=2.13.8", "chalkpy[runtime]==2.155.2", "fastapi==0.116.1", "uvicorn[standard]==0.35.0"])
        .add_local_file(str(HERE / "server.py"), "/app/server.py", strategy="copy")
        .add_local_file(str(HERE / "agent_chain.py"), "/app/agent_chain.py", strategy="copy")
        .add_local_file(str(HERE / "index.html"), "/app/index.html", strategy="copy")
        .workdir("/app")
    )
    provider = (
        f'//iam.googleapis.com/projects/{args.gcp_project_number}/locations/global/workloadIdentityPools/chalk-sandbox-identity-demo/providers/chalk'
    )
    sg = chalkcompute.ScalingGroup(
        image=image,
        name='sandbox-identity',
        cpu="1",
        memory="2Gi",
        env={
            "DEMO_CHALK_ENVIRONMENT_ID": env_id,
            "DEMO_CHALK_API_SERVER": api_server,
            "DEMO_SANDBOX_API_HOST": urlparse(issuer).hostname or "",
            "DEMO_WIF_PROVIDER": provider,
            "DEMO_GCP_PROJECT": args.gcp_project,
            "DEMO_GCP_PROJECT_NUMBER": args.gcp_project_number,
            "DEMO_BQ_DATASET": 'sandbox_identity_demo',
        },
        secrets=[chalkcompute.Secret.from_local_env_file(args.agents_env)],
        entrypoint=["python", "-c", "import server, uvicorn; uvicorn.run(server.app, host='0.0.0.0', port=8080)"],
        port=8080,
        min_replicas=1,
        max_replicas=1,
    )
    t0 = time.monotonic()
    sg.deploy(build_timeout=1200.0, ready_timeout=900.0)
    print(f"deploy returned after {time.monotonic() - t0:.0f}s")
    print("web_url:", sg.web_url)


if __name__ == "__main__":
    main()
