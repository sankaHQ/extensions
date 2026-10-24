# Trust audit advisories

`sanka/trust-audit` checks every advisory under
`packages/sanka-extension-trust-audit/src/sanka_extension_trust_audit/advisories/`.
An advisory records one incident from primary sources: the certificates and keys to
distrust, the roots that root programs withdrew, and the first fixed release of each
runtime or library that ships its own root store. This page is the procedure for
shipping a new or corrected advisory within hours of a disclosure.

| Target | Step |
| --- | --- |
| T+0 | The incident is disclosed |
| T+1 h | [Primary sources verified](#2-verify-the-facts) |
| T+2 h | [Advisory pull request green](#4-test-and-open-the-pull-request) |
| T+3 h | [Release published and verified](#5-release) |
| T+3 h | [Install command for that release published](#6-distribute-without-a-cli-release) |
| Next CLI release | [Default catalog advanced](#7-advance-the-default-catalog) |

## 1. Watch the primary sources

- Mozilla's Bugzilla CA Program components (CA Certificate Compliance, CA Certificate
  Root Program) and the Mozilla Security Blog.
- Chrome Root Program announcements on the Google Online Security Blog, and the Apple
  and Microsoft root programs.
- GitHub Security Advisories for `certifi`, `webpki-roots` / `webpki-root-certs` and
  `composer/ca-bundle`.
- Node.js, Go and OpenJDK security releases; Debian, Ubuntu and Alpine
  `ca-certificates` updates.
- crt.sh for the certificates themselves.

## 2. Verify the facts

Record a link for each of these:

- every certificate to distrust, by crt.sh ID or SHA-256;
- the roots that root programs withdrew, and which programs withdrew them;
- the first fixed release of each runtime or library that ships its own root store,
  and the ecosystems with no fix yet.

News coverage and vendor summaries are leads, not sources. When two primary sources
disagree, use the one closest to the shipped code (release notes, the commit, the
published package) and say which in the pull request.

## 3. Scaffold the advisory

From a branch cut from `origin/main`:

```bash
uv sync --frozen --all-packages
uv run python scripts/new_trust_advisory.py \
  --id example-2027-01 \
  --title "Example CA key compromise (January 2027)" \
  --url "https://bugzilla.mozilla.org/show_bug.cgi?id=NNNNNNN" \
  --published 2027-01-15 \
  --source "Bug NNNNNNN: Example CA key compromise=https://bugzilla.mozilla.org/show_bug.cgi?id=NNNNNNN" \
  --trust-label "Example Root CA - G2" \
  --ca-name Example \
  --verification-risk "With these keys public, anyone on the network path can impersonate the server." \
  --cert "<crt.sh ID or SHA-256>" \
  --cert "<crt.sh ID or SHA-256>" \
  --removed-root "<crt.sh ID or SHA-256>" \
  --withdrawn-by "Mozilla and Chrome"
```

The script downloads each certificate from crt.sh and checks that it parses and
matches the fingerprint you gave. It writes `advisories/<id>.json` with every
certificate's name, issuer, SHA-256 and key digest, plus one fixture per certificate
under `tests/fixtures/advisories/<id>/`. Use `--cert-pem` and `--removed-root-pem`
for certificates published as files (for example, attached to a bug).

Then add the rest to the JSON by hand. `advisories/emudhra-2026-10.json` is a
complete example.

- `fixes`: `certifi`, `webpki-roots`, `composer-ca-bundle` (`target`, `lines`),
  `node` (`last_affected_major`, `fixed` per major), `go` (`last_affected`, `fixed` per
  line) and `java` (`last_affected_major`, `last_affected` per major, `action`). Leave
  out what the incident does not affect.
- `pending`: `bun` and `erlang-certifi`, while they have no fixed release.
- `planned_removals`, `affected_parents`, `cross_sign_roots`, `issuer_organizations`
  and `images_fixed_after`, when the incident has them.

The extension validates every advisory when it starts and refuses to run with an
invalid one.

## 4. Test and open the pull request

```bash
uv run python -m pytest packages/sanka-extension-trust-audit/tests -q
make check
```

`test_packaged_advisories_match_their_certificates` compares each advisory with its
certificate fixtures and scans them. When several advisories apply to a project,
version checks use the highest fixed release, and each finding names the advisories
that require it in its `advisory` field.

If the current trust-audit version is already published, bump it in
`packages/sanka-extension-trust-audit/pyproject.toml`, `extension.template.json` (both
fields) and `src/sanka_extension_trust_audit/__init__.py`, and update the trust-audit
expectation in `tests/test_marketplace.py`. Then run `uv lock`. Rewrite the manifest
last, after every other edit:

```bash
rm -rf release/trust-audit
uv run python scripts/build_trust_audit_release.py --write-manifest
make build-trust-audit-release
```

## 5. Release

Follow [the trust audit release procedure](trust-audit-release.md):

1. Land the pull request.
2. Create the tag `trust-audit-v<version>` at the landed commit.
3. Dispatch `.github/workflows/trust-audit-release.yml` on the tag.
4. Verify the published assets.

The workflow reads the version from the package, so a release needs no workflow
edit.

## 6. Distribute without a CLI release

Users can install the new release at once by pinning the official marketplace to the
landed release commit:

```bash
sanka extension marketplace add https://github.com/sankaHQ/extensions.git \
  --name reviewed --revision <full release commit SHA>
sanka extension add sanka/trust-audit --marketplace reviewed
```

Publish this command wherever the incident is described, until the CLI's default
catalog includes the release.

## 7. Advance the default catalog

In `sankaHQ/sanka`:

1. Set `OFFICIAL_REVISION` in `packages/sanka-cli/src/sanka/runtime/extensions/store.py`
   to a commit that contains the release.
2. Release the CLI.
3. Update the Homebrew formula in `sankaHQ/homebrew-cli`.

## Hosted runs

Sanka Cloud pins each trust-audit release by version and wheel digest. Its operators
update that pin after the release is verified; until then, hosted runs keep the
previous release.
