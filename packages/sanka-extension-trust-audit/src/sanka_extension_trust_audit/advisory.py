# SPDX-License-Identifier: Apache-2.0
"""Facts for the eMudhra intermediate CA key compromise (October 2026).

Sources: Mozilla Bugzilla 2083449 (certificate list, root removal), the Mozilla
Security Blog, Cloudflare's customer advisory and the community tracker at
emudhra.we-have-a-problem.com. Every fingerprint below was checked against the
certificate downloaded from crt.sh.
"""

from __future__ import annotations

from dataclasses import dataclass

ADVISORY_ID = "emudhra-2026-10"
ADVISORY_TITLE = "eMudhra intermediate CA key compromise (October 2026)"
ADVISORY_URL = "https://bugzilla.mozilla.org/show_bug.cgi?id=2083449"


@dataclass(frozen=True)
class AffectedCertificate:
    name: str
    sha256: str
    issuer: str


# Six TLS and two code-signing intermediates whose P-384 keys are integers
# below 256, plus the Sectigo cross-certificates for the same keys.
AFFECTED_CERTIFICATES: tuple[AffectedCertificate, ...] = (
    AffectedCertificate(
        "EM EV TLS CA - G3B",
        "e7568fe6e12331f4fd7de61da4c7559d0f8eb192a218d4741a3ba75184333838",
        "emSign Root TLS CA - G3",
    ),
    AffectedCertificate(
        "EM OV TLS CA - G3B",
        "98b896625c0c230adeec3eb2cf6aad5001ecf9156a68644e82374ce5833bc25b",
        "emSign Root TLS CA - G3",
    ),
    AffectedCertificate(
        "EM DV TLS CA - G3B",
        "42c4accd12c4f59597925e1a08a7a90102608543809308d50ec427e118ba1493",
        "emSign Root TLS CA - G3",
    ),
    AffectedCertificate(
        "EM EV TLS CA - G3C",
        "432ec25f0d28de9006d97bf752f17bbb2cd41f669933fcb072d9a1148d0261f3",
        "emSign Root TLS CA - G3",
    ),
    AffectedCertificate(
        "EM OV TLS CA - G3C",
        "cb6e15165ac802e9b3f993f16c25d41f4e81c21ebde71ae1b012ac00bf9c7c3f",
        "emSign Root TLS CA - G3",
    ),
    AffectedCertificate(
        "EM DV TLS CA - G3C",
        "0164d7ceae5bb909c272af151ff44b1177ae9b47d9424b94f1d9558c30934d19",
        "emSign Root TLS CA - G3",
    ),
    AffectedCertificate(
        "EM OV CS CA - G3B",
        "9e39de2881273acc225a149c72ba3e0f51424963669ab633ee1dc7df5db067b4",
        "emSign Root CS CA - G3",
    ),
    AffectedCertificate(
        "EM OV CS CA - G3C",
        "4b2460a466f95fd40113bcdcca2ceb29b3b093921de45474da7df48874c630b1",
        "emSign Root CS CA - G3",
    ),
    AffectedCertificate(
        "EM EV TLS CA - G3B",
        "579480aea166290bd3c643589e795fc0d0e54e86505d180f1dff99efd3199512",
        "AAA Certificate Services",
    ),
    AffectedCertificate(
        "EM OV TLS CA - G3B",
        "5f1ded4dd590af74631290cf073b122a2aac6a1fa479778a0bd43579bd832699",
        "AAA Certificate Services",
    ),
    AffectedCertificate(
        "EM DV TLS CA - G3B",
        "45a883f349aedfd5f3a649b5498ffb121712ba8b448f3f2a91dbc5a766fcddbe",
        "AAA Certificate Services",
    ),
    AffectedCertificate(
        "EM EV TLS CA - G3C",
        "fd9d5c2dbbd4995ab69b8d50c439d9527b00529199c2fd01b2174eb58f07021f",
        "AAA Certificate Services",
    ),
    AffectedCertificate(
        "EM OV TLS CA - G3C",
        "7485b51c0ed192878fa1d2f948fb529ae1842cc5086c4b67f7be5b2039de9489",
        "AAA Certificate Services",
    ),
    AffectedCertificate(
        "EM DV TLS CA - G3C",
        "62b7f9868263f72f6911fc78ceaf72c1979158b7368bf648683bb2aa39b02e72",
        "AAA Certificate Services",
    ),
    AffectedCertificate(
        "EM OV CS CA - G3B",
        "a496e23ac0cab8155fc9b117b97eae368efcbb9041aec8804d5a308d2d01c2d8",
        "AAA Certificate Services",
    ),
    AffectedCertificate(
        "EM OV CS CA - G3C",
        "84112972b8e59d14560ae7e7119654474aad119990cf3722bfc1e3b91615e1fd",
        "AAA Certificate Services",
    ),
)

