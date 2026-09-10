import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

from .issue_refs import DEFAULT_REPO, canonical, sort_refs, split_ref


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


PROJ_RE = re.compile(r"(?:sources|knowledge)/projects?/([^/]+)/")
WIKI_PROJ_RE = re.compile(r"knowledge/projects/([^/]+)/")
NUM_RE = re.compile("#([1-9]\\d*)")
GH_URL_RE = re.compile(
    "https?://github\\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([1-9]\\d*)", re.I
)
FULL_REF_RE = re.compile("(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9]\\d*)")
SHORT_REF_RE = re.compile("(?<![A-Za-z0-9_.\\-/])([A-Za-z0-9_.-]+)#([1-9]\\d*)")
STATE_RE = re.compile(
    "\\b(MERGED|merged|closed|CLOSED|ready-to-merge|landed|reverted|retracted|superseded|open|OPEN)\\b"
)
DATE_PREFIX_RE = re.compile("(\\d{4}-\\d{2}-\\d{2})")


def classify(subject):
    s = subject.lower()
    if s.startswith("sync:"):
        return "sync"
    if s.startswith("auto-extract"):
        return "auto-extract"
    for pattern, label in OPTIONS.get("commit_kind_patterns", []):
        if re.search(pattern, s):
            return label
    return "content"


OPTIONS = {}
ISSUE_DIRECTORY = "sources/issues"
DOCUMENT_DIRECTORY = "sources/documents"
WIKI_PROJECT_DIRECTORY = "knowledge/projects"
SUMMARY_DIRECTORY = "summaries"
COMMIT_DIRECTORIES = ["sources", "knowledge"]
SESSION_SOURCES = []
PROJECT_PATTERNS = []
SOURCE_PROJECT_LABELS = []


def configure(options):
    global OPTIONS, ISSUE_DIRECTORY, DOCUMENT_DIRECTORY, WIKI_PROJECT_DIRECTORY
    global SUMMARY_DIRECTORY, COMMIT_DIRECTORIES, SESSION_SOURCES, PROJECT_PATTERNS
    global SOURCE_PROJECT_LABELS, MIRROR_PATH_RE
    OPTIONS = options
    ISSUE_DIRECTORY = options["issue_directory"]
    DOCUMENT_DIRECTORY = options["document_directory"]
    WIKI_PROJECT_DIRECTORY = options["wiki_project_directory"]
    SUMMARY_DIRECTORY = options["summary_directory"]
    COMMIT_DIRECTORIES = options["commit_directories"]
    SESSION_SOURCES = options["session_sources"]
    PROJECT_PATTERNS = [re.compile(p) for p in options.get("project_patterns", [])]
    SOURCE_PROJECT_LABELS = options.get("source_project_labels", [])
    MIRROR_PATH_RE = re.compile(re.escape(ISSUE_DIRECTORY) + r"/[^/]+/[1-9]\d*\.md")


def project_of(path):
    for pattern in PROJECT_PATTERNS:
        match = pattern.search(path)
        if match:
            return match.group(1)
    for prefix, label in SOURCE_PROJECT_LABELS:
        if path.startswith(prefix):
            return label
    return None


def semantic_date(path, fallback):
    base = os.path.basename(path)
    m = DATE_PREFIX_RE.search(base)
    return m.group(1) if m else fallback


def frontmatter(text):
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    return text[4:end] if end >= 0 else ""


def fm_scalar(head, field):
    m = re.search(f"^{re.escape(field)}:\\s*(.*?)\\s*$", head, re.M)
    return m.group(1).strip().strip("'\"") if m else ""


def fm_list(head, field):
    lines = head.splitlines()
    for i, line in enumerate(lines):
        if re.fullmatch(f"{re.escape(field)}:\\s*", line):
            values = []
            for item in lines[i + 1 :]:
                m = re.match("^\\s*-\\s*(.*?)\\s*$", item)
                if not m:
                    break
                values.append(m.group(1).strip().strip("'\""))
            return values
    return []


