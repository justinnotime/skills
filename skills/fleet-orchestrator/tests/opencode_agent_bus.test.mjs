import assert from "node:assert/strict"
import { test } from "node:test"
import { readFileSync } from "node:fs"
import { stripTypeScriptTypes } from "node:module"
import { SourceTextModule, SyntheticModule, createContext } from "node:vm"
import { EventEmitter } from "node:events"
import { PassThrough } from "node:stream"
import * as readline from "node:readline"
import * as path from "node:path"
import * as crypto from "node:crypto"
import * as util from "node:util"
import { execFileSync } from "node:child_process"
import { fileURLToPath } from "node:url"

// Evaluate the shipped module, not a copied implementation or source-string
// assertions. I/O is fake except the Python wrapper's isolated native tests.
const source = stripTypeScriptTypes(readFileSync(new URL("../plugins/opencode/agent-bus.ts", import.meta.url), "utf8"))
const agentID = "11111111-1111-4111-8111-111111111111"
const directory = "/project"
const root = (id, extra = {}) => ({ id, projectID: "project", directory, ...extra })
const settle = async () => { for (let i = 0; i < 15; i++) await new Promise(setImmediate) }
const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function harness(options = {}) {
  const identity = options.identity || agentID
  let now = 0
  let nextTimer = 0
  let nextFD = 0
  const timers = new Map()
  const files = options.files || new Map()
  const descriptors = new Map()
  const calls = []
  const prompts = []
  const children = []
  const errors = []
  const sessions = new Map([root("root"), root("other"), root("child", { parentID: "root" })].map((session) => [session.id, session]))
  const state = {
    store: "/state/fleet-a/bus.sqlite3", transport: "local", tmux_server: "fleet-server",
    pane: "%7", socket: "/tmux/server", serverPID: "100", started: "1000",
    location: "fleet-a:1.0", title: "opencode", retired: false, heartbeatError: false,
    promptError: false, messages: [], members: [], ...options.state,
  }
  const env = { TMUX_PANE: state.pane, XDG_RUNTIME_DIR: "/runtime", ...options.env }
  const failure = (stderr) => Object.assign(new Error(stderr), { stderr })
  const command = async (executable, args) => {
    calls.push([executable, ...args])
    if (options.command) {
      const override = await options.command(executable, args, state)
      if (override !== undefined) return override
    }
    if (executable === "tmux") {
      assert.equal(args.at(-1), "#{socket_path}\t#{pid}\t#{start_time}\t#{pane_id}\t#{window_index}.#{pane_index}\t#{pane_dead}")
      assert.deepEqual(Array.from(args.slice(0, -1)), ["-L", "fleet-server", "-u", "list-panes", "-s", "-t", `=${state.location.split(":")[0]}:`, "-F"])
      return [state.socket, state.serverPID, state.started, state.pane, state.location.split(":").at(-1), "0"].join("\t")
    }
    assert.equal(executable, "agent-bus", "no real/unexpected executable allowed")
    const [verb] = args
    if (verb === "environment") return JSON.stringify({ database: state.store, transport: state.transport, tmux_server: state.tmux_server })
    if (verb === "tmux-id") return `tmux=${state.location} win=${state.title}`
    if (verb === "members") return state.members.map((member) => JSON.stringify(member)).join("\n")
    if (verb === "handle") return `host/${args[1]}-tmux1`
    if (verb === "setup") return "setup ok"
    if (verb === "join") {
      const [, handle, slot, harness, mode, host, tmux] = args
      if (state.members.some((member) => member.status === "active" && member.host === host
        && member.pane_id === state.pane && member.slot !== slot)) throw failure("this pane already has an ACTIVE seat")
      return JSON.stringify({ schema: "agent-bus/join-result/v3", agent_id: identity,
        handle, slot, harness, mode, host, tmux, generation: 1, status: "active", ...options.join })
    }
    assert.equal(args[1], identity)
    if (verb === "heartbeat") {
      if (state.retired) throw failure(`agent-bus-v3: identity ${identity} is retired; rejoin it before heartbeat`)
      if (state.heartbeatError) throw failure("registry temporarily unavailable")
      return ""
    }
    if (verb === "pull") {
      const max = Number(args[args.indexOf("--max") + 1])
      const available = state.messages.filter((message) => !message.done && message.lease <= now && message.attempt < 3)
      const selected = available.slice(0, max)
      for (const message of selected) { message.lease = now + 60000; message.attempt++ }
      const records = selected.map(({ lease, done, ...message }) => message)
      if (available.length > max) records.push({ schema: "agent-bus/digest/v3", remaining: available.length - max, urgent: 0, oldest_ms: 0 })
      return records.map((record) => JSON.stringify(record)).join("\n")
    }
    if (verb === "ack") {
      const message = state.messages.find((message) => message.msg_id === args[2])
      assert.ok(message && message.attempt > 0, "must present before acknowledging")
      message.done = true
      return "processed ok"
    }
    throw new Error(`unexpected bus verb: ${verb}`)
  }
  const execFile = () => { throw new Error("use async execFile") }
  execFile[util.promisify.custom] = async (executable, args) => ({ stdout: await command(executable, args), stderr: "" })
  const spawn = (executable, args) => {
    assert.equal(executable, "agent-bus")
    assert.deepEqual(Array.from(args), ["watch", identity])
    const child = new EventEmitter()
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = () => { child.killed = true; child.emit("close", 0) }
    children.push(child)
    return child
  }
  const fs = {
    mkdirSync() {},
    openSync(file, flags) {
      assert.equal(flags, "wx")
      if (files.has(file)) throw Object.assign(new Error("exists"), { code: "EEXIST" })
      files.set(file, "")
      descriptors.set(++nextFD, file)
      return nextFD
    },
    writeFileSync(fd, data) { files.set(descriptors.get(fd), data) },
    readFileSync(file) { assert.ok(files.has(file)); return files.get(file) },
    closeSync(fd) { assert.ok(descriptors.delete(fd), "close only owned lock") },
    unlinkSync(file) { assert.ok(files.delete(file), "unlink only owned lock") },
  }
  const chain = new Proxy(() => chain, { get: () => chain })
  const tool = Object.assign((definition) => definition, { schema: chain })
  const client = {
    session: {
      async get({ path: { id } }) {
        if (options.get) return { data: await options.get(id, sessions) }
        if (!sessions.has(id)) throw new Error("missing session")
        return { data: sessions.get(id) }
      },
      async list() { throw new Error("must never guess a conversation from session.list") },
      async promptAsync(request) {
        if (options.prompt) await options.prompt(request)
        if (state.promptError) throw new Error("prompt unavailable")
        prompts.push(request)
      },
    },
  }
  const context = createContext({
    process: { env, pid: options.pid || 200, kill(pid, signal) {
      assert.equal(signal, 0)
      if (options.killError) throw Object.assign(new Error("kill error"), { code: options.killError })
      assert.ok(pid > 0)
    } },
    console: { error: (...parts) => errors.push(parts.join(" ")) },
    setTimeout(callback, delay) { timers.set(++nextTimer, { callback, due: now + delay }); return nextTimer },
    clearTimeout(id) { timers.delete(id) },
  })
  const modules = {
    "@opencode-ai/plugin": { tool }, "node:child_process": { execFile, spawn },
    "node:fs": fs, "node:readline": readline, "node:path": path, "node:crypto": crypto,
    "node:os": { hostname: () => "host" }, "node:util": util,
  }
  const plugin = new SourceTextModule(source, { context })
  await plugin.link((name) => {
    assert.ok(name in modules, `unmocked import: ${name}`)
    const entries = Object.entries(modules[name])
    return new SyntheticModule(entries.map(([key]) => key), function () {
      for (const [key, value] of entries) this.setExport(key, value)
    }, { context })
  })
  await plugin.evaluate()
  const hooks = await plugin.namespace.default({ client, directory: options.directory || directory, project: { id: "project" } })
  const h = {
    hooks, state, env, files, calls, prompts, children, errors, sessions, timers,
    commands: (verb) => calls.filter((call) => call[1] === verb),
    async chat(id = "root") { await hooks["chat.message"]({ sessionID: id }); await settle() },
    async tick(ms = 0) {
      const until = now + ms
      for (let count = 0; count < 1000; count++) {
        await settle()
        const next = [...timers].filter(([, timer]) => timer.due <= until).sort((a, b) => a[1].due - b[1].due)[0]
        if (!next) { now = until; return }
        now = next[1].due
        timers.delete(next[0])
        next[1].callback()
      }
      throw new Error("timer spin")
    },
    signal(id = identity) { children.at(-1).stdout.write(JSON.stringify({ schema: "agent-bus/inbox-changed/v3", agent_id: id, count: 1 }) + "\n") },
    add(count = 1) {
      for (let i = 0; i < count; i++) state.messages.push({ schema: "agent-bus/message/v3",
        msg_id: `message-${state.messages.length + 1}`, sender_agent_id: "peer-id", sender_handle: "host/peer",
        subject: "work", body: "peer content", priority: "normal", created_ms: now, attempt: 0, lease: 0 })
    },
    pull(max = 10, sessionID = "root") { return hooks.tool.agent_bus_pull.execute({ max }, { sessionID }) },
    ack(msg_id, sessionID = "root") { return hooks.tool.agent_bus_ack.execute({ msg_id, status: "ok", detail: "handled" }, { sessionID }) },
    async dispose() { await hooks.dispose(); await settle() },
  }
  return h
}

