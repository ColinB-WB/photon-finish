# Security policy

## Supported version

Security fixes are made for the current release line.

## Report a vulnerability privately

Do not open a public issue for a vulnerability, unsafe file operation, privacy
failure, dependency compromise, or source-data risk.

Contact the repository owner privately through the GitHub account that hosts
this project (e.g. a private message or, if enabled, a
[private security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
on the repository). Include reproduction steps, affected version, impact, and
any safe mitigation you have identified.

Please do not include private astrophotography data unless it is necessary and
you have deliberately chosen to share it.

## Scope

Particularly important guarantees include:

- source Seestar data must remain read-only;
- output and cleanup must stay inside explicitly owned processing locations;
- image data must not be uploaded;
- external-tool paths and arguments must not permit unintended command
  execution; and
- the bundled catalog and other generated data must fail closed on provenance
  or integrity errors.
