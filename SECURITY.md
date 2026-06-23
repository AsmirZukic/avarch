# Security Policy

## Supported Versions

Avarch is currently under active development. Security updates are provided only for the latest published release.

| Version                        | Supported          |
| ------------------------------ | ------------------ |
| Latest release                 | :white_check_mark: |
| Older releases                 | :x:                |
| Development builds from `main` | Best effort        |

Users should upgrade to the latest available version before reporting an issue that may already have been fixed.

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues, discussions, or pull requests.

Use GitHub's private vulnerability reporting feature:

1. Open the **Security** tab of the Avarch repository.
2. Select **Report a vulnerability**.
3. Provide as much relevant information as possible.

A useful report should include:

* A description of the vulnerability
* The affected Avarch version or container image tag
* Steps to reproduce the issue
* The expected and actual behaviour
* The potential security impact
* Any suggested remediation, if known
* Relevant logs, configuration, or proof-of-concept code

Do not include sensitive personal data, credentials, API keys, private media, or access tokens in the report.

## Response Process

After receiving a vulnerability report:

* Receipt will normally be acknowledged within 7 days.
* The report will be reviewed and an initial assessment provided within 14 days.
* Additional information may be requested to reproduce or understand the issue.
* Accepted vulnerabilities will be tracked privately while a fix is developed.
* A security advisory and patched release may be published once a fix is available.
* Reports that cannot be reproduced or are determined not to represent a security issue will be closed with an explanation.

Fix timelines depend on the severity and complexity of the vulnerability. Critical issues affecting supported releases will be prioritised.

## Disclosure

Please allow reasonable time for investigation and remediation before publicly disclosing a vulnerability.

Where appropriate, reporters may be credited in the published security advisory. Reporters may also request to remain anonymous.

## Scope

Examples of issues that are considered in scope include:

* Arbitrary command execution
* Path traversal or access outside configured directories
* Unsafe handling, replacement, or deletion of source media
* Container privilege escalation
* Exposure of secrets or sensitive configuration
* Dependency vulnerabilities that are exploitable through Avarch
* Malicious profile or VapourSynth script handling
* Bypassing validation or workspace safety controls

The following are generally not considered vulnerabilities:

* Findings that only affect unsupported versions
* Vulnerabilities reported only by version number without a demonstrated impact on Avarch
* Issues in third-party software that Avarch does not expose or make exploitable
* Denial of service caused by intentionally processing extremely large or malformed media
* Problems requiring an already-compromised host or unrestricted access to the Docker daemon
* Missing features, configuration questions, or general software bugs without a security impact

## Container Images

Official container images should be pulled only from the locations documented in the Avarch repository.

When reporting an image vulnerability, include:

* The full image name and tag
* The image digest, when available
* The scanner and scanner version
* The vulnerability identifier
* The affected package and installed version
* Whether the vulnerability is marked as fixable
* Any evidence that the vulnerable functionality is reachable through Avarch
