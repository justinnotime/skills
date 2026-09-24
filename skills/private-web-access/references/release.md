# Selected release checks

Keep the candidate unpublished while reviewing it. Export selected independent
files, not another repository's history, operational notes or deployment receipts.
Validate package isolation by copying this package alone into a temporary directory.
Run Go race tests, static checks, the synthetic Chromium WebAuthn scenario, and
`govulncheck` against the pinned build. Record the inspected versions and actual
results in the caller's private evidence; do not call an unavailable scan a pass.

Review authentication/enrollment, origin and cookie boundaries, upstream credential
handling, direct-backend bypass, unsafe methods, response headers including 1xx and
trailers, redirects, stream cancellation and rollback. Add adversarial reproductions
for findings and resolve relevant defects before publication. An independent review
and negative controls improve coverage but are not proof that no flaw can exist.

Run a credential scan and contextual privacy review over the exact staged files
and commit metadata. Use synthetic addresses and random disposable credentials in
tests. Review public claims for unintended disclosure of an operator's service map,
configuration, account relationships or known vulnerabilities. Do not include
private source/project names, migration narratives or live validation output.

Validate SKILL.md with the Skill validator. Public CI uses only a standalone package,
read-only repository permission and synthetic tests. Inspect actual CI results after
publishing. Reconcile installed artifacts with the reviewed source and retain a
private rollback package when updating an existing deployment.