def source_activity_on(text, T):
    activity = set()
    head = frontmatter(text)
    for field in ("created", "closed", "merged"):
        value = fm_scalar(head, field)
        if re.search(f"(?<!\\d){re.escape(T)}T\\d{{2}}:\\d{{2}}:\\d{{2}}Z", value):
            activity.add(f"frontmatter:{field}")
    for line in text.splitlines():
        if line.startswith("## Body "):
            kind = "body"
        elif line.startswith("### @"):
            kind = "comment"
        else:
            continue
        dates = re.findall("(\\d{4}-\\d{2}-\\d{2})T\\d{2}:\\d{2}:\\d{2}Z", line)
        for i, date in enumerate(dates):
            if date == T:
                activity.add(f"{kind}:{('edited' if i else 'created')}")
    return sorted(activity)


def mirror_identity(path, text):
    head = frontmatter(text)
    gh_repo = fm_scalar(head, "repo")
    number = fm_scalar(head, "number") or os.path.splitext(os.path.basename(path))[0]
    if not gh_repo:
        dirname = os.path.basename(os.path.dirname(path))
        if "_" in dirname:
            owner, name = dirname.split("_", 1)
            gh_repo = f"{owner}/{name}"
    try:
        ref = canonical(gh_repo, number)
    except ValueError:
        return None
    return (ref, *split_ref(ref))


def mirror_meta(repo, path, text, activity, activity_source):
    ident = mirror_identity(path, text)
    if ident is None:
        return None
    ref, gh_repo, number = ident
    head = frontmatter(text)
    typ = fm_scalar(head, "type")
    url = fm_scalar(head, "url")
    created_at = fm_scalar(head, "created")
    title = fm_scalar(head, "title")[:90]
    is_pr = "pull" in typ or "/pull/" in url
    relpath = os.path.relpath(path, repo)
    return (
        ref,
        {
            "identity": ref,
            "repo": gh_repo,
            "number": number,
            "title": title,
            "is_pr": is_pr,
            "url": url,
            "created_at": created_at,
            "file": relpath,
            "activity_on_target": activity,
            "activity_source": activity_source,
        },
    )


MIRROR_PATH_RE = re.compile(r"sources/issues/[^/]+/[1-9]\d*\.md")


def git_blob(repo, revision, path):
    try:
        return sh(["git", "show", f"{revision}:{path}"], repo)
    except subprocess.CalledProcessError:
        return None


def historical_source_activity(repo, T, skip_paths=()):
    skipped = set(skip_paths)
    raw = sh(
        [
            "git",
            "log",
            "--no-merges",
            "--no-renames",
            "--format=@@@%H",
            "--name-only",
            "-G",
            T,
            "--",
            ISSUE_DIRECTORY + "/",
        ],
        repo,
    )
    candidates = []
    commit = None
    for line in raw.splitlines():
        if line.startswith("@@@"):
            commit = line[3:].strip()
            continue
        path = line.strip()
        if commit and MIRROR_PATH_RE.fullmatch(path) and (path not in skipped):
            candidates.append((commit, path))
    found = {}
    checked = set()
    for commit, path in candidates:
        for revision in (commit, f"{commit}^"):
            key = (revision, path)
            if key in checked:
                continue
            checked.add(key)
            text = git_blob(repo, revision, path)
            if text is None:
                continue
            activity = source_activity_on(text, T)
            if not activity:
                continue
            record = found.setdefault(path, {"activity": set(), "text": text})
            record["activity"].update(activity)
    return {
        path: {"activity": sorted(record["activity"]), "text": record["text"]}
        for path, record in found.items()
    }


