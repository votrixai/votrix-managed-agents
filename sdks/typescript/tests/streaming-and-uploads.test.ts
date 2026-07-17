import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import Votrix, {
  APIConnectionError,
  APIStreamError,
  APITimeoutError,
  AuthenticationError,
  type Fetch,
  type SkillCreateParams,
} from "../src/index.js";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function makeClient(fetch: Fetch, maxRetries = 0): Votrix {
  return new Votrix({
    apiKey: "vma_test_key",
    baseURL: "https://managed-agents.test",
    fetch,
    maxRetries,
  });
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function sseResponse(frames: string): Response {
  const encoded = new TextEncoder().encode(frames);
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoded);
        controller.close();
      },
    }),
    { headers: { "content-type": "text/event-stream" } },
  );
}

describe("uploads and binary responses", () => {
  it("keeps file-only Session resource helpers unambiguous", async () => {
    let body: unknown;
    const fetch: Fetch = async (_input, init) => {
      body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      return jsonResponse({
        id: "resource_1",
        type: "file",
        file_id: "file_1",
      });
    };
    const client = makeClient(fetch);

    await client.sessions.resources.addFile("session_1", {
      file_id: "file_1",
      mount_path: "/mnt/input.txt",
    });

    expect(body).toEqual({
      file_id: "file_1",
      mount_path: "/mnt/input.txt",
      type: "file",
    });
    expect(() =>
      client.sessions.resources.add("session_1", {
        type: "github_repository",
        file_id: "file_1",
      } as never),
    ).toThrow(/only type='file'/);
    expect(() =>
      client.files.list({ after_id: "file_1", before_id: "file_2" }),
    ).toThrow(/either after_id or before_id/);
  });

  it("supports filesystem, archive, and repeated multipart uploads", async () => {
    const directory = await mkdtemp(join(tmpdir(), "votrix-ts-upload-"));
    const source = join(directory, "notes.txt");
    await writeFile(source, "filesystem upload");

    const forms: FormData[] = [];
    const fetch: Fetch = async (_input, init) => {
      if (init?.body instanceof FormData) forms.push(init.body);
      const index = forms.length;
      if (index === 1) return jsonResponse({ id: "file_1", type: "file" }, 201);
      return jsonResponse({ id: `skill_${index}`, type: "skill" }, 201);
    };
    const client = makeClient(fetch);

    await client.files.upload({ file: source });
    await client.skills.create({
      display_title: "Archive skill",
      archive: new Uint8Array([80, 75, 3, 4]),
    });
    await client.skills.create({
      display_title: "Multipart skill",
      files: [
        {
          data: new Uint8Array([1]),
          filename: "skill/SKILL.md",
          mime_type: "text/markdown",
        },
        {
          data: new Uint8Array([2]),
          filename: "skill/helper.js",
          mime_type: "text/javascript",
        },
      ],
    });

    expect(forms).toHaveLength(3);
    const uploaded = forms[0]?.get("file");
    expect(uploaded).toBeInstanceOf(File);
    expect((uploaded as File).name).toBe("notes.txt");
    expect((uploaded as File).type).toBe("text/plain");
    expect(await (uploaded as File).text()).toBe("filesystem upload");

    const archive = forms[1]?.get("files");
    expect(archive).toBeInstanceOf(File);
    expect((archive as File).name).toBe("skill.zip");
    expect((archive as File).type).toBe("application/zip");
    expect(forms[1]?.get("display_title")).toBe("Archive skill");

    const multipartFiles = forms[2]?.getAll("files") ?? [];
    expect(multipartFiles).toHaveLength(2);
    expect(multipartFiles.map((item) => (item as File).name)).toEqual([
      "skill/SKILL.md",
      "skill/helper.js",
    ]);
  });

  it("rejects missing, conflicting, and mixed skill content before fetch", () => {
    const fetch = vi.fn<Fetch>();
    const client = makeClient(fetch);

    expect(() => client.skills.create({} as SkillCreateParams)).toThrow(
      /requires archive or files/,
    );
    expect(() =>
      client.skills.create({
        archive: new Uint8Array([1]),
        files: [{ filename: "skill/SKILL.md", content: "content" }],
      } as unknown as SkillCreateParams),
    ).toThrow(/either archive or files/);
    expect(() =>
      client.skills.create({
        files: [
          { filename: "skill/SKILL.md", content: "content" },
          { data: new Uint8Array([1]), filename: "skill/helper.js" },
        ],
      } as unknown as SkillCreateParams),
    ).toThrow(/all JSON file objects or all multipart uploads/);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("streams downloads, exposes metadata, and writes incrementally to disk", async () => {
    let call = 0;
    const fetch: Fetch = async () => {
      call += 1;
      const chunks = [
        new TextEncoder().encode("streamed "),
        new TextEncoder().encode("content"),
      ];
      return new Response(
        new ReadableStream<Uint8Array>({
          pull(controller) {
            const chunk = chunks.shift();
            if (chunk) controller.enqueue(chunk);
            else controller.close();
          },
        }),
        {
          headers: {
            "content-type": "text/plain",
            "content-disposition":
              "attachment; filename*=UTF-8''hello%20world.txt",
          },
        },
      );
    };
    const client = makeClient(fetch);

    const first = await client.files.download("file_1");
    expect(first.filename).toBe("hello world.txt");
    expect(first.contentType).toBe("text/plain");
    const chunks = [];
    for await (const chunk of first.iterBytes())
      chunks.push(new TextDecoder().decode(chunk));
    expect(chunks.join("")).toBe("streamed content");

    const directory = await mkdtemp(join(tmpdir(), "votrix-ts-download-"));
    const destination = join(directory, "download.txt");
    const second = await client.files.download("file_1", { stream: true });
    expect(await second.writeToFile(destination)).toBe(destination);
    expect(await readFile(destination, "utf8")).toBe("streamed content");
    expect(call).toBe(2);
  });

  it("cancels an incremental body when iteration ends early", async () => {
    const cancelled = vi.fn();
    const fetch: Fetch = async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array([1]));
            controller.enqueue(new Uint8Array([2]));
          },
          cancel: cancelled,
        }),
      );
    const response = await makeClient(fetch).files.download("file_1");

    for await (const _chunk of response.iterBytes()) break;

    expect(cancelled).toHaveBeenCalledTimes(1);
  });

  it("times out or aborts a stalled binary body", async () => {
    vi.useFakeTimers();
    const timeoutCancelled = vi.fn();
    const timeoutClient = new Votrix({
      apiKey: "vma_test_key",
      baseURL: "https://managed-agents.test",
      timeout: 25,
      maxRetries: 0,
      fetch: async () =>
        new Response(
          new ReadableStream<Uint8Array>({ cancel: timeoutCancelled }),
        ),
    });
    const timedResponse = await timeoutClient.files.download("file_1");
    const timedOut = expect(timedResponse.bytes()).rejects.toBeInstanceOf(
      APITimeoutError,
    );
    await vi.advanceTimersByTimeAsync(25);
    await timedOut;
    expect(timeoutCancelled).toHaveBeenCalledTimes(1);

    const abortCancelled = vi.fn();
    const controller = new AbortController();
    const abortClient = new Votrix({
      apiKey: "vma_test_key",
      baseURL: "https://managed-agents.test",
      timeout: 0,
      maxRetries: 0,
      fetch: async () =>
        new Response(
          new ReadableStream<Uint8Array>({ cancel: abortCancelled }),
        ),
    });
    const abortedResponse = await abortClient.files.download(
      "file_1",
      {},
      { signal: controller.signal },
    );
    const aborted = expect(abortedResponse.bytes()).rejects.toBeInstanceOf(
      APIConnectionError,
    );
    controller.abort(new Error("stop download"));
    await aborted;
    expect(abortCancelled).toHaveBeenCalledTimes(1);
  });
});