const legacyMember = (extra = {}) => ({
  schema: "agent-bus/agent/v3", agent_id: agentID, slot: "opencode:/project",
  handle: "host/opencode-project-tmux1", status: "active", harness: "opencode", mode: "watch", host: "host",
  pane_id: "%7", tmux_server_id: "tmux:" + crypto.createHash("sha256").update(JSON.stringify(["/tmux/server", "100", "1000"])).digest("hex"),
  terminal_presence: "present", tmux: "tmux=fleet-a:1.0", ...extra,
})

test("seeded directory-slot upgrade preserves identity and pending inbox without rejoining", async () => {
  const member = legacyMember()
  const h = await harness({ state: { members: [member] } })
  h.add(12)
  h.state.messages[0].attempt = 1
  h.state.messages[0].lease = 60000
  await h.chat("child")
  assert.equal(h.commands("members").length, 0)
  await h.chat()
  await h.tick()
  assert.equal(h.commands("members").length, 1)
  assert.equal(h.commands("join").length, 0)
  assert.equal(h.commands("setup").length, 0)
  assert.equal(h.children.length, 1)
  assert.equal(h.prompts.length, 11)
  assert.equal(h.prompts.every((prompt) => prompt.path.id === "root"), true)
  assert.equal(h.state.messages[0].attempt, 1, "existing lease is preserved")
  assert.deepEqual(h.state.members, [member], "registration is not rewritten")
  assert.equal(h.commands("pull").every((call) => call[2] === member.agent_id), true)
  await h.tick(60000)
  assert.ok(h.prompts.some((prompt) => prompt.body.parts[0].text.includes("Message ID: message-1\n")))
  await h.ack("message-1")
  assert.equal(h.state.messages[0].done, true)
  await h.dispose()
})

