"""Runs inside an agent's Chalk sandbox: forward the sandbox's identity, hop by hop, to BigQuery.

Usage: python agent_chain.py '<plan json>'

The plan names a service account to impersonate (or none) and the tables to read. Each hop is
printed as one JSON line as soon as it finishes, so the caller can stream them. Raw tokens are
never printed: hops report decoded claims, a short SHA-256 fingerprint, and the token's TTL.
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import chalkcompute

STS_URL = "https://sts.googleapis.com/v1/token"
IAM_CREDENTIALS_URL = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateAccessToken"
BQ_ROWS_URL = (
    "https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}/data?maxResults=5"
)
BQ_FIELDS_URL = (
    "https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}?fields=schema"
)


def emit(**hop: object) -> None:
    print(json.dumps(hop), flush=True)


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def http(method: str, url: str, token: str | None, body: dict | None, form: bool) -> tuple[int, dict]:
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, {"error": raw.decode(errors="replace")[:300]}


def google_error(body: dict) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message", ""))[:300]
    return str(body.get("error_description") or err or "")[:300]


def read_table(project: str, dataset: str, table: str, token: str, via: str) -> None:
    t0 = time.monotonic()
    status, body = http("GET", BQ_ROWS_URL.format(project=project, dataset=dataset, table=table), token, None, False)
    ms = round((time.monotonic() - t0) * 1000)
    target = f"bigquery:{dataset}.{table}"
    if status != 200:
        emit(hop="read", target=target, via=via, decision="denied", status=status, ms=ms, detail=google_error(body))
        return
    _, schema = http("GET", BQ_FIELDS_URL.format(project=project, dataset=dataset, table=table), token, None, False)
    cols = [f["name"] for f in schema.get("schema", {}).get("fields", [])]
    rows = [[cell.get("v") for cell in r.get("f", [])] for r in body.get("rows", [])]
    emit(hop="read", target=target, via=via, decision="allowed", status=status, ms=ms, columns=cols, rows=rows)


def main() -> None:
    plan = json.loads(sys.argv[1])
    project, dataset = plan["project"], plan["dataset"]

    # Hop 1: the sandbox's own Chalk identity, issued at launch to whoever launched it.
    mounted = open(os.environ["CHALK_WEB_IDENTITY_TOKEN_FILE"]).read().strip()
    mc = jwt_claims(mounted)
    emit(
        hop="sandbox",
        decision="issued",
        detail="Chalk mounted an identity for the launcher into the sandbox",
        claims={"kind": mc.get("kind"), "sandbox_id": mc.get("sandbox_id")},
        fingerprint=fingerprint(mounted),
        ttl_s=int(mc["exp"] - time.time()),
    )

    # Hop 2: a short-lived OIDC token for the cloud provider's audience.
    t0 = time.monotonic()
    oidc = chalkcompute.ConnectClient().get_workload_identity_token(plan["audience"])
    oc = jwt_claims(oidc)
    emit(
        hop="oidc",
        decision="issued",
        ms=round((time.monotonic() - t0) * 1000),
        claims={k: oc.get(k) for k in ("iss", "sub", "aud", "environment_id")},
        fingerprint=fingerprint(oidc),
        ttl_s=int(oc["exp"] - time.time()),
    )

    # Hop 3: the cloud's token service checks the issuer, audience, and pool condition.
    t0 = time.monotonic()
    status, body = http(
        "POST",
        STS_URL,
        None,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": plan["sts_audience"],
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "subject_token": oidc,
        },
        True,
    )
    ms = round((time.monotonic() - t0) * 1000)
    if status != 200:
        emit(hop="sts", target="sts.googleapis.com", decision="denied", status=status, ms=ms, detail=google_error(body))
        return
    federated = body["access_token"]
    emit(
        hop="sts",
        target="sts.googleapis.com",
        decision="allowed",
        status=status,
        ms=ms,
        principal=f"principal://…/subject/{oc['sub']}",
        fingerprint=fingerprint(federated),
        ttl_s=body.get("expires_in"),
    )

    # Hop 4: tables granted to the whole Chalk environment, read with the federated token directly.
    for table in plan.get("direct_tables", []):
        read_table(project, dataset, table, federated, "federated principal")

    sa = plan.get("service_account")
    if not sa:
        return

    # Hop 5: act as a narrower service account, if the pool binding allows this subject to.
    t0 = time.monotonic()
    status, body = http(
        "POST",
        IAM_CREDENTIALS_URL.format(sa=sa),
        federated,
        {"scope": ["https://www.googleapis.com/auth/bigquery.readonly"], "lifetime": "300s"},
        False,
    )
    ms = round((time.monotonic() - t0) * 1000)
    if status != 200:
        emit(hop="impersonate", target=sa, decision="denied", status=status, ms=ms, detail=google_error(body))
        return
    sa_token = body["accessToken"]
    emit(
        hop="impersonate",
        target=sa,
        decision="allowed",
        status=status,
        ms=ms,
        fingerprint=fingerprint(sa_token),
        ttl_s=300,
    )

    # Hop 6: the service account's own table grants decide every read.
    for table in plan.get("sa_tables", []):
        read_table(project, dataset, table, sa_token, sa.split("@")[0])


if __name__ == "__main__":
    main()
