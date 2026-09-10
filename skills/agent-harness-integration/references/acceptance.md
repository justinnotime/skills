# Cross-mechanism acceptance

Use synthetic fixtures in each owning package before touching production.
For authorized live checks, use a dedicated conversation and selected sources;
keep content and evidence private. Never call a model or send a peer message
merely because a checker is available.

## One recognizable trace

Create a uniquely identifiable human input in the selected harness and receive
a separately identifiable peer message through its actual adapter. Follow both
through native records, backup, replication, extraction and selected downstream
readers. Assert that the human input becomes a human prompt, while the peer
message remains peer history and is absent from human prompts. A text disclaimer
such as "not operator authorization" does not establish machine authorship.

Use the same trace to confirm full native session identity and exact UTC time.
If the source only provides approximate or unknown times, verify downstream
handling explicitly rather than inventing a timestamp to pass its parser.
Check source and prompt truncation policies separately; a bounded prompt view
is not a lossless backup.

## Required assertions

| Case | Required result |
|---|---|
| Native default and two named roots | Correct state/config paths, preserved cwd and arguments, no accidental credential-source change |
| Existing launcher, alias or function | Preserved or deliberately migrated; installation does not silently shadow it |
| Skill discovery in each selected root | The running harness can discover the intended canonical package, not just a filesystem link |
| Private config under changed XDG | Child commands select the intended shared or isolated configuration |
| Two same-directory conversations | No bus identity theft, cross-conversation presentation or extraction collision |
| Resume and terminal rename | Stable selected identity; restart does not register a different inbox accidentally |
| Named ORC fleet | Registration, tasks, acknowledgment and turn reporting use the same store |
| Native hook event and trust | Supported by the installed version; revoked or changed trust does not count as active |
| Human, peer, hook, child and quoted marker inputs | Correct authorship; human quotations preserved; peer input excluded from human prompts |
| Owner plus older mirror | One stable session; longest compatible history selected; divergence does not silently win |
| Live SQLite and checkpointed snapshot | Correct reader mode, consistent content and untouched source files |
| Failed database backup | Previous good snapshot preserved; nonzero result; no raw DB/WAL copy masquerading as a snapshot |
| Remote copy | Selected file version is available on a receiver; restore/read it separately from index completion |
| Queue larger than one pull | A defined subsequent presentation trigger handles the remainder |
| Crash after pull before acknowledgment | Durable message becomes eligible again according to lease/attempt policy |
| Prompt submission failure | No processed acknowledgment; no loss of queued content |
| Session departure | Obligations handled explicitly; a retired identity's watcher is not restarted |
| Repeated extraction | Identical inputs converge without duplicate output or unnecessary changes |
| Required source unreadable | Failure visible; no deletion attributed to an empty source; no incomplete publication |
| Policy-only role correction | Managed histories/prompts refresh without source mutation; legacy freeze handled explicitly |
| Downstream selection | New history is selected by summaries and human prompts by learning readers |
| Publication interruption | Durable progress advances only after successful publication; recovery retains completed work |
| Scheduler environment | Actual scheduled command works with its minimal PATH/configuration and existing locks |

Not every harness supports every case. Record unsupported capabilities against
the caller's requirements rather than claiming a generic full integration.
Changing a native hook event or delivery adapter requires tests of its executable
behavior, not only source-string assertions. Temporarily remove the central fix
and observe its regression test fail when practical.

## Activation and rollback

Keep the prior code revision, configuration changes and selected scheduler entry
recoverable. Do not run old and new writers against one output store. Pause only
the selected job when a path switch requires it, using the existing scheduler
installation lock; retain concurrent unrelated changes when restoring the job.

Inspect the actual invocation after installation. A source version and a process
version may differ. Report pending restart or native trust approval precisely;
do not manufacture approval records or retire another person's session.

Result evidence should identify the trace, versions, selected sources, commands,
assertions, exit statuses and output locations without publishing private content
or credentials. Current status is queried, not copied into permanent guidance.