test("explicit slot overrides a matching legacy registration without a members lookup", async () => {
  const h = await harness({ env: { AGENT_BUS_SLOT: "operator-selected" }, state: { members: [legacyMember()] } })
  await h.chat()
  assert.equal(h.commands("members").length, 0)
  assert.equal(h.commands("join")[0][3], "operator-selected")
  assert.equal(h.children.length, 0, "bus still prevents replacing an active same-pane identity")
  await h.dispose()
})

test("legacy pane survives window rename/renumber but checkout during adoption stays final", async () => {
  const resumed = await harness({ state: { members: [legacyMember()], location: "renamed:9.0", title: "new title" } })
  await resumed.chat()
  assert.equal(resumed.commands("join").length, 0)
  assert.equal(resumed.children.length, 1)
  await resumed.dispose()
  const retired = await harness({ state: { members: [legacyMember()] }, command: (_exe, args, state) => {
    if (args[0] === "members") state.retired = true
  } })
  await retired.chat()
  await retired.tick(60000)
  assert.equal(retired.commands("join").length, 0)
  assert.equal(retired.commands("pull").length, 0)
  assert.equal(retired.children.length, 0)
  assert.equal(retired.files.size, 0)
  assert.equal(retired.timers.size, 0)
  await retired.dispose()
})

