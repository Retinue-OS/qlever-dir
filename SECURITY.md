# Security Policy

## Reporting a vulnerability

Please do not put vulnerability details in a public issue.

1. If the **Security** tab of this repository offers **"Report a vulnerability"**
   (GitHub's private vulnerability reporting), use it.
2. If that option is not available, open a public issue containing only the
   words **"security contact requested"** and nothing else. A maintainer will
   contact you privately with a channel for the details.

## What we consider in scope

`qlever-dir` builds a SPARQL endpoint from untrusted files dropped into a
mounted directory, and its index-build path shells out (`rapper`, `sed`) to
do it. In scope:

- Anything that lets a crafted filename or file content escape the intended
  shell/SPARQL context during indexing (see the open injection-shaped issues:
  [#5](https://github.com/retinue-os/qlever-dir/issues/5),
  [#6](https://github.com/retinue-os/qlever-dir/issues/6),
  [#8](https://github.com/retinue-os/qlever-dir/issues/8) — those are filed,
  triaged, and not what this policy is for; a *new* way to turn one into code
  execution or data exfiltration rather than a build error is).
- Anything that lets the watcher or build process read or write outside the
  mounted data directory.

## Known limitations — please don't report these as vulnerabilities

These are documented, already-filed design gaps, not undiscovered bugs:
[#4](https://github.com/retinue-os/qlever-dir/issues/4) (watcher failure is
silent), [#7](https://github.com/retinue-os/qlever-dir/issues/7) (no
supervision or readiness signal), [#10](https://github.com/retinue-os/qlever-dir/issues/10)
(watcher misses files in directories created after startup). A report telling
us these exist tells us nothing new; a report showing one of them is worse
than described is in scope.
