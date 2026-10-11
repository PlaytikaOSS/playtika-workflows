# PlaytikaOSS release workflows

This repository provides a reusable Maven Central release workflow. Repositories
call one pinned workflow; release policy, signing and publication are maintained
here. GitHub's official checkout, Java setup, artifact and provenance actions
handle the CI boundary. The official [JReleaser action](https://github.com/jreleaser/release-action)
handles PGP signatures, checksums, Central bundles, upload and publication status.
Actions are pinned to full commit SHAs and JReleaser is pinned to version 1.26.0.

## Repository caller

Add this caller as `.github/workflows/release.yml`. Replace both occurrences of
`REVIEWED_WORKFLOW_SHA` with the **same full commit SHA** from this repository:

```yaml
name: Publish to Maven Central

on:
  release:
    types: [published]

permissions: {}

jobs:
  release:
    uses: PlaytikaOSS/playtika-workflows/.github/workflows/maven-central-release.yml@REVIEWED_WORKFLOW_SHA
    permissions:
      contents: read
      actions: read
      id-token: write
      attestations: write
    with:
      workflow-sha: REVIEWED_WORKFLOW_SHA
      group-id: com.playtika.reactivefeign
      release-branch: develop
      java-version: '21'
```

The `workflow-sha` input binds provenance verification to the shared workflow
revision. It is separate from the caller's release commit. Do not use mutable
branches or tags for `uses`, and do not add `secrets: inherit`.

| Input | Default | Purpose |
| --- | --- | --- |
| `workflow-sha` | Required | Full SHA matching the caller's `uses` pin |
| `group-id` | Required | Maven group shared by all staged modules, under `com.playtika` |
| `release-branch` | `develop` | Protected branch containing approved release commits |
| `java-version` | `21` | JDK for the Maven build |
| `skip-tests` | `false` | Set to `true` only when required CI verifies tests separately |

For example, another caller can set `group-id: com.playtika.testcontainers` and
`release-branch: main`. Projects must have a Maven wrapper and generate their
publication artifacts during `install`. Release tags are stable numeric versions,
such as `5.0.0`; manual publication, snapshots, drafts and prereleases are rejected.
The build updates POM versions locally to the release tag. It does not push source
changes or require a GitHub App write token.

## Onboard each caller repository

1. Allow calls to this repository's reusable workflows in GitHub Actions settings.
   If this repository is private, configure its workflow access for the permitted
   PlaytikaOSS repositories. Allow the pinned GitHub and JReleaser actions.
2. Set the repository or organization Actions variable `RELEASE_ACTORS` to a
   comma-separated allowlist of logins permitted to author, publish and rerun
   releases. Include an approved bot if it authors release drafts. Every one of
   those actors must be allowed; an empty configuration rejects publication.
3. Create the caller repository's `maven-central` GitHub Environment. Require
   reviewers, enable **Prevent self-review**, disable administrator bypass, and
   configure a tag-only deployment policy named `[0-9]*.[0-9]*.[0-9]*`.
   The workflow checks that reviewers, self-review prevention and this tag policy
   exist. Administrator bypass is an external setting not exposed by that API.
4. Put these secrets in the **caller repository's Environment**:

   | Secret | Value |
   | --- | --- |
   | `MAVEN_CENTRAL_USERNAME` | Central Portal user-token username |
   | `MAVEN_CENTRAL_TOKEN` | Central Portal user-token password/token |
   | `GPG_PUBLIC_KEY` | ASCII-armored public signing key |
   | `GPG_PRIVATE_KEY` | ASCII-armored secret signing key |
   | `GPG_PASSPHRASE` | Signing-key passphrase |

   Remove repository-level duplicates and exclude the caller from organization
   secrets with the same names. Reusable workflows cannot forward Environment
   secrets through `workflow_call`; the publisher declares the Environment in the
   caller repository and receives its secrets there.
5. Protect the selected release branch with reviews and checks; protect version
   tags against update and deletion. Require maintainer review for changes to the
   caller, this shared workflow and its action pins. Environment reviewers must
   inspect the validated release SHA and the pinned shared workflow before approval.

## Release boundary

The workflow validates the event, actor allowlist and protected Environment,
resolves lightweight or annotated release tags, and requires the tag, event and
caller workflow to identify the same immutable commit on the protected branch.
The build checks out that full SHA with `persist-credentials: false` and stages
unsigned Maven artifacts in a local file repository without publication secrets.

An isolated job attests the unsigned bundle digest. After Environment approval,
the publisher repeats authorization and tag validation, downloads the exact
artifact ID, checks its SHA-256 digest and verifies GitHub provenance against the
caller source SHA/tag and shared workflow SHA. The publisher has no caller source
checkout, shared Maven cache or Maven build. Archive validation accepts only
regular POM/JAR files for the configured group and release version.

The publisher generates a fixed JReleaser configuration. Caller-provided release
configuration, hooks, POM plugins, wrappers and JARs are never executed there.
JReleaser signs and verifies signatures, generates checksums, validates required
artifacts and deploys through the Central Publisher API. Its Maven-backed
PomChecker is explicitly disabled in this job; Central validates POM metadata
on the server. Deploy-only invocation skips GitHub tag/release creation.

## Maintain and test

Dependabot proposes updates to action pins in this repository. Review and test
them here, then roll out the reviewed shared workflow SHA to caller repositories;
only the central implementation needs maintenance. A later release can add
support for other build tools or multiple Maven groups without duplicating
publication logic in callers.

```sh
python3 -m pip install -r .github/tests/requirements.txt
python3 -m unittest discover -s .github/tests -v
actionlint .github/workflows/maven-central-release.yml .github/workflows/release-checks.yml
```

CI checks run on pull requests and pushes to `main`. They exercise unauthorized
actors and reruns, tag movement, protected Environment requirements, alternate
PlaytikaOSS repositories, immutable workflow pins, altered bundles, unsafe archive
entries and the build/publication credential boundary.