test("legacy adoption requires exact directory, harness, pane, server generation, host and active presence", async () => {
  for (const changes of [
    { slot: "opencode:/another" }, { slot: "opencode:/project/" }, { harness: "claude" }, { mode: "pull" },
    { host: "another-host" }, { pane_id: "%8" }, { tmux_server_id: "tmux:another-server" },
    { tmux_server_id: null }, { terminal_presence: "unknown" }, { terminal_presence: "absent" }, { status: "retired" },
  ]) {
    const member = legacyMember(changes)
    const h = await harness({ state: { members: [member] } })
    await h.chat()
    assert.equal(h.commands("join").length, 1, JSON.stringify(changes))
    assert.match(h.commands("join")[0][3], /^opencode:[0-9a-f]{64}$/)
    assert.deepEqual(h.state.members, [member], "must not move/revive/rewrite the legacy registration")
    await h.dispose()
  }
})

test("only the selected database's members can authorize legacy adoption", async () => {
  const stores = new Map([["/state/fleet-a/bus.sqlite3", [legacyMember()]], ["/state/fleet-b/bus.sqlite3", []]])
  const h = await harness({ state: { store: "/state/fleet-b/bus.sqlite3" }, command: (_exe, args, state) => args[0] === "members"
    ? stores.get(state.store).map((member) => JSON.stringify(member)).join("\n") : undefined })
  await h.chat()
  assert.match(h.commands("join")[0][3], /^opencode:[0-9a-f]{64}$/)
  await h.dispose()
  const changed = await harness({ command: (_exe, args, state) => {
    if (args[0] === "members") { state.store = "/state/fleet-b/bus.sqlite3"; return JSON.stringify(legacyMember()) }
  } })
  await changed.chat()
  assert.equal(changed.commands("join").length, 0)
  assert.equal(changed.children.length, 0)
  assert.equal(changed.commands("heartbeat").length, 0)
  await changed.dispose()
})

test("unavailable, unprovable or ambiguous members never authorize legacy adoption", async () => {
  for (const result of ["failure", "not json", JSON.stringify({ schema: "wrong" }),
    JSON.stringify(legacyMember({ slot: undefined })), JSON.stringify(legacyMember({ agent_id: "invalid" })),
    [legacyMember(), legacyMember({ agent_id: "22222222-2222-4222-8222-222222222222" })].map(JSON.stringify).join("\n"),
  ]) {
    const h = await harness({ command: (_exe, args) => {
      if (args[0] === "members") {
        if (result === "failure") throw new Error("members unavailable")
        return result
      }
    } })
    await h.chat()
    assert.equal(h.commands("join").length, 0)
    assert.equal(h.children.length, 0)
    assert.equal(h.files.size, 0)
    await h.dispose()
  }
})

