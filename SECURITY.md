# Security Policy

This is the default security policy for repositories under this account
that don't define their own. If a repository has its own `SECURITY.md`,
that file applies instead of this one.

## Reporting a vulnerability

Please report security issues privately using
[GitHub Security Advisories](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability):
open the "Report a vulnerability" form under the repository's Security tab.
Don't open a public issue for a suspected vulnerability.

These are one-person, spare-time projects. Expect an acknowledgment within
a week, not immediately. If you've genuinely had no response after two
weeks, it's fine to open a public issue saying only that you're waiting on
a security report — no vulnerability details in that issue.

## Supported versions

The latest release is supported — on npm, where a package is published
there, otherwise the tip of `main`. There are no maintained older or
long-term-support branches.

## Scope

In scope: anything that lets untrusted input reach code execution, corrupt
output silently, or leak data it shouldn't.

Out of scope: ordinary correctness bugs (wrong output on well-formed
input) — please file those as regular public issues, not security reports.

For the COLREGS family of repositories specifically: none of this software
is fit for use in real navigation, and a correctness bug in collision-
avoidance logic is a correctness bug, not a security report — file it as a
public issue.