describe("Session event streams", () => {
  it("parses frames and reconnects with Last-Event-ID while suppressing replay duplicates", async () => {
    const lastEventIDs: Array<string | null> = [];
    let call = 0;
    const fetch: Fetch = async (_input, init) => {
      call += 1;
      lastEventIDs.push(new Headers(init?.headers).get("last-event-id"));
      if (call === 1) {
        return sseResponse(
          ": ping\n" +
            "id: 1\n" +
            "event: agent.message\n" +
            "retry: 0\n" +
            'data: {"id":"event_1","type":"agent.message",\n' +
            'data: "seq":1}\n\n',
        );
      }
      return sseResponse(
        'id: 1\nevent: agent.message\ndata: {"id":"event_1","type":"agent.message","seq":1}\n\n' +
          'id: 2\nevent: session.status_idle\ndata: {"id":"event_2","type":"session.status_idle","seq":2}',
      );
    };
    const client = makeClient(fetch);
    const stream = await client.sessions.events.stream("session_1", {
      last_event_id: "0",
      max_reconnects: 1,
    });

    const events = [];
    for await (const event of stream) events.push(event);

    expect(events.map((event) => [event.sse_id, event.id, event.seq])).toEqual([
      ["1", "event_1", 1],
      ["2", "event_2", 2],
    ]);
    expect(lastEventIDs).toEqual(["0", "1"]);
  });

  it("raises typed in-stream and HTTP errors", async () => {
    const streamErrorClient = makeClient(async () =>
      sseResponse(
        'event: error\nid: request_stream\ndata: {"type":"error","error":{"type":"runtime_error","message":"stream failed"}}\n\n',
      ),
    );
    const stream = await streamErrorClient.sessions.events.stream("session_1", {
      max_reconnects: 0,
    });

    await expect(collect(stream)).rejects.toMatchObject({
      name: "APIStreamError",
      message: "stream failed",
      errorType: "runtime_error",
      requestID: "request_stream",
    });
    await expect(collect(stream)).rejects.toThrow(/only be iterated once/);

    const authClient = makeClient(async () =>
      jsonResponse(
        { error: { type: "authentication_error", message: "invalid key" } },
        401,
      ),
    );
    const unauthorized = await authClient.sessions.events.stream("session_1", {
      max_reconnects: 2,
    });
    await expect(collect(unauthorized)).rejects.toBeInstanceOf(
      AuthenticationError,
    );

    const disconnected = await makeClient(async () => {
      throw new Error("network unavailable");
    }).sessions.events.stream("session_1", { max_reconnects: 0 });
    await expect(collect(disconnected)).rejects.toBeInstanceOf(
      APIConnectionError,
    );
  });

  it("close aborts a pending body and ends iteration without reconnecting", async () => {
    const cancelled = vi.fn();
    const fetch = vi.fn<Fetch>(async () => {
      const encoded = new TextEncoder().encode(
        'id: 1\nevent: agent.message\ndata: {"id":"event_1","type":"agent.message"}\n\n',
      );
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoded);
          },
          cancel: cancelled,
        }),
        { headers: { "content-type": "text/event-stream" } },
      );
    });
    const stream = await makeClient(fetch).sessions.events.stream("session_1", {
      max_reconnects: 3,
    });
    const iterator = stream[Symbol.asyncIterator]();

    expect((await iterator.next()).value).toMatchObject({
      id: "event_1",
      sse_id: "1",
    });
    await stream.close();
    expect((await iterator.next()).done).toBe(true);
    expect(cancelled).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const values: T[] = [];
  for await (const value of iterable) values.push(value);
  return values;
}

// Keep the class referenced so its public export is type-checked by this test.
void APIStreamError;
