/**
 * room-tools: the only tools a writers-room agent has.
 *
 * The tool list and every rule behind it live in Python (src/room/tools.py). This file asks
 * `room tools <role>` for the role's schemas at startup and runs `room tool <name>` for each call,
 * passing the arguments on stdin. A non-zero exit fails the call, and pi hands the message back to
 * the model so it can fix what it sent.
 */
import { execFile, execFileSync } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const bin: string[] = JSON.parse(process.env.ROOM_BIN ?? "[]");
const role = process.env.ROOM_ROLE ?? "";

type Spec = { name: string; label: string; description: string; parameters: object };

function run(args: string[], input?: string): Promise<string> {
	return new Promise((resolve, reject) => {
		const child = execFile(bin[0], [...bin.slice(1), ...args], { maxBuffer: 16 * 1024 * 1024 }, (err, stdout, stderr) => {
			if (err) reject(new Error((stderr || stdout || String(err)).trim()));
			else resolve(stdout);
		});
		child.stdin?.end(input ?? "");
	});
}

export default function (pi: ExtensionAPI) {
	if (!bin.length || !role) throw new Error("room-tools needs ROOM_BIN and ROOM_ROLE");
	const specs: Spec[] = JSON.parse(execFileSync(bin[0], [...bin.slice(1), "tools", role], { encoding: "utf8" }));
	for (const spec of specs) {
		pi.registerTool({
			name: spec.name,
			label: spec.label,
			description: spec.description,
			parameters: spec.parameters as any,
			async execute(_id: string, params: unknown) {
				const out = await run(["tool", spec.name], JSON.stringify(params ?? {}));
				return { content: [{ type: "text" as const, text: out }], details: {} };
			},
		});
	}
}
