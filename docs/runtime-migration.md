# Migrating runtimes into independent Skills

A migration is complete when the public package contains the working capability
and its consumers use that package. Moving only `SKILL.md` while leaving the
implementation or required helper libraries in a private repository does not
achieve this. Each machine still needs its own installation and verification;
a merged change does not update every existing caller.

## Separate behavior from private choices

Keep implementation, executable entries, tests, dependency manifests, lockfiles
and supporting references inside `skills/<name>/`. Verify that the package works
when copied without its siblings or the private repository. Integrations use
explicit external commands or installed dependencies, rather than locating
another package's source tree.

| Public package | Caller-owned configuration or policy |
|---|---|
| API clients, pagination, parsing and rendering | Accounts, selected sources, destination paths and exclusions |
| Incremental processing and recovery | State locations, existing checkpoints and pending outputs |
| Worktree and publication mechanisms | Repository, branch, owned paths, writer identity and locks |
| Model invocation and input reuse | Model/provider selection, credential references, prompts and spending limits |
| Installation and link operations | Package selection, schedules, service commands and hook settings |
| General validation mechanisms | Repository-specific content rules and commit metadata |

Credentials and source content stay private. Configuration should point to
credential storage without embedding credential values. A private policy command
may remain when it expresses actual repository rules; it should not retain a
second generic publisher, scheduler or client implementation.

Prefer a validator's command interface over an adapter that imports its source.
The [structure checker](../skills/structure-lint/references/configuration.md)
can invoke configured argument arrays and consume structured findings. The
[translation validator](../skills/prompt-translation/references/configuration.md)
can inspect its configured output directory and emit TSV findings. Configure
their locations and repository-specific selection privately, then delete the
adapter and its duplicated path lookup. A compatibility inspection of historical
output must be explicit; keep strict validation for newly published output.

Remove checks of historical configuration when that configuration no longer
controls execution. Preserve its provenance when needed, and test the current
source manifest or other active authority instead. A historical source list
must never become an implicit access grant during migration.

Review the exact publication set before copying it into a public package.
Use synthetic examples, fixtures and identities. Check error messages, comments,
test metadata and dependency files as well as the main source. Removing a
username from a path does not make deployment history or private source layouts
appropriate for publication.

Before pushing, inspect the commit author and committer metadata as well as the
files. Use the repository's approved public identity and email; a private global
Git setting can otherwise add contact or machine details to an unchanged public
diff. Make identity validation a required successful step before the push.

## Inspect current callers before deleting entries

Inspect the current checkout and the actual installation on each affected
machine: crontab entries, command links and launchers, user services, harness
hooks, installed configuration, and Python or shell imports. Read configuration
without exposing credential contents. Keep the search bounded to executable
and configuration locations; archived conversations are not a call graph.

Classify matches before acting:

- An active command or import needs to point at the public interface.
- An installer's exact old-command match may remain to remove a retired cron
  entry during an upgrade, even after that executable is deleted.
- Tests for a removed forwarding layer can be deleted; tests for private
  selection and public integration still matter.
- Historical documentation or a backup launcher is evidence of an earlier
  installation, not proof that a compatibility entry is still required.

Absence on one machine does not establish absence elsewhere. Give other owners
explicit upgrade instructions and the required public version. Preserve any
compatibility interface required by the affected package's contract; otherwise
delete obsolete forwards after updating callers, instead of maintaining another
permanent dispatch layer.

## Apply only the selected machine's installation

Start with the machine's existing responsibilities. An interactive consumer or
backup source need not become an archive publisher when it installs packages.
Absent schedules, hooks and credentials can be intentional. Preserve that
selection unless the operator requests a new capability; a copied service
template or a successful link installation is not activation authority.

A remote repository rename does not rename its local checkout. If the checkout
location changes, inspect active callers and linked worktrees before moving it,
repair Git worktree metadata, and update the remote URL separately. Check command
symlinks, shell launchers, repository-manager registries and existing scheduler
arguments. Preserve data destinations and package-required compatibility entries;
do not rename backup data merely because executable source moved. Install lasting
links from the canonical source, not the temporary editing worktree. Recheck the
inspected revision if another synchronizer updates that source during migration.

