# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Bounded signed bearer policy; JWT runtime belongs to generated applications."""

from typing import Any


def go_access_principal(scope_check: str) -> str:
    """Expose verified claims to handlers using the existing signed-token policy."""
    import re

    authenticate = (
        GO_ACCESS.replace("SCOPE_CHECK", scope_check)
        .replace("func accessStatus(", "func accessPrincipal(")
        .replace("path string) int {", "path string) (map[string]string, int) {")
    )
    authenticate = re.sub(r"return (\d+)", r"return nil, \1", authenticate)
    return authenticate.replace(
        "return nil, 0",
        'return map[string]string{"sub":claims["sub"].(string), "tenant":claims["tenant"].(string), "role":claims["role"].(string)}, 0',
    )


ACCESS_BODY = r"""token = request.headers.get("Authorization", "").strip(" \t")
secret = environ.get("AUTH_JWT_SECRET", "")
issuer = environ.get("AUTH_JWT_ISSUER", "")
audience = environ.get("AUTH_JWT_AUDIENCE", "")
if not 32 <= len(secret) <= 256 or not secret.isascii() or not issuer or not audience or not issuer.isascii() or not audience.isascii():
    UNAVAILABLE
if len(token) > 8192 or fullmatch(r"Bearer [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token) is None:
    UNAUTHENTICATED
try:
    if get_unverified_header(token[7:]) != {"alg": "HS256", "typ": "JWT"}:
        raise InvalidTokenError()
    claims = decode(token[7:], secret, algorithms=["HS256"], issuer=issuer, audience=audience, options={"require": ["exp", "sub", "tenant", "role", "iss", "aud"], "strict_aud": True, "verify_exp": False, "verify_iat": False, "verify_nbf": False})
    if set(claims) - {"exp", "iat", "nbf", "sub", "tenant", "role", "iss", "aud"}:
        raise InvalidTokenError()
    for name in ("sub", "tenant", "role"):
        if type(claims[name]) is not str or fullmatch(r"[A-Za-z0-9_-]{1,128}", claims[name]) is None:
            raise InvalidTokenError()
    for name in ("exp", "iat", "nbf"):
        if name in claims and (type(claims[name]) is not int or not 0 <= claims[name] <= 9007199254740991):
            raise InvalidTokenError()
    now = int(time())
    if claims["exp"] <= now or claims.get("iat", 0) > now or claims.get("nbf", 0) > now:
        raise InvalidTokenError()
except InvalidTokenError:
    UNAUTHENTICATED
if claims["role"] not in ("reader", "writer") or (request.method not in ("GET", "HEAD", "OPTIONS") and claims["role"] != "writer"):
    FORBIDDEN
"""

GO_ACCESS = r"""
var bearerJWT = regexp.MustCompile(`^Bearer [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$`)
var identityClaim = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
func asciiCredential(value string) bool {
    for i := 0; i < len(value); i++ { if value[i] > 127 { return false } }
    return true
}
func accessStatus(token, method, path string) int {
    SCOPE_CHECK
    token = strings.Trim(token, " \t")
    secret, issuer, audience := os.Getenv("AUTH_JWT_SECRET"), os.Getenv("AUTH_JWT_ISSUER"), os.Getenv("AUTH_JWT_AUDIENCE")
    if len(secret) < 32 || len(secret) > 256 || !asciiCredential(secret) || issuer == "" || audience == "" || !asciiCredential(issuer) || !asciiCredential(audience) { return 503 }
    if len(token) > 8192 || !bearerJWT.MatchString(token) { return 401 }
    // WithJSONNumber decodes one value; reject trailing JSON and invalid UTF-8 first.
    for _, segment := range strings.Split(token[7:], ".")[:2] {
        data, err := base64.RawURLEncoding.Strict().DecodeString(segment)
        if err != nil || !utf8.Valid(data) || !json.Valid(data) { return 401 }
    }
    claims := jwt.MapClaims{}
    parsed, err := jwt.ParseWithClaims(token[7:], claims, func(t *jwt.Token) (any,error) { return []byte(secret),nil }, jwt.WithValidMethods([]string{"HS256"}), jwt.WithJSONNumber(), jwt.WithStrictDecoding(), jwt.WithoutClaimsValidation())
    if err != nil || !parsed.Valid || len(parsed.Header) != 2 || parsed.Header["alg"] != "HS256" || parsed.Header["typ"] != "JWT" { return 401 }
    // Signature verification is delegated to jwt; the strict source claim contract follows.
    for key := range claims {
        switch key { case "exp", "iat", "nbf", "sub", "tenant", "role", "iss", "aud": default: return 401 }
    }
    if claims["iss"] != issuer || claims["aud"] != audience { return 401 }
    for _, key := range []string{"sub", "tenant", "role"} {
        value, ok := claims[key].(string)
        if !ok || !identityClaim.MatchString(value) { return 401 }
    }
    now := time.Now().Unix()
    for _, key := range []string{"exp", "iat", "nbf"} {
        raw, exists := claims[key]
        if !exists { if key == "exp" { return 401 }; continue }
        number, ok := raw.(json.Number); if !ok { return 401 }
        value, err := strconv.ParseInt(string(number), 10, 64)
        if err != nil || value < 0 || value > 9007199254740991 { return 401 }
        if key == "exp" && value <= now || key != "exp" && value > now { return 401 }
    }
    role := claims["role"].(string)
    if role != "reader" && role != "writer" || method != "GET" && method != "HEAD" && method != "OPTIONS" && role != "writer" { return 403 }
    return 0
}
"""

