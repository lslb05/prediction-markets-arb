/**
 * One structured line per event, on stdout.
 *
 * The original build rendered a live dashboard with `console.clear()`. That
 * reads well over a shoulder and is useless afterwards: it overwrites itself,
 * so there is no record of what the bot saw at the moment it decided. A bot
 * that moves money needs the record more than it needs the picture.
 */

type Level = "info" | "warn" | "error";

export type Fields = Record<string, string | number | boolean | null>;

function emit(level: Level, event: string, fields: Fields = {}): void {
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    level,
    event,
    ...fields,
  });
  if (level === "error") {
    process.stderr.write(line + "\n");
  } else {
    process.stdout.write(line + "\n");
  }
}

export const log = {
  info: (event: string, fields?: Fields) => emit("info", event, fields),
  warn: (event: string, fields?: Fields) => emit("warn", event, fields),
  error: (event: string, fields?: Fields) => emit("error", event, fields),
};
