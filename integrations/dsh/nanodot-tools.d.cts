// Type declarations for nanodot-tools.cjs, so the Cordis shim (index.ts)
// imports the runner with full types under strict module resolution.

export interface ToolSpec {
  name: string;
  description: string;
  parameters: Record<string, {
    type: 'string' | 'number';
    required?: boolean;
    enum?: string[];
    description?: string;
  }>;
  argv: (args: Record<string, unknown>) => string[];
}

export interface RunOptions {
  binary?: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  killGraceMs?: number;
  env?: Record<string, string>;
}

export declare class NanodotToolError extends Error {
  code: string;
  detail: string;
}

export declare const TOOLS: ToolSpec[];
export declare function runTool(
  name: string,
  args: Record<string, unknown> | undefined,
  options?: RunOptions,
): Promise<string>;
export declare function runNanodot(argv: string[], options?: RunOptions): Promise<string>;
export declare function toolByName(name: string): ToolSpec;