// Only the Python wrapper supplies the private server/database. Models and
// watcher processes stay fake; the upgrade test invokes the real bus runtime.
if (process.env.OPENCODE_TEST_TMUX_SERVER) {
  test("native tmux C locale parses the plugin's tab-delimited terminal query", async () => {
    assert.equal(process.env.LC_ALL, "C")
    const server = process.env.OPENCODE_TEST_TMUX_SERVER
    let observed = false
    const h = await harness({ state: { tmux_server: server, pane: process.env.OPENCODE_TEST_TMUX_PANE, location: "plugin-test:0.0" },
      command: (executable, args) => {
        if (executable !== "tmux") return undefined
        assert.deepEqual(Array.from(args.slice(0, 2)), ["-L", server])
        const output = execFileSync(process.env.OPENCODE_TEST_TMUX_BIN, args, { env: process.env, encoding: "utf8", timeout: 10000 })
        assert.equal(output.trim().split("\t").length, 6)
        observed = true
        return output
      },
    })
    assert.equal(observed, true)
    await h.chat()
    assert.equal(h.commands("join").length, 1, h.errors.join("\n"))
    assert.equal(h.children.length, 1)
    await h.dispose()
  })

  test("real members legacy upgrade preserves the registered identity and processes its inbox", async () => {
    const server = process.env.OPENCODE_TEST_TMUX_SERVER
    const identity = process.env.OPENCODE_TEST_AGENT_ID
    const messageID = process.env.OPENCODE_TEST_MESSAGE_ID
    assert.ok(identity && messageID)
    const runtime = fileURLToPath(new URL("../scripts/agent-bus-v3.py", import.meta.url))
    const h = await harness({ identity,
      state: { tmux_server: server, pane: process.env.OPENCODE_TEST_TMUX_PANE, location: "plugin-test:0.0" },
      command: (executable, args) => {
        if (executable === "tmux") {
          assert.deepEqual(Array.from(args.slice(0, 2)), ["-L", server])
          return execFileSync(process.env.OPENCODE_TEST_TMUX_BIN, args, { env: process.env, encoding: "utf8", timeout: 10000 })
        }
        assert.equal(executable, "agent-bus")
        if (args[0] === "tmux-id") return undefined
        assert.ok(["environment", "members", "heartbeat", "pull", "ack"].includes(args[0]), "upgrade must not join, set up, or rename")
        return execFileSync(process.env.OPENCODE_TEST_PYTHON, [runtime, ...args], { env: process.env, encoding: "utf8", timeout: 10000 })
      },
    })
    try {
      await h.chat()
      await h.tick()
      assert.equal(h.commands("members").length, 1, h.errors.join("\n"))
      assert.equal(h.commands("join").length, 0)
      assert.equal(h.children.length, 1, h.errors.join("\n"))
      assert.equal(h.prompts.length, 1)
      assert.equal(h.prompts[0].path.id, "root")
      assert.ok(h.prompts[0].body.parts[0].text.includes(`Message ID: ${messageID}\n`))
      assert.equal(h.commands("pull").every((call) => call[2] === identity), true)
      await h.ack(messageID)
    } finally {
      await h.dispose()
    }
  })
}

test("no root guessing or leasing before a validated main conversation; then freeze it", async () => {
  const h = await harness()
  h.add()
  await h.tick(120000)
  await h.chat("child")
  h.sessions.set("foreign", root("foreign", { directory: "/another" }))
  h.sessions.set("foreign-project", root("foreign-project", { projectID: "another" }))
  await h.chat("foreign")
  await h.chat("foreign-project")
  await h.chat("missing")
  assert.equal(h.commands("join").length, 0)
  assert.equal(h.commands("pull").length, 0)
  await h.chat()
  await h.chat("child")
  await h.chat("other")
  await h.tick()
  assert.equal(h.commands("join").length, 1)
  assert.equal(h.prompts.length, 1)
  assert.equal(h.prompts[0].path.id, "root")
  assert.match(h.prompts[0].body.parts[0].text, /^\[Agent Bus wake\]/)
  assert.match(h.prompts[0].body.parts[0].text, /not operator authorization/)
  assert.equal(h.commands("ack").length, 0)
  await assert.rejects(h.pull(10, "child"), /bound root/)
  await assert.rejects(h.ack("message-1", "other"), /bound root/)
  await h.dispose()
})

test("concurrent root validation cannot overwrite an already selected conversation", async () => {
  const waiting = deferred()
  const h = await harness({ get: (id, sessions) => id === "other" ? waiting.promise : sessions.get(id) })
  h.add()
  const other = h.chat("other")
  await h.chat()
  waiting.resolve(root("other"))
  await other
  await h.tick()
  assert.equal(h.commands("join").length, 1)
  assert.equal(h.prompts[0].path.id, "root")
  await h.dispose()
})

