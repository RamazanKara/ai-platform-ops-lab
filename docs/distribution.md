# Distribution and discovery

Release tags publish one tested source revision through four channels. Images are promoted
from the already-tested commit digest, Helm charts embed those immutable image digests, the
Python client is built separately and published with PyPI Trusted Publishing, and versioned
documentation is retained by `mike`.

## Helm OCI

The umbrella chart is the recommended public entry point:

```bash
helm pull oci://ghcr.io/ramazankara/private-ai-platform-kit/charts/platform --version 0.28.0
helm install private-ai oci://ghcr.io/ramazankara/private-ai-platform-kit/charts/platform \
  --version 0.28.0 --namespace ai-platform --create-namespace
```

Release CI publishes `artifacthub-repo.yml` to the chart repository's special
`artifacthub.io` OCI tag. To finish discoverability, a maintainer must register
`oci://ghcr.io/ramazankara/private-ai-platform-kit/charts/platform` once in the Artifact Hub
control panel, copy the assigned `repositoryID` into `artifacthub-repo.yml`, and cut the next
release. This external registration cannot be completed from repository code.

## Python package

```bash
python -m pip install private-ai-platform-kit-client==0.28.0
```

Before the first publish, register a pending PyPI Trusted Publisher with owner
`RamazanKara`, repository `private-ai-platform-kit`, workflow `ci.yml`, environment `pypi`,
and project `private-ai-platform-kit-client`. Protect the GitHub `pypi` environment with
required reviewer approval and tag-only deployment rules. CI keeps package building in an
unprivileged job; only the prebuilt artifact reaches the OIDC-enabled publish job. PyPI
attestations remain enabled.

## Versioned documentation

Main publishes the `development` documentation alias. A `v*` release tag publishes its exact
version, moves `latest`, and makes `latest` the site root. Older generated versions remain on
the `gh-pages` branch and in the version selector.

## Operator-owned one-time setup

The code and workflows are complete, but these account-level actions require repository-owner
authority:

1. Set GitHub Pages source to **GitHub Actions**.
2. Create and protect the `pypi` environment, then register its PyPI Trusted Publisher.
3. Register the platform OCI chart in Artifact Hub and record its assigned repository ID.
4. Keep GHCR packages public so anonymous Helm pulls and Artifact Hub indexing work.
