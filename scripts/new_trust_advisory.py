# SPDX-License-Identifier: Apache-2.0
"""Scaffold a sanka/trust-audit advisory from the certificates themselves.

Downloads each certificate from crt.sh (by crt.sh ID or SHA-256) or reads it from a
PEM file, checks it parses and matches the requested fingerprint, and writes:

- src/sanka_extension_trust_audit/advisories/<id>.json with every certificate's name,
  issuer, SHA-256 and SPKI digest, the removed roots and the sources you name;
- tests/fixtures/advisories/<id>/<sha256>.pem, one file per certificate, which the
  tests compare against the advisory.

Fixed releases, pending ecosystems and finding wording are added by hand afterwards
from the vendors' own advisories. See docs/trust-audit-advisories.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sanka_extension_trust_audit.advisory import parse_advisory
from sanka_extension_trust_audit.certs import Certificate, read_pem

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "sanka-extension-trust-audit"
ADVISORIES = PACKAGE / "src" / "sanka_extension_trust_audit" / "advisories"
FIXTURES = PACKAGE / "tests" / "fixtures" / "advisories"
_REFERENCE = re.compile(r"\d+|[0-9a-fA-F]{64}")
_ATTEMPTS = 6


def download(reference: str) -> bytes:
    """One PEM certificate from crt.sh, by crt.sh ID or SHA-256 fingerprint."""
    if not _REFERENCE.fullmatch(reference):
        raise ValueError(f"{reference}: expected a crt.sh ID or a SHA-256 fingerprint")
    request = Request(
        f"https://crt.sh/?d={reference}",
        headers={
            "User-Agent": "sanka-trust-audit-advisory/1 (+https://github.com/sankaHQ/extensions)"
        },
    )
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=60) as response:
                body: bytes = response.read(1 << 20)
                return body
        except (HTTPError, URLError, TimeoutError) as exc:
            # crt.sh rate-limits and times out under load; back off, then give up.
            if attempt == _ATTEMPTS:
                raise RuntimeError(f"{reference}: crt.sh download failed ({exc})") from exc
            time.sleep(10 * attempt)
    raise AssertionError("unreachable")


def certificate(pem: bytes, reference: str) -> Certificate:
    found = read_pem(pem)
    if len(found) != 1:
        raise ValueError(f"{reference}: expected exactly one certificate, found {len(found)}")
    (cert,) = found
    if len(reference) == 64 and cert.sha256 != reference.lower():
        raise ValueError(f"{reference}: crt.sh returned a certificate with SHA-256 {cert.sha256}")
    return cert


def load(references: list[str], files: list[Path]) -> list[tuple[Certificate, bytes]]:
    loaded = []
    for index, reference in enumerate(references):
        if index:
            time.sleep(2)  # crt.sh asks clients not to hammer it.
        pem = download(reference)
        loaded.append((certificate(pem, reference), pem))
    for path in files:
        pem = path.read_bytes()
        loaded.append((certificate(pem, str(path)), pem))
    return loaded


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--id", required=True, help="e.g. emudhra-2026-10")
    parser.add_argument("--title", required=True)
    parser.add_argument("--url", required=True, help="the primary incident report")
    parser.add_argument("--published", required=True, help="disclosure date, YYYY-MM-DD")
    parser.add_argument(
        "--source", action="append", default=[], metavar="TITLE=URL", help="a primary source"
    )
    parser.add_argument(
        "--trust-label", required=True, help="the root findings name, e.g. emSign ECC Root CA - G3"
    )
    parser.add_argument("--ca-name", action="append", default=[], help="keystore search term")
    parser.add_argument(
        "--verification-risk",
        required=True,
        help="one sentence on what disabled verification exposes during this incident",
    )
    parser.add_argument(
        "--cert", action="append", default=[], help="crt.sh ID or SHA-256 of a CA to distrust"
    )
    parser.add_argument("--cert-pem", action="append", default=[], type=Path)
    parser.add_argument(
        "--removed-root", action="append", default=[], help="crt.sh ID or SHA-256 of a root"
    )
    parser.add_argument("--removed-root-pem", action="append", default=[], type=Path)
    parser.add_argument("--withdrawn-by", help="who removed the roots, e.g. Mozilla and Chrome")
    parser.add_argument("--force", action="store_true", help="replace an existing advisory")
    parser.add_argument("--advisories-dir", type=Path, default=ADVISORIES, help=argparse.SUPPRESS)
    parser.add_argument("--fixtures-dir", type=Path, default=FIXTURES, help=argparse.SUPPRESS)
    args = parser.parse_args()

    target = args.advisories_dir / f"{args.id}.json"
    fixtures = args.fixtures_dir / args.id
    if target.exists() and not args.force:
        sys.exit(f"{target} exists; pass --force to replace it")
    roots = load(args.removed_root, args.removed_root_pem)
    if roots and not args.withdrawn_by:
        sys.exit("--withdrawn-by is required with removed roots")
    certificates = load(args.cert, args.cert_pem)
    sources = []
    for item in args.source:
        title, separator, url = item.partition("=")
        if not separator:
            sys.exit(f"--source {item!r}: expected TITLE=URL")
        sources.append({"title": title, "url": url})
    document = {
        "schema_version": "sanka-trust-advisory/v1",
        "id": args.id,
        "title": args.title,
        "url": args.url,
        "published": args.published,
        "sources": sources,
        "trust_label": args.trust_label,
        "ca_names": args.ca_name,
        "verification_risk": args.verification_risk,
        "certificates": [
            {
                "name": cert.subject_cn,
                "issuer": cert.issuer_cn,
                "sha256": cert.sha256,
                "spki_sha256": cert.spki_sha256,
            }
            for cert, _ in certificates
        ],
        "removed_roots": [
            {"subject": cert.subject_cn, "withdrawn_by": args.withdrawn_by} for cert, _ in roots
        ],
        "fixes": {},
        "pending": {},
    }
    parse_advisory(document, target.name)  # Fail before writing anything.
    fixtures.mkdir(parents=True, exist_ok=True)
    for cert, pem in [*certificates, *roots]:
        (fixtures / f"{cert.sha256}.pem").write_bytes(pem)
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {target} and {len(certificates) + len(roots)} fixtures in {fixtures}.")
    print("Next: add fixes and pending ecosystems from vendor advisories, then run the tests.")


if __name__ == "__main__":
    main()