def gh_touched(repo, T):
    found = {}
    current = {}
    paths = glob.glob(os.path.join(repo, ISSUE_DIRECTORY, "*", "*.md"))
    for path in paths:
        relpath = os.path.relpath(path, repo)
        try:
            with open(path, encoding="utf-8", errors="replace") as source:
                text = source.read()
        except OSError:
            continue
        activity = source_activity_on(text, T)
        current[relpath] = (path, text)
        if not activity:
            continue
        record = mirror_meta(repo, path, text, activity, "current")
        if record is not None:
            ref, meta = record
            found[ref] = meta
    historical = historical_source_activity(
        repo, T, skip_paths={meta["file"] for meta in found.values()}
    )
    for relpath, evidence in historical.items():
        path, text = current.get(relpath, (os.path.join(repo, relpath), evidence["text"]))
        record = mirror_meta(repo, path, text, evidence["activity"], "git-history")
        if record is not None:
            ref, meta = record
            found[ref] = meta
    return {ref: found[ref] for ref in sort_refs(found)}


def mirror_repo_catalog(repo):
    by_number = {}
    by_name = {}
    path_repo = {}
    for path in glob.glob(os.path.join(repo, ISSUE_DIRECTORY, "*", "*.md")):
        try:
            with open(path, encoding="utf-8", errors="replace") as source:
                text = source.read()
        except OSError:
            continue
        ident = mirror_identity(path, text)
        if ident is None:
            continue
        _ref, gh_repo, number = ident
        by_number.setdefault(number, set()).add(gh_repo)
        basename = gh_repo.rsplit("/", 1)[-1]
        by_name.setdefault(basename, set()).add(gh_repo)
        parts = basename.split("-")
        for i in range(1, len(parts)):
            by_name.setdefault("-".join(parts[i:]), set()).add(gh_repo)
        path_repo[os.path.relpath(path, repo)] = gh_repo
    return {"by_number": by_number, "by_name": by_name, "path_repo": path_repo}


def choose_repo(number, catalog, projects=(), paths=(), context=""):
    number = int(number)
    candidates = set(catalog["by_number"].get(number, set()))
    hints = {str(project).lower() for project in projects if project}
    hints.update(
        (
            gh_repo.rsplit("/", 1)[-1]
            for path in paths
            if (gh_repo := catalog["path_repo"].get(path))
        )
    )
    hinted = {repo_name for repo_name in candidates if repo_name.rsplit("/", 1)[-1] in hints}
    if len(hinted) == 1:
        return next(iter(hinted))
    lowered = context.lower()
    mentioned = {
        repo_name
        for repo_name in candidates
        if re.search(
            f"(?<![A-Za-z0-9_.-]){re.escape(repo_name.rsplit('/', 1)[-1])}(?![A-Za-z0-9_.-])",
            lowered,
        )
    }
    if len(mentioned) == 1:
        return next(iter(mentioned))
    if len(candidates) == 1:
        return next(iter(candidates))
    hinted_repos = set()
    for hint in hints:
        hinted_repos.update(catalog["by_name"].get(hint, set()))
    if len(hinted_repos) == 1:
        return next(iter(hinted_repos))
    return DEFAULT_REPO


def issue_numbers_in_text(text):
    numbers = {int(n) for n in NUM_RE.findall(text)}
    numbers.update((int(match.group(2)) for match in GH_URL_RE.finditer(text)))
    return sorted(numbers)


def issue_refs_in_text(text, catalog, projects=(), paths=()):
    refs = set()
    occupied = []
    for match in GH_URL_RE.finditer(text):
        refs.add(canonical(match.group(1), match.group(2)))
    for match in FULL_REF_RE.finditer(text):
        refs.add(canonical(match.group(1), match.group(2)))
        occupied.append(match.span())
    for match in SHORT_REF_RE.finditer(text):
        if any((start <= match.start() < end for start, end in occupied)):
            continue
        short_name, number = match.groups()
        repos = set(catalog["by_name"].get(short_name.lower(), set()))
        number_candidates = set(catalog["by_number"].get(int(number), set()))
        narrowed = repos & number_candidates
        if len(narrowed) == 1:
            gh_repo = next(iter(narrowed))
        elif len(repos) == 1:
            gh_repo = next(iter(repos))
        else:
            gh_repo = choose_repo(number, catalog, projects, paths, match.group(0))
        refs.add(canonical(gh_repo, number))
        occupied.append(match.span())
    for match in NUM_RE.finditer(text):
        if any((start <= match.start() < end for start, end in occupied)):
            continue
        gh_repo = choose_repo(match.group(1), catalog, projects, paths, text)
        refs.add(canonical(gh_repo, match.group(1)))
    return sort_refs(refs)


