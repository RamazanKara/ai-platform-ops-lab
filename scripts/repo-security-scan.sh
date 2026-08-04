#!/usr/bin/env bash
# Canonical repository security scan: Trivy secret + misconfiguration scanning.
# Called by `make repo-security-scan` and scripts/validate.sh so the command
# cannot drift between the two. --helm-kube-version matches the tested kind
# node version (docs/version-matrix.md) so charts render and get scanned
# instead of being skipped for Trivy's default Kubernetes 1.20.
#
# deploy/vendor holds pinned upstream manifests (reviewed at vendor time, not
# repo-authored); like the intentionally-bad Kyverno test fixtures, they are
# excluded from the repo-authored HIGH/CRITICAL gate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Trivy invokes Helm for chart misconfiguration scanning. Vendor the umbrella
# chart's locked file:// dependencies first so a clean checkout is actually
# rendered and scanned instead of being skipped after "dependency not found".
command -v helm >/dev/null 2>&1 || {
  echo "helm is required to render the umbrella chart for repository scanning" >&2
  exit 1
}
helm dependency build deploy/charts/platform >/dev/null

exec trivy fs \
  --scanners secret,misconfig \
  --severity HIGH,CRITICAL \
  --exit-code 1 \
  --timeout 10m \
  --ignorefile .trivyignore.yaml \
  --helm-kube-version 1.31.4 \
  --skip-dirs .tools \
  --skip-dirs results \
  --skip-dirs .out \
  --skip-dirs deploy/policies/kyverno/tests/resources \
  --skip-dirs deploy/vendor \
  --skip-dirs src/inference-gateway/.venv \
  --skip-dirs src/rag-service/.venv \
  .
