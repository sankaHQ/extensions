# Sanka trust audit

`sanka/trust-audit` finds the places a repository still trusts the eMudhra
certificate authorities whose private keys were compromised in October 2026
([Mozilla Bugzilla 2083449](https://bugzilla.mozilla.org/show_bug.cgi?id=2083449)).
It writes the fixes it can make mechanically into a **new patch directory**. It
never modifies an existing file.

```bash
sanka extension add sanka/trust-audit
sanka scan
sanka plan --to trust-patches
sanka apply --plan-hash sha256:...
```

`apply` creates `sanka-trust-patches/` (set `output` to change it) containing:

- `README.md`: findings, how to apply the patches, and what needs a person
- `trust-audit.patch`: one unified diff for `git apply` or `patch -p1`
- `patches/`: the same diff split per file
- `files/`: full patched copies, for code that is not under version control
- `MANIFEST.json`: the reviewed plan hash and the digests of every original
  and patched file

## What it checks

| Area | Finding | Patched |
| --- | --- | --- |
| Compromised CA certificates, keys and pins | The 8 intermediates, their Sectigo cross-certificates, certificates they issued, SPKI pins and fingerprints | Removed from multi-certificate bundles |
| Bundles and keystores | `emSign ECC Root CA - G3`, its subordinate roots, `AAA Certificate Services`, eMudhra-issued certificates in PEM, JKS and PKCS#12 files; `emSign Root CA - C1` / `ECC Root CA - C3` (removed from NSS at eMudhra's request) as info | Removed root from bundles |
| Python | `certifi` before 2026.10.19 (pins and constraints that cannot reach it) in requirements, `pyproject.toml`, uv, Poetry, PDM and Pipfile locks | Requirement and `pyproject.toml` pins |
| Go | Toolchains before 1.26.10 / 1.27.3 in `go.mod`, Dockerfiles, CI and `.tool-versions`; every module needs a rebuild | Toolchain lines and image tags |
| Node.js | Releases before 22.23.4 / 24.22.1 / 26.12.1 in `.nvmrc`, `package.json`, Dockerfiles and CI | Version files and image tags |
| Rust, PHP | `webpki-roots` / `webpki-root-certs` before 1.0.10, `composer/ca-bundle` before 1.6.0 / 1.4.16 | Commands to run |
| Java | JDKs up to 27.0.1 / 25.0.5 / 21.0.13 / 17.0.21 / 11.0.33 / 8u512 ([CVE-2026-91403](https://openjdk.org/groups/vulnerability/advisories/2026-10-21)) in images and builds | Interim steps from the OpenJDK advisory |
| Bun, Erlang | No fixed release yet | Instructions |
| Containers | Base images built before the fix, images pinned by digest | Instructions |
| Verification switched off | `verify=False`, `InsecureSkipVerify`, `rejectUnauthorized: false`, `curl -k`, `NODE_TLS_REJECT_UNAUTHORIZED=0` and similar | Never: needs a person |

The scan reads files only. It does not inspect deployed binaries, images that
were already built, host trust stores, appliances, or dependencies resolved at
install time. Rebuild and redeploy after applying the patches.

## Advisories

Each incident the audit checks is one JSON file under
`src/sanka_extension_trust_audit/advisories/`: the certificates and keys to
distrust, the roots that root programs withdrew, and the first fixed release of
each runtime or library that ships its own root store. Every file is validated
when the extension starts. A scan covers every packaged advisory at once; when
several apply, version checks use the highest fixed release and each finding
names the advisories that require it. To add one, follow
[the advisory procedure](../../docs/trust-audit-advisories.md).
