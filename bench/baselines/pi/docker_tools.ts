/**
 * Routes pi's built-in read/write/edit/bash tools into an existing Docker container via `docker exec`.
 *
 * Env:
 *   SCITEAM_PI_CONTAINER  container name or id (required)
 *   SCITEAM_PI_WORKDIR    working directory inside the container (default /testbed)
 *   SCITEAM_PI_INIT       shell snippet prepended to every bash command (default: activate testbed env)
 *
 * The pi process itself runs on the host (it calls the LLM API from there); the container can stay on
 * `--network none`. Host environment variables are never forwarded into the container.
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { appendFileSync, existsSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
	type BashOperations,
	createBashToolDefinition,
	createEditToolDefinition,
	createReadToolDefinition,
	createWriteToolDefinition,
	type EditOperations,
	type ReadOperations,
	type WriteOperations,
} from "@earendil-works/pi-coding-agent";

const CONTAINER = process.env.SCITEAM_PI_CONTAINER ?? "";
const WORKDIR = process.env.SCITEAM_PI_WORKDIR ?? "/testbed";
const INIT = process.env.SCITEAM_PI_INIT ?? "source /opt/miniconda3/bin/activate testbed >/dev/null 2>&1";
const DOCKER = process.env.SCITEAM_DOCKER ?? "docker";

function dockerRun(args: string[], stdin?: Buffer | string): Promise<{ code: number | null; out: Buffer; err: Buffer }> {
	return new Promise((resolve, reject) => {
		const child = spawn(DOCKER, args, { stdio: [stdin === undefined ? "ignore" : "pipe", "pipe", "pipe"] });
		const out: Buffer[] = [];
		const err: Buffer[] = [];
		child.stdout.on("data", (d) => out.push(d));
		child.stderr.on("data", (d) => err.push(d));
		child.on("error", reject);
		child.on("close", (code) => resolve({ code, out: Buffer.concat(out), err: Buffer.concat(err) }));
		if (stdin !== undefined && child.stdin) {
			child.stdin.end(stdin);
		}
	});
}

async function execChecked(argv: string[], stdin?: Buffer | string): Promise<Buffer> {
	const r = await dockerRun(["exec", ...(stdin !== undefined ? ["-i"] : []), CONTAINER, ...argv], stdin);
	if (r.code !== 0) {
		const msg = r.err.toString().trim() || r.out.toString().trim();
		throw new Error(msg || `command failed with exit code ${r.code}`);
	}
	return r.out;
}

const readOps: ReadOperations = {
	readFile: (p) => execChecked(["cat", "--", p]),
	access: async (p) => {
		await execChecked(["test", "-r", p]).catch(() => {
			throw new Error(`ENOENT: no such file or directory, access '${p}'`);
		});
	},
	detectImageMimeType: async (p) => {
		const ext = p.toLowerCase().split(".").pop() ?? "";
		const map: Record<string, string> = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp" };
		return map[ext] ?? null;
	},
};

const writeOps: WriteOperations = {
	writeFile: async (p, content) => {
		await execChecked(["sh", "-c", 'cat > "$1"', "_", p], content);
	},
	mkdir: async (dir) => {
		await execChecked(["mkdir", "-p", "--", dir]);
	},
};

const editOps: EditOperations = {
	readFile: readOps.readFile,
	writeFile: writeOps.writeFile,
	access: async (p) => {
		await execChecked(["sh", "-c", 'test -r "$1" && test -w "$1"', "_", p]).catch(() => {
			throw new Error(`EACCES or ENOENT: cannot read/write '${p}'`);
		});
	},
};

const bashOps: BashOperations = {
	// Host env (options.env) is intentionally not forwarded into the container.
	exec: (command, cwd, { onData, signal, timeout }) =>
		new Promise((resolve, reject) => {
			const pidFile = `/tmp/.pi_exec_${randomUUID()}.pid`;
			// setsid gives the command its own process group so timeouts/aborts can kill the whole tree
			// inside the container (killing the local `docker exec` client alone would not).
			const script = `echo $$ > ${pidFile}; ${INIT}; cd ${JSON.stringify(cwd)} || exit 1\n${command}`;
			const child = spawn(DOCKER, ["exec", "-w", cwd, CONTAINER, "setsid", "-w", "bash", "-c", script], {
				stdio: ["ignore", "pipe", "pipe"],
			});
			let timedOut = false;
			let settled = false;
			const killRemote = () => {
				spawn(DOCKER, ["exec", CONTAINER, "sh", "-c", `kill -9 -- -$(cat ${pidFile}) 2>/dev/null; rm -f ${pidFile}`], {
					stdio: "ignore",
				}).on("close", () => child.kill("SIGKILL"));
			};
			const timer =
				timeout && timeout > 0
					? setTimeout(() => {
							timedOut = true;
							killRemote();
						}, timeout * 1000)
					: undefined;
			const onAbort = () => killRemote();
			signal?.addEventListener("abort", onAbort, { once: true });
			child.stdout.on("data", onData);
			child.stderr.on("data", onData);
			const finish = (fn: () => void) => {
				if (settled) return;
				settled = true;
				if (timer) clearTimeout(timer);
				signal?.removeEventListener("abort", onAbort);
				spawn(DOCKER, ["exec", CONTAINER, "rm", "-f", pidFile], { stdio: "ignore" });
				fn();
			};
			child.on("error", (e) => finish(() => reject(e)));
			child.on("close", (code) => {
				finish(() => {
					if (signal?.aborted) reject(new Error("aborted"));
					else if (timedOut) reject(new Error(`timeout:${timeout}`));
					else resolve({ exitCode: code });
				});
			});
		}),
};

// pi spills truncated bash output to a host temp file and tells the model its path; mirror that file into
// the container at the same path so the model can read it with either tool.
const FULL_OUTPUT_RE = /Full output: (\S+?)\]/g;

async function mirrorSpillFiles(text: string): Promise<void> {
	for (const m of text.matchAll(FULL_OUTPUT_RE)) {
		const hostPath = m[1];
		if (!existsSync(hostPath)) continue;
		const dir = hostPath.substring(0, hostPath.lastIndexOf("/")) || "/";
		await dockerRun(["exec", CONTAINER, "mkdir", "-p", dir]);
		await dockerRun(["cp", hostPath, `${CONTAINER}:${hostPath}`]);
	}
}

export default function (pi: ExtensionAPI) {
	if (!CONTAINER) {
		throw new Error("SCITEAM_PI_CONTAINER is not set");
	}
	const read = createReadToolDefinition(WORKDIR, { operations: readOps });
	const write = createWriteToolDefinition(WORKDIR, { operations: writeOps });
	const edit = createEditToolDefinition(WORKDIR, { operations: editOps });
	const bash = createBashToolDefinition(WORKDIR, { operations: bashOps, exposeSessionEnvironment: false });

	pi.registerTool(read);
	pi.registerTool(write);
	pi.registerTool(edit);
	pi.registerTool({
		...bash,
		async execute(id, params, signal, onUpdate, ctx) {
			try {
				const res = await bash.execute(id, params, signal, onUpdate, ctx);
				for (const c of res.content ?? []) {
					if (c.type === "text") await mirrorSpillFiles(c.text);
				}
				return res;
			} catch (e) {
				if (e instanceof Error) await mirrorSpillFiles(e.message);
				throw e;
			}
		},
	});

	// Per-request audit of the wire payload (no message content): verifies thinking params and that
	// prior assistant reasoning_content is replayed.
	const audit = process.env.SCITEAM_PI_PAYLOAD_AUDIT;
	if (audit) {
		pi.on("before_provider_request", async (event) => {
			const p = event.payload as Record<string, any>;
			const msgs: any[] = Array.isArray(p?.messages) ? p.messages : [];
			const asst = msgs.filter((m) => m?.role === "assistant");
			const row = {
				model: p?.model,
				thinking: p?.thinking,
				reasoning_effort: p?.reasoning_effort,
				max_tokens: p?.max_tokens ?? p?.max_completion_tokens,
				tool_choice: p?.tool_choice,
				n_messages: msgs.length,
				n_assistant: asst.length,
				n_assistant_reasoning_nonempty: asst.filter((m) => typeof m.reasoning_content === "string" && m.reasoning_content.length > 0).length,
				n_assistant_reasoning_missing: asst.filter((m) => m.reasoning_content === undefined).length,
			};
			appendFileSync(audit, `${JSON.stringify(row)}\n`);
		});
	}

	pi.on("before_agent_start", async (event) => {
		return {
			systemPrompt: event.systemPrompt.replace(
				`Current working directory: ${process.cwd().replace(/\\/g, "/")}`,
				`Current working directory: ${WORKDIR}`,
			),
		};
	});
}
