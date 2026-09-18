# deploy/

Deployment files for the Artifact Cache Gateway server (hosted mode).

- `Dockerfile.hosted`: hosted image build file.
- `compose.yaml`: compose deploy description.
- `hosted-entrypoint.sh` / `hosted_supervisor.py`: container entry and process supervision
  (gateway and its subordinate processes).
- `install_mihomo.py`: server-side upstream proxy component install script.

See `docs/operator-guide.md` for deployment and operations details. Proxy-related sensitive
config can only be injected via platform secrets; it must not be written into committed files
in this directory.