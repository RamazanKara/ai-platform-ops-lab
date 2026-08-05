# Release Verification

Use this checklist before trusting a public release in a customer-owned cluster.

Set the release and repository once:

```bash
export RELEASE=v0.28.1
export IMAGE_REPO=ghcr.io/ramazankara/private-ai-platform-kit
```

## Helm OCI Charts

Tag builds publish each chart to `oci://$IMAGE_REPO/charts`.

```bash
helm pull "oci://$IMAGE_REPO/charts/inference-gateway" --version "${RELEASE#v}"
helm pull "oci://$IMAGE_REPO/charts/rag-service" --version "${RELEASE#v}"
helm pull "oci://$IMAGE_REPO/charts/agent-workspace" --version "${RELEASE#v}"
```

Render the downloaded chart before installing:

```bash
helm template verify-inference "inference-gateway-${RELEASE#v}.tgz" \
  --values deploy/clusters/customer/values/inference-gateway.yaml >/tmp/inference.yaml
```

## Image Signatures

Release images are signed by digest with Cosign in GitHub Actions.

```bash
cosign verify "$IMAGE_REPO/inference-gateway:$RELEASE" \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

cosign verify "$IMAGE_REPO/rag-service:$RELEASE" \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Release images are also multi-arch (`linux/amd64` and `linux/arm64`); the signature covers the manifest list, so the same `cosign verify` works on Apple Silicon and arm64 (Graviton/Ampere) clusters.

## Chart Signatures

Helm chart OCI artifacts are cosign-signed by digest in the same release workflow as the images.

```bash
cosign verify "$IMAGE_REPO/charts/inference-gateway:${RELEASE#v}" \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

cosign verify "$IMAGE_REPO/charts/rag-service:${RELEASE#v}" \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

cosign verify "$IMAGE_REPO/charts/agent-workspace:${RELEASE#v}" \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Chart OCI tags drop the leading `v` (`${RELEASE#v}`) to match the chart `version`, while runtime image tags keep it.

## Provenance And SBOM Attestations

Tag builds publish SLSA provenance and SPDX SBOM attestations to GHCR for each runtime image.

```bash
cosign verify-attestation "$IMAGE_REPO/inference-gateway:$RELEASE" \
  --type slsaprovenance \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

cosign verify-attestation "$IMAGE_REPO/rag-service:$RELEASE" \
  --type slsaprovenance \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

cosign verify-attestation "$IMAGE_REPO/inference-gateway:$RELEASE" \
  --type https://spdx.dev/Document \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

cosign verify-attestation "$IMAGE_REPO/rag-service:$RELEASE" \
  --type https://spdx.dev/Document \
  --certificate-identity-regexp 'https://github.com/.+/.github/workflows/ci.yml@refs/tags/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## SBOM And Scan Checksums

Download the release assets and verify the checksum manifest:

```bash
gh release download "$RELEASE" \
  --pattern 'inference-gateway.spdx.json' \
  --pattern 'rag-service.spdx.json' \
  --pattern 'trivy-results.sarif' \
  --pattern 'trivy-rag-results.sarif' \
  --pattern 'supply-chain-checksums.txt'

sha256sum --check supply-chain-checksums.txt
```

Review the SBOMs and SARIF files before promotion. The Trivy release gate is configured to fail on HIGH or CRITICAL image vulnerabilities.

## Strict Evidence

Strict release evidence must be generated from current artifacts, not sample evidence:

```bash
make validate-full
make image-scan
make supply-chain-check
make loadtest-local
make evidence
make release-gate-strict
```

For a live customer-style validation path, run the local cluster checks and generate live evidence:

```bash
QUICKSTART_DIRECT_APPLY=1 make quickstart
make trace-smoke
make tenant-smoke
make agent-smoke
make evidence LIVE=1
```

Record the command output, generated evidence paths under `results/`, image digests, chart versions, and GitHub Actions run URL in the release notes.
