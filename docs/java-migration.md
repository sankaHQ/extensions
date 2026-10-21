# Java Spring MVC to Python FastAPI (unpublished candidate)

`sanka/java-to-python` is an experimental local code migration extension with target
`python-fastapi`. It implements Scan, Plan, Apply, Test and Verify. The official
marketplace does not yet distribute these packages. Publishing and broader source
support remain separate milestones.

The shared `sanka-java-migration` package captures Java syntax without depending on
the Sanka runtime, SDK, Python web frameworks or this particular destination. A
future Go generator can consume `sanka.java-capture/v1` directly. The executable
adapter owns Python generation and uses the published SDK a4 and shared HTTP
replay v1. No model API or Sanka account is required.

## Qualified first profile

- Single-module Maven, Spring Boot parent **3.5.16**, Java source level 17 or 21.
  The analyzer and source replay run on a Java 21 JDK.
- Exactly the Web and test starters, and optionally the standard Boot Maven
  plugin. The parent must explicitly disable relative-parent lookup.
- One standard `SpringBootApplication` main, plain `RestController` classes and
  plain records in the same package. Public GET methods return one expression.
- Literal paths, optional class mapping prefix, explicitly bound string path
  variables and string query parameters with literal defaults. Missing and single
  empty query values use the default; repeated values are joined with commas.
- JSON `Map.of`, `List.of`, record construction, strings, booleans, nonnegative
  decimal int/long literals and string concatenation. Null is supported in String
  record fields. Integer JSON values are compared without conversion to float.

Security, persistence, arbitrary service code, fields/initializers, custom DTO
behavior, unresolved imports, external configuration, additional build plugins,
profiles, resources, Gradle and unknown annotations produce blockers. Generation
requires the entire captured application to fit the profile; endpoint selection
is deferred. Global unsupported behavior cannot be hidden by selecting a route.

The source analyzer uses JavaParser **3.28.2**, shipped in the helper wheel with
SHA-256 `b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd`.
It parses ASTs in a subprocess. Scan does not run Maven, annotation processors,
source main methods, Spring, application classes or source tests. Imports resolve
against a conservative allowlist; this is not a general symbol solver. JavaParser
is used under its Apache-2.0 option; its upstream license notices ship with it.

## Review and evidence

Plans bind the complete source inventory, scenario corpus, parser and converter
digests, manifest identity, generated files and target dependency lock. Apply
requires the CLI's reviewed plan plus the matching extension plan hash. It writes
only a new candidate directory under `.sanka`, refusing collisions, stale inputs
and symlink escapes. Source code stays unchanged.

Each source must provide `sanka-verify.json` in the shared
`sanka.http-scenarios/v1` format. Every captured endpoint needs a GET case with
`expected_status: 200`. Custom headers and other methods are not qualified.

Test compiles the actual candidate, runs pytest for every scenario, rejects
skipped/missing native tests, and validates target JSON responses. Verify also
compiles the source and runs a trusted JUnit harness through **Spring MockMvc**,
then compares it with **FastAPI TestClient**. Reports contain ordered observations,
status, media type, exact JSON types/values, source/candidate/scenario digests,
runtime versions and the Maven dependency-cache digest. A changed candidate gets
new evidence; an invalid rerun removes its previous success report.

The source's existing test files are inventoried but are not executed by Verify;
the injected JUnit harness is the source check. Python tests are generated for the
destination. No claim is made about arbitrary source JUnit translation, live TCP
behavior, HEAD/OPTIONS, default errors, arbitrary headers, database effects or
production readiness.

## Local qualification

Use a Java 21 JDK, Python 3.12.13 and uv 0.11.21 on macOS or Linux. Source builds
and candidate tests execute code. `SANKA_JAVA_REPLAY=1` is an explicit opt-in for
trusted code. Disposable directories, a minimal environment and offline Maven
reduce accidental coupling; they **are not a security sandbox**. Use an isolated
machine/container for unfamiliar projects.

From a clean checkout of this repository:

```sh
python3 scripts/fetch_java_parser.py
python3 scripts/fetch_java_maven.py
uv sync --frozen --all-packages
uv venv --python 3.12.13 .java-tools/python
uv pip sync --python .java-tools/python/bin/python --require-hashes \
  packages/sanka-extension-java-to-python/src/sanka_extension_java_to_python/requirements.lock
export SANKA_JAVA_TESTS=1 SANKA_JAVA_REPLAY=1
export SANKA_JAVA_MAVEN="$PWD/.java-tools/apache-maven-3.9.16/bin/mvn"
export SANKA_JAVA_MAVEN_REPO="$PWD/.java-tools/maven-repository"
export SANKA_JAVA_PYTHON="$PWD/.java-tools/python/bin/python"
uv run python scripts/prepare_java_fixture.py
uv run python scripts/build_java_release.py
uv pip install --reinstall --no-index --find-links release/java-converters \
  release/java-converters/sanka_extension_java_to_python-*.whl
uv run --no-sync python -m pytest packages/sanka-extension-java-to-python/tests -q
```

Preparation downloads dependencies and executes only the synthetic fixture. The
subsequent source replay uses offline Maven with explicit empty settings and a
separate dependency cache. The candidate builder requires a clean committed Git
revision, builds a complete wheel closure, verifies bundled parser/runner assets,
and records artifact hashes and provenance. It does not publish anything.

The lifecycle suite checks actual observations for nine requests, an intentionally
altered target response, source preservation, review/hash binding, output
collisions, symlink containment, empty/missing coverage, stale reports and source
blockers. The dedicated Java CI lane installs those actual wheels with published
SDK dependencies before running the suite. General `make check` remains the
repository gate; Java execution is opt-in outside its dedicated lane.

The companion runnable example in `sanka-examples/java/spring-greetings` installs
these candidate wheels through public CLI 0.3.9 using a temporary marketplace.
Generated output runs with Python alone: `python -m uvicorn app:app`.