# Synthetic replay-only key and claims. Never emitted in application configuration.
REPLAY_JWT_ENV = {
    "AUTH_JWT_SECRET": "sanka-isolated-replay-signing-key-32-bytes",
    "AUTH_JWT_ISSUER": "sanka-replay",
    "AUTH_JWT_AUDIENCE": "sanka-replay-backend",
}


def replay_token(
    claims: dict[str, Any],
    *,
    key: str | None = None,
    header: dict[str, Any] | None = None,
    payload_suffix: bytes = b"",
) -> str:
    import base64
    import hashlib
    import hmac
    import json

    def encoded(value: Any) -> bytes:
        return base64.urlsafe_b64encode(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=")

    signing = (
        encoded(header if header is not None else {"alg": "HS256", "typ": "JWT"})
        + b"."
        + base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode() + payload_suffix
        ).rstrip(b"=")
    )
    signature = hmac.new(
        (key or REPLAY_JWT_ENV["AUTH_JWT_SECRET"]).encode(), signing, hashlib.sha256
    ).digest()
    return "Bearer " + (signing + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def replay_roles(case: dict[str, Any], *, first: bool) -> tuple[tuple[str, str, int], ...]:
    claims: dict[str, Any] = {
        "iss": REPLAY_JWT_ENV["AUTH_JWT_ISSUER"],
        "aud": REPLAY_JWT_ENV["AUTH_JWT_AUDIENCE"],
        "sub": "fixture-user",
        "tenant": "fixture-tenant",
        "role": "writer",
        "exp": 4102444800,
    }
    roles = [
        ("writer", replay_token(claims), case["expected_status"]),
        ("missing", "", 401),
        ("invalid", "Bearer invalid-replay-token", 401),
        (
            "reader",
            replay_token(claims | {"role": "reader"}),
            case["expected_status"] if case["method"] in ("GET", "HEAD", "OPTIONS") else 403,
        ),
        ("forbidden-role", replay_token(claims | {"role": "guest"}), 403),
    ]
    if case["method"] in ("GET", "HEAD", "OPTIONS"):
        roles.append(
            (
                "other-identity",
                replay_token(claims | {"sub": "other-user", "tenant": "other-tenant"}),
                case["expected_status"],
            )
        )
        roles.append(("original-identity", replay_token(claims), case["expected_status"]))
    if first:
        token = replay_token(claims)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        noncanonical = token[:-1] + alphabet[alphabet.index(token[-1]) | 1]
        roles.append(("noncanonical-signature", noncanonical, 401))
        roles.append(("trailing-json", replay_token(claims, payload_suffix=b" {}"), 401))
        for name, changed in (
            ("expired", {"exp": 1}),
            ("future-nbf", {"nbf": 4102444800}),
            ("future-iat", {"iat": 4102444800}),
            ("issuer", {"iss": "other"}),
            ("audience", {"aud": "other"}),
            ("audience-list", {"aud": [claims["aud"]]}),
            ("numeric-subject", {"sub": 1}),
            ("empty-tenant", {"tenant": ""}),
            ("boolean-exp", {"exp": True}),
            ("float-exp", {"exp": 4102444800.0}),
            ("string-exp", {"exp": "4102444800"}),
            ("overflow-exp", {"exp": 9007199254740992}),
            ("extra-claim", {"admin": True}),
            ("role-list", {"role": ["writer"]}),
        ):
            roles.append((name, replay_token(claims | changed), 401))
        for name in ("exp", "sub", "tenant", "role", "iss", "aud"):
            roles.append(
                (
                    "missing-" + name,
                    replay_token({k: v for k, v in claims.items() if k != name}),
                    401,
                )
            )
        roles.extend(
            [
                (
                    "wrong-key",
                    replay_token(claims, key="another-isolated-signing-key-32-bytes"),
                    401,
                ),
                ("none-algorithm", replay_token(claims, header={"alg": "none", "typ": "JWT"}), 401),
                (
                    "algorithm-confusion",
                    replay_token(claims, header={"alg": "HS512", "typ": "JWT"}),
                    401,
                ),
                (
                    "key-id",
                    replay_token(claims, header={"alg": "HS256", "typ": "JWT", "kid": "other"}),
                    401,
                ),
            ]
        )
    return tuple(roles)
