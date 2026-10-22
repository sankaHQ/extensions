# SPDX-License-Identifier: Apache-2.0
"""Dependency-free X.509 reading for PEM bundles and binary keystores.

Only the fields the audit needs are decoded: subject and issuer names, the
certificate fingerprint and the SubjectPublicKeyInfo digest. Signatures are not
verified; the audit matches known certificates and keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, replace

_PEM = re.compile(
    rb"-----BEGIN (?:TRUSTED |X509 )?CERTIFICATE-----\s*(.*?)\s*"
    rb"-----END (?:TRUSTED |X509 )?CERTIFICATE-----",
    re.DOTALL,
)
_CN = bytes.fromhex("550403")
_O = bytes.fromhex("55040a")
_STRING_TAGS = {0x0C: "utf-8", 0x13: "ascii", 0x16: "ascii", 0x14: "latin-1", 0x1E: "utf-16-be"}


@dataclass(frozen=True)
class Certificate:
    sha256: str
    spki_sha256: str
    spki_sha256_b64: str
    subject_cn: str
    subject_o: str
    issuer_cn: str
    issuer_o: str
    # 1-based line span inside a PEM file; None for binary containers.
    lines: tuple[int, int] | None = None

    @property
    def self_issued(self) -> bool:
        return (self.subject_cn, self.subject_o) == (self.issuer_cn, self.issuer_o)


class _DerError(ValueError):
    pass


def _tlv(data: bytes, pos: int, end: int) -> tuple[int, int, int]:
    """Return (tag, content start, content end) for the element at pos."""
    if pos + 2 > end:
        raise _DerError("truncated")
    tag = data[pos]
    first = data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 4 or pos + count > end:
            raise _DerError("bad length")
        length = int.from_bytes(data[pos : pos + count], "big")
        pos += count
    if pos + length > end:
        raise _DerError("overflow")
    return tag, pos, pos + length


def _name(data: bytes, start: int, end: int) -> tuple[str, str]:
    values: dict[bytes, str] = {}
    pos = start
    while pos < end:
        tag, set_start, set_end = _tlv(data, pos, end)
        if tag != 0x31:
            raise _DerError("name")
        inner = set_start
        while inner < set_end:
            _, atv_start, atv_end = _tlv(data, inner, set_end)
            oid_tag, oid_start, oid_end = _tlv(data, atv_start, atv_end)
            value_tag, value_start, value_end = _tlv(data, oid_end, atv_end)
            oid = data[oid_start:oid_end]
            encoding = _STRING_TAGS.get(value_tag)
            if oid_tag == 0x06 and oid in (_CN, _O) and encoding and oid not in values:
                values[oid] = data[value_start:value_end].decode(encoding, "replace")
            inner = atv_end
        pos = set_end
    return values.get(_CN, ""), values.get(_O, "")


def parse_certificate(data: bytes, pos: int = 0) -> tuple[Certificate, int]:
    """Parse one DER certificate at pos; return it and the offset after it."""
    tag, cert_start, cert_end = _tlv(data, pos, len(data))
    if tag != 0x30:
        raise _DerError("certificate")
    tag, tbs_start, tbs_end = _tlv(data, cert_start, cert_end)
    if tag != 0x30:
        raise _DerError("tbs")
    alg_tag, _, alg_end = _tlv(data, tbs_end, cert_end)
    sig_tag, _, sig_end = _tlv(data, alg_end, cert_end)
    if alg_tag != 0x30 or sig_tag != 0x03 or sig_end != cert_end:
        raise _DerError("signature")
    field = tbs_start
    tag, _, field_end = _tlv(data, field, tbs_end)
    if tag == 0xA0:
        field = field_end
    fields = []
    for _ in range(6):
        tag, start, field_end = _tlv(data, field, tbs_end)
        fields.append((tag, field, start, field_end))
        field = field_end
    serial, _, issuer, _, subject, spki = fields
    if serial[0] != 0x02 or any(item[0] != 0x30 for item in fields[1:]):
        raise _DerError("tbs fields")
    issuer_cn, issuer_o = _name(data, issuer[2], issuer[3])
    subject_cn, subject_o = _name(data, subject[2], subject[3])
    spki_digest = hashlib.sha256(data[spki[1] : spki[3]]).digest()
    certificate = Certificate(
        sha256=hashlib.sha256(data[pos:cert_end]).hexdigest(),
        spki_sha256=spki_digest.hex(),
        spki_sha256_b64=base64.b64encode(spki_digest).decode("ascii"),
        subject_cn=subject_cn,
        subject_o=subject_o,
        issuer_cn=issuer_cn,
        issuer_o=issuer_o,
    )
    return certificate, cert_end


def read_pem(text: bytes) -> list[Certificate]:
    found = []
    for match in _PEM.finditer(text):
        try:
            der = base64.b64decode(b"".join(match.group(1).split()), validate=True)
            certificate, _ = parse_certificate(der)
        except (_DerError, binascii.Error, ValueError):
            continue
        first = text.count(b"\n", 0, match.start()) + 1
        last = text.count(b"\n", 0, match.end()) + 1
        found.append(replace(certificate, lines=(first, last)))
    return found


def read_binary(data: bytes) -> list[Certificate]:
    """Find DER certificates embedded in JKS, JCEKS, PKCS#7 or plain PKCS#12."""
    found: list[Certificate] = []
    seen: set[str] = set()
    pos = data.find(b"\x30\x82")
    while pos != -1:
        try:
            certificate, end = parse_certificate(data, pos)
        except _DerError:
            pos = data.find(b"\x30\x82", pos + 1)
            continue
        if certificate.sha256 not in seen:
            seen.add(certificate.sha256)
            found.append(certificate)
        pos = data.find(b"\x30\x82", end)
    return found