# SHA-256 of the DER SubjectPublicKeyInfo, as base64 (HPKP / OkHttp pins) and
# hex. The cross-certificates share these eight keys.
COMPROMISED_SPKI: dict[str, str] = {
    "4CVMf83CZeLw+KQo2rt1q0rgObSHLWL17lfwh31aikE=": "EM EV TLS CA - G3B",
    "Q7/6GdbvZRRR9oQeAZkx6W9vLq7/KKtLbxzkV23icXs=": "EM OV TLS CA - G3B",
    "6+1kdcLC2A/LGZ8/6N1uazzmC3frFdTkLD3sDG9bUQU=": "EM DV TLS CA - G3B",
    "XAZgorFkkQ2P9YnBjhRVLp4Owjfc7+LLc21BpIEfgaA=": "EM EV TLS CA - G3C",
    "Y+zv7kMdSVI7wRNsTnLQIGpHcIbK9gbNXPAlaGwQnPg=": "EM OV TLS CA - G3C",
    "Sd01PcK5GaN3BkURfQ5gwWEV59G8JZ7v5zSwDqQ0/l8=": "EM DV TLS CA - G3C",
    "7E/6GtCsiAgTFLeq/+9qkfl81XKRu1umhsVBX1609rY=": "EM OV CS CA - G3B",
    "ksBj1n6PFNtx0ojhEmb/2vacvxFfe6sU87wvaNJeDjA=": "EM OV CS CA - G3C",
}
COMPROMISED_SPKI_HEX: dict[str, str] = {
    "e0254c7fcdc265e2f0f8a428dabb75ab4ae039b4872d62f5ee57f0877d5a8a41": "EM EV TLS CA - G3B",
    "43bffa19d6ef651451f6841e019931e96f6f2eaeff28ab4b6f1ce4576de2717b": "EM OV TLS CA - G3B",
    "ebed6475c2c2d80fcb199f3fe8dd6e6b3ce60b77eb15d4e42c3dec0c6f5b5105": "EM DV TLS CA - G3B",
    "5c0660a2b164910d8ff589c18e14552e9e0ec237dcefe2cb736d41a4811f81a0": "EM EV TLS CA - G3C",
    "63ecefee431d49523bc1136c4e72d0206a477086caf606cd5cf025686c109cf8": "EM OV TLS CA - G3C",
    "49dd353dc2b919a3770645117d0e60c16115e7d1bc259eefe734b00ea434fe5f": "EM DV TLS CA - G3C",
    "ec4ffa1ad0ac88081314b7aaffef6a91f97cd57291bb5ba686c5415f5eb4f6b6": "EM OV CS CA - G3B",
    "92c063d67e8f14db71d288e11266ffdaf69cbf115f7bab14f3bc2f68d25e0e30": "EM OV CS CA - G3C",
}

# Mozilla and Chrome withdrew trust in this root (Bugzilla 2083449 comments 18
# and 20). emSign Root CA - G1, - C1 and ECC Root CA - C3 stay trusted; certifi
# 2026.10.19 still ships them.
REMOVED_ROOTS: frozenset[str] = frozenset({"emSign ECC Root CA - G3"})
# Issuers of the affected intermediates. Never in public root stores on their
# own, but a bundle that carries them extends trust to the compromised keys.
AFFECTED_PARENTS: frozenset[str] = frozenset({"emSign Root TLS CA - G3", "emSign Root CS CA - G3"})
# Sectigo's cross-certificates chain to this root. Mozilla and Chrome dropped
# it earlier; Microsoft, Apple and some JDK distributions still trust it.
CROSS_SIGN_ROOT = "AAA Certificate Services"


# Earliest safe release per maintained line. certifi 2026.10.18 already lacks
# the root; 2026.10.19 is the current release, so patches target it.
CERTIFI_SAFE = "2026.10.18"
CERTIFI_TARGET = "2026.10.19"
WEBPKI_SAFE = "1.0.10"
COMPOSER_CA_BUNDLE_SAFE = {(1, 4): "1.4.16"}
COMPOSER_CA_BUNDLE_TARGET = "1.6.0"
NODE_FIXED = {22: "22.23.4", 24: "24.22.1", 26: "26.12.1"}
GO_FIXED = {(1, 26): "1.26.10", (1, 27): "1.27.3"}
# Release lines after these include the fix.
NODE_LAST_AFFECTED_MAJOR = 26
GO_LAST_AFFECTED_MINOR = (1, 27)

PENDING = {
    "bun": "Bun has not shipped a fix yet.",
    "erlang-certifi": "The Erlang/Elixir certifi package has not shipped a fix yet.",
    "java": "No JDK release removes the root yet. Temurin 21.0.12.1 still trusts "
    "emSign ECC Root CA - G3.",
}
# keytool alias used by Temurin and other OpenJDK builds that generate cacerts
# from Mozilla's store.
JAVA_ROOT_ALIAS = (
    "cn_emsign_ecc_root_ca__g3,o_emudhra_technologies_limited,ou_emsign_pki,c_in [jdk]"
)