Audit the scheduler native to the host: cron and user services on Linux, and
cron plus user/system launchd definitions and loaded jobs on macOS. Inspect
effective Git hook directories, user and project harness settings, enabled plugin
hooks and launcher-selected alternate configuration roots. A downloaded plugin
catalog is not proof that its hooks are enabled. Classify old-command removal
patterns, session history and saved command approvals separately from executable
callers. Record unreadable locations and search exclusions in private evidence.

Report link resolution, command readiness and active execution separately.
Discovery links do not install runtime dependencies. Provision the selected
package's declared dependencies in a persistent environment and select its
documented interpreter in the actual caller. A temporary test environment does
not make the default installed command ready. A
configuration-only doctor can succeed without credentials, while an absent hook
has nothing to reload. Validate an existing enabled entry through an invocation
after the switch; an earlier log or zero exit from a skipped task does not prove
the new entry ran. Keep machine inventories and results private, as described in
the activation procedure below.

## Prefer a directly consumed private configuration

Before retaining a configuration generator, check whether the public loader can
already read the required settings. Static source selection, prompts, paths and
publication arguments usually belong in one private configuration. Remove the
old copy of a setting when moving it, so two files do not become competing
authorities. Keep an exporter only for necessary behavior the native interface
cannot express.

Read the selected package's path contract. Support for `~`, environment
variables, relative paths and command placeholders differs between packages
and sometimes between fields. A path relative to the configuration file may be
supported while the same text inside an argument array is passed literally.
Some loaders resolve a configuration symlink before interpreting relative paths;
others do not. Do not assume changing the working directory fixes either case.

The existing `profiles` array in
[runtime-install](../skills/runtime-install/references/configuration.md) also
accepts caller-selected configuration files. This synthetic example installs a
package discovery link and a JSON configuration link:

```json
{
  "schema": "runtime-install/v1",
  "kind": "skills",
  "lock": "/example/state/install.lock",
  "packages": {
    "example-tool": {
      "source": "/example/packages/example-tool",
      "required": [
        {"path": "SKILL.md", "kind": "file"},
        {"path": "scripts/run", "kind": "executable"}
      ]
    }
  },
  "destinations": ["/example/client/skills"],
  "profiles": [
    {
      "source": "/example/private/example-tool.settings.json",
      "destination": "/example/config/example-tool/config.json"
    }
  ]
}
```