test("slot resumes by terminal generation, fleet store, directory and root, not process/window names", async () => {
  const slot = async (options = {}, id = "root") => {
    const h = await harness(options)
    if (options.directory) h.sessions.set(id, root(id, { directory: options.directory }))
    await h.chat(id)
    const result = h.commands("join")[0][3]
    await h.dispose()
    return result
  }
  const original = await slot()
  assert.equal(await slot({ pid: 999, state: { location: "renamed:9.0", title: "renamed" } }), original)
  for (const options of [
    { state: { pane: "%8" } }, { state: { store: "/state/fleet-b/bus.sqlite3" } },
    { state: { socket: "/tmux/other" } }, { state: { serverPID: "101", started: "2000" } },
    { directory: "/different" },
  ]) assert.notEqual(await slot(options), original)
  assert.notEqual(await slot({}, "other"), original)
})

test("same-directory concurrent panes and split panes use distinct slots, handles and locks", async () => {
  const files = new Map()
  const first = await harness({ files })
  const second = await harness({ files, pid: 201, state: { pane: "%8", location: "fleet-a:1.1" } })
  await Promise.all([first.chat(), second.chat()])
  assert.equal(files.size, 2)
  assert.notEqual(first.commands("join")[0][3], second.commands("join")[0][3])
  assert.notEqual(first.commands("join")[0][2], second.commands("join")[0][2])
  await first.dispose()
  await second.dispose()
  assert.equal(files.size, 0)
})

test("explicit slots are preserved; locks distinguish punctuation and fleet stores", async () => {
  const files = new Map()
  const options = [
    { env: { AGENT_BUS_SLOT: "shipped/a" } }, { env: { AGENT_BUS_SLOT: "shipped:a" } },
    { env: { AGENT_BUS_SLOT: "shipped/a" }, state: { store: "/state/fleet-b/bus.sqlite3" } },
  ]
  const all = []
  for (const option of options) {
    const h = await harness({ ...option, files })
    await h.chat()
    assert.equal(h.commands("join")[0][3], option.env.AGENT_BUS_SLOT)
    all.push(h)
  }
  assert.equal(files.size, 3)
  const duplicate = await harness({ ...options[0], files })
  await duplicate.chat()
  assert.equal(duplicate.commands("join").length, 0)
  assert.match(duplicate.errors.join("\n"), /already has an OpenCode watcher/)
  assert.equal(files.size, 3)
  await duplicate.dispose()
  for (const h of all) await h.dispose()
})

for (const [field, value] of Object.entries({ schema: "old", agent_id: "", slot: "wrong", handle: "wrong", host: "wrong",
  tmux: "wrong", harness: "claude", mode: "pull", status: "retired", generation: 0 })) {
  test(`invalid join ${field} fails closed and releases only its lock`, async () => {
    const h = await harness({ join: { [field]: value } })
    await h.chat()
    await h.tick(120000)
    assert.equal(h.children.length, 0)
    assert.equal(h.commands("pull").length, 0)
    assert.equal(h.files.size, 0)
    assert.match(h.errors.join("\n"), /invalid join identity/)
    await h.chat("other")
    assert.equal(h.commands("join").length, 1)
    await h.dispose()
  })
}

test("invalid concrete terminal data refuses registration", async () => {
  for (const options of [
    { env: { TMUX_PANE: "" } }, { env: { TMUX_PANE: "%99" } },
    { state: { socket: "relative" } }, { state: { serverPID: "unknown" } },
    { state: { started: "" } }, { state: { location: "unknown" } },
  ]) await assert.rejects(harness(options), /tmux/)
})

test("all durable batches are delivered without another watcher signal", async () => {
  const h = await harness()
  h.add(25)
  await h.chat()
  await h.tick()
  assert.equal(h.prompts.length, 25)
  assert.equal(h.commands("pull").length, 4)
  assert.equal(new Set(h.prompts.map((request) => request.body.parts[0].text)).size, 25)
  assert.equal(h.commands("ack").length, 0)
  await h.dispose()
})

test("ack refills immediately; expired leases recover without a new message event", async () => {
  const h = await harness()
  h.add()
  await h.chat()
  await h.tick()
  h.add()
  await h.ack("message-1")
  await h.tick()
  assert.equal(h.prompts.length, 2)
  await h.tick(30000)
  assert.equal(h.prompts.length, 2, "no duplicate before lease expiry")
  await h.tick(30000)
  assert.equal(h.prompts.length, 3, "unacknowledged message is presented again")
  assert.match(h.prompts[2].body.parts[0].text, /Message ID: message-2/)
  await h.dispose()
})