def commit_entities(repo, T):
    catalog = mirror_repo_catalog(repo)
    start = (datetime.strptime(T, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    raw = sh(
        [
            "git",
            "log",
            f"--since={start} 00:00",
            f"--until={T} 23:59",
            "--no-merges",
            "--name-only",
            "--format=@@@%h|%cs|%s",
            "--",
            *[value + "/" for value in COMMIT_DIRECTORIES],
        ],
        repo,
    )
    commits = []
    cur = None
    for line in raw.splitlines():
        if line.startswith("@@@"):
            if cur:
                commits.append(cur)
            h, cdate, subj = line[3:].split("|", 2)
            kind = classify(subj)
            cur = {
                "commit": h,
                "commit_date": cdate,
                "subject": subj,
                "kind": kind,
                "files": [],
                "projects": set(),
                "issues": [],
                "issue_refs": [],
                "states": sorted(set((m.lower() for m in STATE_RE.findall(subj)))),
                "semantic_dates": set(),
            }
        elif line.strip() and cur is not None:
            if line.startswith(SUMMARY_DIRECTORY + "/"):
                continue
            cur["files"].append(line)
            p = project_of(line)
            if p:
                cur["projects"].add(p)
            cur["semantic_dates"].add(semantic_date(line, cur["commit_date"]))
    if cur:
        commits.append(cur)
    commits = [c for c in commits if c["files"] or c["kind"] != "content"]
    for c in commits:
        c["projects"] = sorted(c["projects"])
        c["semantic_dates"] = sorted(c["semantic_dates"])
        c["file_count"] = len(c["files"])
        c["issues"] = [str(number) for number in issue_numbers_in_text(c["subject"])]
        c["issue_refs"] = issue_refs_in_text(c["subject"], catalog, c["projects"], c["files"])
        body_issues = set()
        body_refs = set()
        if c["kind"] == "content":
            diff = sh(["git", "show", "--no-color", "--unified=0", "--format=", c["commit"]], repo)
            diff_path = ""
            for line in diff.splitlines():
                if line.startswith("+++ b/"):
                    diff_path = line[6:]
                    continue
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                for m in NUM_RE.finditer(line):
                    n = m.group(1)
                    lo = max(0, m.start() - 40)
                    hi = min(len(line), m.end() + 40)
                    if STATE_RE.search(line[lo:hi]):
                        body_issues.add(n)
                        body_refs.update(
                            issue_refs_in_text(
                                line[lo:hi],
                                catalog,
                                c["projects"],
                                [diff_path] if diff_path else c["files"],
                            )
                        )
                for m in GH_URL_RE.finditer(line):
                    lo = max(0, m.start() - 40)
                    hi = min(len(line), m.end() + 40)
                    if STATE_RE.search(line[lo:hi]):
                        body_issues.add(m.group(2))
                        body_refs.add(canonical(m.group(1), m.group(2)))
        c["issues_in_body"] = sorted(body_issues - set(c["issues"]), key=int)
        c["issue_refs_in_body"] = sort_refs(body_refs - set(c["issue_refs"]))
        if len(c["files"]) > 12:
            c["files"] = c["files"][:12] + [f"...(+{len(c['files']) - 12} more)"]
    return commits


HIST_MSG = re.compile(
    "^###\\s+(\\d{4}-\\d{2}-\\d{2})\\s+(\\d{2}):(\\d{2}):\\d{2}Z\\s+(?:—|--)\\s+(user|assistant)",
    re.M,
)
CLAW_MSG = re.compile(
    "^##\\s+(\U0001f464 User|\U0001f916 Assistant)\\s+\\((\\d{2}):(\\d{2})\\)", re.M
)
CLAW_DATE = re.compile("^- \\*\\*Date:\\*\\*\\s+(\\d{4}-\\d{2}-\\d{2})", re.M)
CRON_TAG = re.compile("\\[cron:[0-9a-f-]+\\s+([^\\]]+)\\]")


def is_machine_prompt(body):
    b = body.strip()
    if not b:
        return True
    bl = b.lower()
    if re.match("^\\[slash\\]", b) or re.match("^/\\w+\\s*$", b):
        return True
    if re.match("^\\[cron:", b):
        return True
    if re.search("<teammate-message|teammate_id=|</teammate-message>", b):
        return True
    if re.match(
        "^(BACKTEST|You are |You are an? |Read /home/|Read the prompt|FULL .*REGRESSION)", b
    ):
        return True
    if re.search('runtime="?(subagent|acp)"|sessions_spawn|spawn(ed)? (a )?sub', bl):
        return True
    if re.search(
        "OpenClaw runtime context|\\[Internal task completion|HEARTBEAT|Read HEARTBEAT\\.md|system event|\\[Inter-session message\\]|\\[Bootstrap|Current time:\\s",
        b,
    ):
        return True
    if (OPTIONS.get("anti_echo_job_name") and OPTIONS["anti_echo_job_name"].lower() in bl) or (
        OPTIONS.get("anti_echo_summary_path")
        and OPTIONS["anti_echo_summary_path"] in b
        and "scan" in bl
    ):
        return True
    if any(re.search(pattern, b) for pattern in OPTIONS.get("machine_prompt_patterns", [])):
        return True
    return False


def clean_prompt(body):
    b = body.strip()
    if b.startswith("Sender") and "metadata" in b[:40]:
        m = re.search("\\]\\s*(.+)$", b, re.S)
        if m:
            b = m.group(1).strip()
        else:
            m = re.search("```\\s*(.+)$", b, re.S)
            b = m.group(1).strip() if m else ""
    b = re.sub("^\\[[^\\]]{4,40}\\]\\s*", "", b).strip()
    return b


def legacy_claw_format(text, source):
    selected = next(
        (item["format"] for item in SESSION_SOURCES if item["label"] == source), "history"
    )
    header = text.partition("\n---\n")[0]
    managed = re.search(r"^- Managed-By: agent-session-extraction/v1\s*$", header, re.MULTILINE)
    return selected == "claw" and not managed


def real_user_prompts(text, source, T):
    out = []
    if legacy_claw_format(text, source):
        for m in re.finditer(
            "## \U0001f464 User \\((\\d{2}):(\\d{2})\\)\\s*\\n+(.+?)(?=\\n## |\\Z)", text, re.S
        ):
            hh, mm, body = (int(m.group(1)), int(m.group(2)), m.group(3).strip())
            if is_machine_prompt(body):
                continue
            body = clean_prompt(body)
            if not body or is_machine_prompt(body):
                continue
            out.append((hh * 60 + mm, body[:240]))
    else:
        for m in re.finditer(
            "###\\s+(\\d{4}-\\d{2}-\\d{2})\\s+(\\d{2}):(\\d{2}):\\d{2}Z\\s+(?:—|--)\\s+user\\s*\\n+((?:>.*\\n?)+)",
            text,
        ):
            d, hh, mm = (m.group(1), int(m.group(2)), int(m.group(3)))
            if d != T:
                continue
            body = re.sub("^>\\s?", "", m.group(4), flags=re.M).strip()
            if is_machine_prompt(body):
                continue
            body = clean_prompt(body)
            if not body or is_machine_prompt(body):
                continue
            out.append((hh * 60 + mm, body[:240]))
    return out


def session_events(repo, T):
    events = []
    meta = {}
    for selected in SESSION_SOURCES:
        source = selected["label"]
        paths = [
            p
            for p in glob.glob(
                os.path.join(repo, selected["directory"], "**", "*.md"), recursive=True
            )
            if not (
                p.endswith("README.md") or p.endswith("PROVENANCE.md") or "/compacted-legacy/" in p
            )
        ]
        by_base = {}
        for p in paths:
            by_base.setdefault(os.path.basename(p), []).append(p)
        paths = [
            max(ps, key=lambda q: (os.path.getsize(q), q.count("/"))) for ps in by_base.values()
        ]
        for p in sorted(paths):
            try:
                with open(p, encoding="utf-8", errors="replace") as source_file:
                    text = source_file.read()
            except OSError:
                continue
            mins = []
            if legacy_claw_format(text, source):
                dm = CLAW_DATE.search(text)
                fdate = dm.group(1) if dm else os.path.basename(p)[8:18]
                if fdate != T:
                    continue
                for _who, hh, mm in CLAW_MSG.findall(text):
                    mins.append(int(hh) * 60 + int(mm))
                started_on = fdate
            else:
                hist = HIST_MSG.findall(text)
                for d, hh, mm, _who in hist:
                    if d == T:
                        mins.append(int(hh) * 60 + int(mm))
                sm = re.search("^- Started:\\s*~?(\\d{4}-\\d{2}-\\d{2}) ", text, re.M)
                started_on = (
                    sm.group(1) if sm else min((d for d, _hh, _mm, _who in hist), default=T)
                )
            if not mins:
                continue
            sid = ""
            m = re.search("Session ID:\\**\\s*`?([0-9a-f-]{8,})`?", text)
            if m:
                sid = m.group(1)
            elif selected["format"] == "claw":
                m = re.search(
                    r"^- Session: ([^\r\n]+)$", text.partition("\n---\n")[0], re.MULTILINE
                )
                if m:
                    sid = m.group(1).strip()
            prompts = real_user_prompts(text, source, T)
            cron = CRON_TAG.search(text)
            if cron:
                kind = f"cron:{cron.group(1).strip()}"
            elif prompts:
                kind = "human"
            else:
                kind = "machine"
            ttl = ""
            m = re.search("^#\\s+(.+)", text, re.M)
            if m and (not m.group(1).startswith("Claw Session")):
                ttl = m.group(1).strip()[:120]
            meta[p] = {
                "sid": sid,
                "kind": kind,
                "title": ttl,
                "started_on": started_on,
                "prompts": prompts,
                "source": source,
            }
            for mn in mins:
                events.append((mn, source, p, sid, kind))
    return (events, meta)


def _cluster_one_stream(events, gap_min):
    clusters = []
    cur = None
    for mn, source, path, sid, kind in events:
        if cur is None or mn - cur["_last"] > gap_min:
            cur = {"start_min": mn, "_last": mn, "files": {}, "msgs": 0}
            clusters.append(cur)
        cur["_last"] = mn
        cur["msgs"] += 1
        cur["files"][path] = cur["files"].get(path, 0) + 1
    return clusters


def cluster_sessions(repo, T, gap_min):
    events, meta = session_events(repo, T)
    if not events:
        return []
    human_ev = sorted((e for e in events if e[4] == "human"))
    machine_ev = sorted((e for e in events if e[4] != "human"))
    raw = [("human", c) for c in _cluster_one_stream(human_ev, gap_min)] + [
        ("machine", c) for c in _cluster_one_stream(machine_ev, gap_min)
    ]

    def fmt(m):
        return f"{m // 60:02d}:{m % 60:02d}"

    out = []
    for kind, c in raw:
        files = list(c["files"].keys())
        continued, cronnames = (set(), set())
        sess_detail = []
        all_prompts = []
        for f in files:
            md = meta[f]
            in_window = [pt for pm, pt in md["prompts"] if c["start_min"] <= pm <= c["_last"]]
            if md["started_on"] < T:
                continued.add(md["started_on"])
            if md["kind"].startswith("cron:"):
                cronnames.add(md["kind"][5:])
            if kind == "human":
                sess_detail.append(
                    {
                        "session": md["sid"][:8] if md["sid"] else os.path.basename(f),
                        "source": md.get("source", ""),
                        "title": md["title"],
                        "n_real_prompts": len(in_window),
                        "started_on": md["started_on"],
                    }
                )
                all_prompts += in_window
        rec = {
            "kind": kind,
            "time": f"{fmt(c['start_min'])}–{fmt(c['_last'])}Z",
            "span_min": c["_last"] - c["start_min"],
            "messages": c["msgs"],
            "n_sessions": len(files),
        }
        if kind == "human":
            seen = set()
            uniq_p = [p for p in all_prompts if not (p in seen or seen.add(p))]
            rec["n_real_prompts"] = len(all_prompts)
            rec["sessions"] = sorted(sess_detail, key=lambda s: -s["n_real_prompts"])
            rec["user_prompts"] = uniq_p
            rec["continued_from"] = sorted(continued)
        else:
            rec["cron"] = sorted(cronnames) or ["machine (subagent/teammate/system)"]
        out.append(rec)
    out.sort(key=lambda r: (r["kind"] != "human", -r.get("n_real_prompts", 0), -r["messages"]))
    return out


def doc_changes(repo, T):
    raw = sh(
        [
            "git",
            "log",
            f"--since={T} 00:00",
            f"--until={T} 23:59",
            "--no-merges",
            "--name-only",
            "--format=",
            "--",
            DOCUMENT_DIRECTORY + "/",
            WIKI_PROJECT_DIRECTORY + "/",
        ],
        repo,
    )
    gdocs, wiki = ({}, {})
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(SUMMARY_DIRECTORY + "/"):
            continue
        if line.startswith(DOCUMENT_DIRECTORY + "/"):
            m = re.search(re.escape(DOCUMENT_DIRECTORY) + r"/([^/]+)/", line)
            if m:
                gdocs[m.group(1)] = gdocs.get(m.group(1), 0) + 1
        elif line.startswith(WIKI_PROJECT_DIRECTORY + "/"):
            m = re.search(re.escape(WIKI_PROJECT_DIRECTORY) + r"/([^/]+)/", line)
            if m:
                wiki.setdefault(m.group(1), set()).add(line)
    return {
        "google_docs": [{"doc": k, "files_changed": v} for k, v in sorted(gdocs.items())],
        "wiki_projects": [
            {"project": k, "files": sorted(v)[:8], "n": len(v)}
            for k, v in sorted(wiki.items(), key=lambda kv: -len(kv[1]))
        ],
    }


def extract(T, repo, gap=45):
    datetime.strptime(T, "%Y-%m-%d")
    commits = commit_entities(repo, T)
    clusters = cluster_sessions(repo, T, gap)
    start = (datetime.strptime(T, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    n_human = sum((1 for c in clusters if c["kind"] == "human"))
    gh = gh_touched(repo, T)
    out = {
        "date": T,
        "window": f"{start}..{T}",
        "gh_touched_today": gh,
        "gh_touched_count": len(gh),
        "doc_changes": doc_changes(repo, T),
        "commit_count": len(commits),
        "content_commit_count": sum((1 for c in commits if c["kind"] == "content")),
        "commits": commits,
        "session_cluster_count": len(clusters),
        "human_cluster_count": n_human,
        "session_clusters": clusters,
    }
    return out


def serialize(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main(argv=None):
    import argparse

    from .config import DEFAULT_CONFIG, activate, home, load

    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("repository", nargs="?")
    parser.add_argument("--root")
    parser.add_argument(
        "--config", default=os.environ.get("ACTIVITY_SUMMARY_CONFIG", DEFAULT_CONFIG)
    )
    parser.add_argument("--gap-min", type=int)
    args = parser.parse_args(argv)
    cfg = load(home(args.config), args.root or args.repository)
    activate(cfg)
    sys.stdout.buffer.write(
        serialize(
            extract(
                args.target, cfg["repository_root"], args.gap_min or cfg["facts"]["gap_minutes"]
            )
        )
    )
    return 0


if __name__ == "__main__":
    # Use the module whose globals config.activate() configures, not __main__.
    from .facts import main

    raise SystemExit(main())
