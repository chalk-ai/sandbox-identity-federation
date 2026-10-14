# Sandbox identity forwarding

Give every agent's sandbox an identity of its own, and let your cloud's IAM decide what it can
reach. This demo runs three agents for a fictional outfitter, each in a [Chalk](https://chalk.ai)
sandbox launched with its own service token, and follows each sandbox's identity into Google
Cloud one hop at a time:

1. **Sandbox identity.** `chalk_identity=True` mounts a Chalk identity for whoever launched the
   sandbox, a person or a service token, into the sandbox.
2. **OIDC token.** Inside the sandbox, `ConnectClient().get_workload_identity_token(audience)`
   returns a one-hour token signed by your Chalk API (`iss`), naming the environment and the
   launcher (`sub = v1:env:<environment>:service-token:<id>`), for one audience (`aud`).
3. **Token exchange.** Google's STS checks the signature against Chalk's published keys, the
   audience, and the pool's attribute condition, and returns a federated access token.
4. **Direct grants.** Tables granted to the whole Chalk environment (a `principalSet`) are read
   with the federated token itself.
5. **Impersonation.** The federated principal asks IAM Credentials for a five-minute token for a
   service account. Only subjects bound with `roles/iam.workloadIdentityUser` get one.
6. **Reads.** BigQuery checks each read against the service account's table grants.

The scenarios include three refusals: the release agent reading customer payments (BigQuery 403),
an unvetted agent trying to act as the release agent's service account (IAM 403), and a token
minted for a different audience (STS 400). Tokens never leave the sandbox: the page shows decoded
claims, SHA-256 fingerprints and TTLs only.

| File | Role |
| --- | --- |
| `server.py` | FastAPI app on a Chalk ScalingGroup. Keeps one warm sandbox per agent and streams each scenario's hops as NDJSON. Visitors choose a scenario id; plans and credentials stay server-side. |
| `agent_chain.py` | Runs inside an agent's sandbox: mints the OIDC token, exchanges it, impersonates, reads. One JSON line per hop. |
| `index.html` | The page: the token chain, the identity in flight, the rows each agent could read, and an audit trail. `?embed=1` for iframes. |
| `setup_gcp.sh` | Creates the pool, provider, dataset, service accounts and bindings in your project. |
| `print_subjects.py` | Prints the subject each agent's sandbox federates as, for `setup_gcp.sh`. |
| `deploy.py` | Deploys the server as a ScalingGroup. |

## Run it

You need a Chalk environment, a Google Cloud project you can create IAM resources in,
[`uv`](https://docs.astral.sh/uv/), `gcloud` and `bq`.

```bash
export CHALK_API_SERVER=https://api.chalk.ai CHALK_ENVIRONMENT_ID=<your environment>
export CHALK_CLIENT_ID=... CHALK_CLIENT_SECRET=...   # used only to deploy the server

# 1. One service token per agent; each launches and execs into its own sandbox.
for a in release support scratch; do
  chalk token create --name "sandbox-identity-$a-agent" \
    --permission deploy.create --permission deploy.read --permission deploy.redeploy \
    --output "$a.json"
done
# Copy the three client ids and secrets into agents.env, in the shape of agents.env.example.

# 2. The subject each agent federates as.
./print_subjects.py --agents-env agents.env

# 3. Pool, provider, tables, service accounts and bindings in your project.
./setup_gcp.sh my-gcp-project "$CHALK_API_SERVER" <release subject> <support subject> <scratch subject>

# 4. The demo server.
./deploy.py --agents-env agents.env --gcp-project my-gcp-project --gcp-project-number <number>
```

Open the printed URL and pick a scenario.

## Mapping identities onto your IAM

Bind whichever subject fits:

- **Per person**: `principal://iam.googleapis.com/<pool>/subject/v1:env:<env>:user:<user id>`.
  Every sandbox that developer launches gets the same access.
- **Per agent**: `.../subject/v1:env:<env>:service-token:<token id>`, as in this demo. The agent's
  access is separate from any developer's.
- **Per environment**: `principalSet://iam.googleapis.com/<pool>/attribute.environment_id/<env>`.
  One binding covers everyone in a Chalk environment, so new users need no IAM change.

Grant roles to these principals directly, or let them act as a service account you already have
(`roles/iam.workloadIdentityUser`) so its existing roles carry over. Cloud Audit Logs record the
federated subject on calls it makes directly, and the service account on calls made as one.

## License

Apache-2.0. All company names and data in this demo are fictional.