test("delivery failure retries retained batch before acquiring more leases", async () => {
  const h = await harness({ state: { promptError: true } })
  h.add(15)
  await h.chat()
  await h.tick()
  await h.tick(60000)
  assert.equal(h.commands("pull").length, 1)
  assert.equal(h.prompts.length, 0)
  h.state.promptError = false
  await h.tick(5000)
  assert.ok(h.prompts.some((request) => request.body.parts[0].text.includes("message-15")), "resume must drain backlog too")
  await h.dispose()
})

test("digest-only oversized backlog does not busy-loop", async () => {
  const h = await harness({ command: (_exe, args) => args[0] === "pull"
    ? JSON.stringify({ schema: "agent-bus/digest/v3", remaining: 1, urgent: 0, oldest_ms: 0 }) : undefined })
  await h.chat()
  await h.tick()
  assert.equal(h.commands("pull").length, 1)
  await h.tick(30000)
  assert.equal(h.commands("pull").length, 2)
  assert.equal(h.prompts.length, 0)
  await h.dispose()
})

test("manual pull binds a root and retains the peer marker", async () => {
  const h = await harness()
  h.add(2)
  await assert.rejects(h.pull(1, "child"), /bound root/)
  assert.equal(h.commands("join").length, 0)
  const result = await h.pull(1)
  assert.match(result, /^\[Agent Bus wake\]/)
  assert.match(result, /not operator authorization/)
  await h.tick()
  assert.equal(h.prompts.length, 1)
  assert.match(h.prompts[0].body.parts[0].text, /message-2/)
  await h.dispose()
})

test("foreign watcher signals cannot pull this inbox", async () => {
  const h = await harness()
  await h.chat()
  await h.tick()
  const before = h.commands("pull").length
  h.add()
  h.signal("someone-else")
  await h.tick()
  assert.equal(h.commands("pull").length, before)
  assert.equal(h.prompts.length, 0)
  h.signal()
  await h.tick()
  assert.equal(h.prompts.length, 1)
  await h.dispose()
})

test("retired identity stops both a resident watcher and restart, without rejoin", async () => {
  for (const close of [false, true]) {
    const h = await harness()
    await h.chat()
    await h.tick()
    h.state.retired = true
    if (close) h.children[0].emit("close", 1)
    await h.tick(120000)
    assert.equal(h.children.length, 1)
    assert.equal(h.commands("join").length, 1)
    assert.equal(h.commands("pull").length, 1)
    assert.equal(h.timers.size, 0)
    assert.equal(h.files.size, 0)
    assert.equal(h.children[0].killed, true)
    await assert.rejects(h.pull(), /bound root/)
    await h.dispose()
  }
})

test("transient watcher failure restarts only after successful identity validation", async () => {
  const h = await harness()
  await h.chat()
  await h.tick()
  h.state.heartbeatError = true
  h.children[0].emit("close", 1)
  await h.tick(1000)
  assert.equal(h.children.length, 1)
  h.state.heartbeatError = false
  await h.tick(2000)
  assert.equal(h.children.length, 2)
  assert.equal(h.commands("join").length, 1)
  await h.dispose()
})

test("changing fleets cannot move an existing or not-yet-bound conversation to another store", async () => {
  for (const bound of [false, true]) {
    const h = await harness()
    if (bound) { await h.chat(); await h.tick() }
    const before = h.commands("pull").length
    h.state.store = "/state/different/bus.sqlite3"
    await h.chat()
    await h.tick(30000)
    assert.equal(h.commands("pull").length, before)
    assert.equal(h.commands("join").length, bound ? 1 : 0)
    assert.equal(h.timers.size, 0)
    await h.dispose()
  }
})

