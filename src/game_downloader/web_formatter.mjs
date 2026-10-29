import { readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import {
  isMainThread,
  parentPort,
  Worker,
  workerData,
} from "node:worker_threads";

// One worker reuses Prettier across the batch. The main thread can terminate a
// synchronous parser that exceeds the same per-file deadline as the CLI adapter.
if (!isMainThread) {
  const prettier = await import(workerData.prettier_module);
  if (prettier.version !== workerData.expected_version) {
    throw new Error("batch formatter did not load the pinned Prettier version");
  }
  parentPort.on("message", async (item) => {
    try {
      const encoded = await readFile(item.source);
      const source = new TextDecoder("utf-8", {
        fatal: true,
        ignoreBOM: true,
      }).decode(encoded);
      const formatted = await prettier.format(source, {
        parser: item.parser,
        printWidth: workerData.print_width,
        tabWidth: workerData.tab_width,
        endOfLine: "lf",
      });
      const output = Buffer.from(formatted, "utf8");
      if (output.length > workerData.max_output_bytes) {
        throw new Error("web formatter output exceeds policy");
      }
      await writeFile(`${workerData.output_root}/${item.id}`, output, {
        flag: "wx",
        mode: 0o600,
      });
      parentPort.postMessage({
        id: item.id,
        size: output.length,
        sha256: createHash("sha256").update(output).digest("hex"),
      });
    } catch (error) {
      parentPort.postMessage({ error: String(error.message).slice(0, 1024) });
    }
  });
  parentPort.postMessage({ ready: true });
} else {
  const manifest = JSON.parse(await readFile(process.argv[2], "utf8"));
  const worker = new Worker(new URL(import.meta.url), { workerData: manifest });
  function waitForMessage(timeout, label) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => finish(new Error(`${label} timed out`)),
        timeout,
      );
      const onMessage = (value) => finish(null, value);
      const onError = (error) => finish(error);
      const onExit = (code) =>
        finish(new Error(`web formatter worker exited (${code})`));
      function finish(error, value) {
        clearTimeout(timer);
        worker.off("message", onMessage);
        worker.off("error", onError);
        worker.off("exit", onExit);
        if (error) reject(error);
        else resolve(value);
      }
      worker.once("message", onMessage);
      worker.once("error", onError);
      worker.once("exit", onExit);
    });
  }
  try {
    const ready = await waitForMessage(30000, "web formatter startup");
    if (ready.ready !== true)
      throw new Error("web formatter worker did not become ready");
    const outputs = [];
    for (const item of manifest.items) {
      const pending = waitForMessage(
        manifest.timeout_ms,
        `web formatter for ${item.source}`,
      );
      worker.postMessage(item);
      const output = await pending;
      if (output.error)
        throw new Error(
          `web formatter failed for ${item.source}: ${output.error}`,
        );
      outputs.push(output);
    }
    process.stdout.write(JSON.stringify({ outputs }));
  } catch (error) {
    process.stderr.write(
      `${JSON.stringify({ error: String(error.message).slice(0, 2048) })}\n`,
    );
    process.exitCode = 1;
  } finally {
    await worker.terminate();
  }
}
