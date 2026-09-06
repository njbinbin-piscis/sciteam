// Executes one pinned-pi built-in tool for real, offline, with no LLM.
//
// WP-R2-2b needs behavioural evidence that the pi arm's `--tools` names are
// tools that actually work — pi accepts unknown names in `--tools` without
// complaint, so the allowlist itself proves nothing. This driver imports the
// same tool factories the pi CLI uses and runs one against a real directory.
//
// usage: node pi_tool_probe.mjs <pi-package-dir> <cwd> <toolName> <argsJson>
// stdout: {"ok": bool, "text": string, "isError": bool} or {"ok": false, "error": ...}

import { pathToFileURL } from "node:url";
import { join } from "node:path";

const [pkgDir, cwd, toolName, argsJson] = process.argv.slice(2);

function emit(payload) {
	process.stdout.write(JSON.stringify(payload));
}

try {
	const toolsUrl = pathToFileURL(join(pkgDir, "dist/core/tools/index.js")).href;
	const { createTool } = await import(toolsUrl);
	const tool = createTool(toolName, cwd);
	// pi's bash tool stamps PI_SESSION_ID / PI_SESSION_FILE into the child
	// env from the live session manager; a probe has no session, so supply
	// the same shape with placeholder values.
	const ctx = {
		cwd,
		model: undefined,
		thinkingLevel: undefined,
		sessionManager: {
			getSessionId: () => "probe-session",
			getSessionFile: () => null,
		},
	};
	const result = await tool.execute("probe-call", JSON.parse(argsJson), undefined, undefined, ctx);
	const blocks = Array.isArray(result?.content) ? result.content : [];
	const text = blocks
		.filter((b) => b && b.type === "text")
		.map((b) => String(b.text ?? ""))
		.join("\n");
	emit({ ok: true, text, isError: Boolean(result?.isError) });
} catch (error) {
	emit({ ok: false, error: String(error && error.message ? error.message : error) });
}