test("root deletion/disposal freezes delivery; in-flight pull cannot inject afterward", async () => {
  const waiting = deferred()
  const h = await harness({ command: (_exe, args) => args[0] === "pull" ? waiting.promise : undefined })
  await h.chat()
  await h.tick()
  await h.hooks.event({ event: { type: "session.deleted", properties: { info: root("root") } } })
  h.add()
  waiting.resolve(JSON.stringify(h.state.messages[0]))
  await settle()
  await h.chat("other")
  await h.tick(120000)
  assert.equal(h.prompts.length, 0)
  assert.equal(h.commands("join").length, 1)
  assert.equal(h.files.size, 0)
  assert.equal(h.timers.size, 0)
  await h.dispose()
  await h.dispose()
})

test("disposed during initial validation never joins", async () => {
  const waiting = deferred()
  const h = await harness({ get: () => waiting.promise })
  const chat = h.chat()
  await h.dispose()
  waiting.resolve(root("root"))
  await chat
  assert.equal(h.commands("join").length, 0)
  assert.equal(h.files.size, 0)
})

test("failed acknowledgments do not consume messages or trigger an immediate retry loop", async () => {
  const h = await harness({ command: (_exe, args) => {
    if (args[0] === "ack") throw new Error("ack unavailable")
  } })
  h.add()
  await h.chat()
  await h.tick()
  const before = h.commands("pull").length
  await assert.rejects(h.ack("message-1"), /ack unavailable/)
  await h.tick()
  assert.equal(h.commands("pull").length, before)
  assert.equal(h.state.messages[0].done, undefined)
  await h.tick(60000)
  assert.equal(h.prompts.length, 2)
  await h.dispose()
})

test("watcher bursts, manual pulls and acknowledgments do not duplicate in-flight presentation", async () => {
  const waiting = deferred()
  let first = true
  const h = await harness({ prompt: () => {
    if (first) { first = false; return waiting.promise }
  } })
  h.add(12)
  await h.chat()
  await h.tick()
  assert.equal(h.commands("pull").length, 1)
  const pull = h.pull()
  const ack = h.ack("message-1")
  for (let i = 0; i < 10; i++) h.signal()
  await settle()
  assert.equal(h.commands("pull").length, 1)
  waiting.resolve()
  const result = await pull
  await ack
  await h.tick()
  assert.equal(h.prompts.length, 10)
  assert.match(result, /message-11/)
  assert.match(result, /message-12/)
  assert.equal(h.state.messages.every((message) => message.attempt === 1), true)
  assert.equal(h.state.messages[0].done, true)
  await h.dispose()
})

test("malformed pull data retries without injecting an unknown schema", async () => {
  let invalid = true
  const h = await harness({ command: (_exe, args) => args[0] === "pull" && invalid
    ? JSON.stringify({ schema: "agent-bus/not-a-message", body: "not a message" }) : undefined })
  h.add()
  await h.chat()
  await h.tick()
  assert.equal(h.prompts.length, 0)
  assert.match(h.errors.join("\n"), /invalid inbox record/)
  invalid = false
  await h.tick(5000)
  assert.equal(h.prompts.length, 1)
  await h.dispose()
})

test("lock permission errors and invalid owners do not delete another watcher's lock", async () => {
  for (const kind of ["EPERM", "invalid"]) {
    const files = new Map()
    const first = await harness({ files })
    await first.chat()
    const file = [...files.keys()][0]
    if (kind === "invalid") files.set(file, "")
    const second = await harness({ files, killError: kind })
    await second.chat()
    assert.equal(second.commands("join").length, 0)
    assert.equal(files.size, 1)
    await second.dispose()
    await first.dispose()
  }
})

test("a confirmed dead process lock allows the same stable slot to resume", async () => {
  const files = new Map()
  const first = await harness({ files })
  await first.chat()
  const slot = first.commands("join")[0][3]
  // Simulate an unclean exit: retain only the lock, not a live watcher.
  const staleFiles = new Map(files)
  await first.dispose()
  const resumed = await harness({ files: staleFiles, pid: 900, killError: "ESRCH" })
  await resumed.chat()
  assert.equal(resumed.commands("join")[0][3], slot)
  assert.equal(resumed.children.length, 1)
  await resumed.dispose()
})