Replace the example paths with explicit private locations. Installation paths
must be absolute after native resolution. The installer supports `~`, explicit
`${NAME}` references, `${CONFIG_DIR}` resolved through the selected configuration
link, and structured environment/default selections; it does not evaluate shell
expressions. Use the [native configuration reference](../skills/runtime-install/references/configuration.md#native-environment-values-and-structured-jobs)
for the exact syntax, required variables and defaults. Sources must already exist. Preview with
the installed package's entry:

```sh
"$RUNTIME_INSTALL_ROOT/scripts/skills" --config /example/private/install.json --dry-run
```

Set `RUNTIME_INSTALL_ROOT` to the selected package directory. The preview creates
no links or directories. For `profiles`, existing regular files and links to
other sources are preserved. Review a retained custom configuration explicitly;
successful installation does not mean it was replaced by the proposed file.
Discovery links have different ownership rules, documented in the package.
Keep access permissions appropriate for private configuration and its parent
directories; a symlink does not restrict access to its target.

## Compare meaning before switching

Load the old and proposed configurations under the intended runtime environment
and compare their resolved values, not just their text. A useful comparison
covers:

- credential references and read/write scope separation;
- selected source identities, exclusions, ownership and output boundaries;
- state, pending-publication records, reusable generated output and filenames;
- task locks, publication locks, installer locks and the scheduled writer;
- publication arguments, private validators and commit-message commands;
- model/provider choice, prompts, budgets, timeouts and no-work behavior.

Do not silently narrow source selection or grant another machine writer access
while changing implementation. Keep one active writer for each owned output.
A failed source read must follow the explicitly chosen failure policy; treating
an error as an empty successful result can discard data or advance progress.

When changing HOME or repository location, inspect every resolved path and
installed command again. Quote shell paths, including paths containing spaces;
argument arrays avoid shell quoting only when the consumer executes them
directly. Regenerate installation artifacts that intentionally contain absolute
paths. Git worktree metadata, service commands and already-running processes can
still refer to the old location after a directory move.

Move durable state deliberately, with the relevant writers stopped or locked.
Two path spellings of a lock must not create two independent locks. The optional
[runtime-layout migration command](../skills/runtime-layout/SKILL.md) supports
an explicit configured move and read-only preview; it does not discover the
right accounts, sources or services to migrate.

## Verify recovery and activate the actual installation

Run package-owned checks using isolated HOME and configuration, synthetic inputs,
fake external commands and local test repositories. Ensure tests cannot fall
back to real credentials or a real model. Cover failure propagation, incomplete
reads, publication rejection and recovery of already-generated output. Compare
unchanged inputs and outputs where compatibility is required. A dry run or doctor
has only the guarantees documented by that command; some inspection commands
contact the configured service.

For repository writers, verify separately that publication succeeded and that
durable progress advanced only after the required checks. Preserve pending
verification records and persistent generated results until recovery completes.
[repository-publish](../skills/repository-publish/SKILL.md) distinguishes
reproducible temporary transactions from existing persistent worktrees. Do not
delete a pending draft or reset a checkpoint merely to make a retry look clean.

Preview installation against the current machine. For managed cron changes,
retain unrelated jobs, preserve the chosen schedule and configuration selection,
and share the installer's lock with other crontab writers. Narrow old-command
matches enough to preserve another repository or configuration using the same
public executable. Configured pre-install commands may have external effects;
the installer cannot undo those effects when a later step fails.

After authorized installation, read back the actual links, crontab, service
commands and hook configuration. A configuration file on disk does not prove a
running service or harness has loaded it. Use the applicable reload, restart or
hook trust procedure, then observe the new entry in the real invocation path.
Do not equate a process being present, a successful skip, or a doctor result with
successful execution of the intended task.

Keep private rollback evidence tying together code versions, installed commands,
configuration, output and state. Before reverting, check state compatibility
and stop the replaced writer so two versions cannot write concurrently. Record
what was actually checked and which machines remain unverified. Leave changing
deployment inventories and run results in private evidence, rather than turning
this public guide into a deployment log.

## Retiring configuration generators and installed copies

Prefer native configuration accepted by the public program. When removing a
configuration generator, record the previous input format, replacement file,
variable expansion rules and how existing customized files are preserved.
Installation settings can be supplied directly to `runtime-install`; ordered
archive commands and file publication can use `repository-publish --config`.
Keep source authorization and repository-specific validation in private policy.

Check installed artifacts independently of source. A Git hook copied before a
migration can still contain an old checkout path after the repository source has
changed. The public Git hook installer links selected executable entries and
backs up explicitly recognized replacements. Inspect local `core.hooksPath`:
a redundant setting pointing at Git's default hook directory can be removed
once verified; a shared custom directory needs coordinated treatment across
all of its consumers. Do not overwrite a symlink during rollback by copying
through it into the public executable.

For every removed entry, the caller's migration notes should identify:

- The old command and the complete replacement invocation.
- Cron, services, Git/harness hooks, shell links and other repositories referring
  to the entry, including settings outside Git.
- Required configuration changes, source authorization, credentials references,
  durable progress and task/shared lock paths.
- Public/private version ordering, activation checks and the exact rollback
  artifacts to preserve.

Keep machine-specific paths, identities and historical deployment evidence in
the caller's private notes. A source deletion is complete only after the actual
selected installation uses the public entry; other machines must apply their
own configuration and activation steps.

## Keep tests and instructions with their owner

After removing a private implementation, remove tests whose only purpose was
its forwarding wrapper, generated configuration copy, or retired file name.
Public behavior tests belong inside the corresponding package, with synthetic
inputs and package-owned CI. Transfer a missing regression before deleting its
private copy; do not preserve a second implementation just to keep an old test.

The consumer still needs tests that load its real private configuration and
exercise private source selection, permissions, publication rules, and recovery.
Similar public test names are not a substitute when they use synthetic policy.
Group the remaining consumer checks by integration and private policy. Use a
shared package-location helper rather than repeating an installation path in
each test. Update explicit test commands when files move; runtime configuration
paths need not move along with test files.

Private Skill profiles should retain the caller's choices and rules. Reference
the installed public package for its command manual and generic behavior.
Move useful missing instructions into its existing public references, after
removing personal examples. Prefer tests of executable examples and machine-read
template fields over exact sentence matching that prevents ordinary editing.
