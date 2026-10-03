// nanodot tools — a DSH plugin (topology B: nanodot as a tool surface).
//
// Registration shim only. Everything testable lives in nanodot-tools.cjs.
// Status: written against the plugin API documented in the DeepSeek Harness
// v0.1 developer-preview guide (ctx.tools.register + defineTool, static
// `inject`, function-plugin form). DSH is a preview that expects breaking
// changes — if `defineTool` moves, fix this one import; the tool table and
// the runner do not depend on DSH.

import type { Context } from 'cordis';
import { defineTool } from 'dsh';
import { TOOLS, runTool } from './nanodot-tools.cjs';

export const name = 'nanodot-tools';

export const inject = ['tools'];

export function apply(ctx: Context) {
  for (const tool of TOOLS) {
    ctx.tools.register(defineTool({
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      output: {
        schema: { type: 'string' },
        render: (_args: unknown, value: string) => [{ kind: 'terminal', text: value }],
      },
      async execute(args: Record<string, unknown>, exec: { signal: AbortSignal }) {
        return runTool(tool.name, args, { signal: exec.signal });
      },
    }));
  }
}
