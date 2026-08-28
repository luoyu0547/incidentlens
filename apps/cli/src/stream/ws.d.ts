/**
 * Minimal type declarations for the `ws` package.
 *
 * `@types/ws` is not installed in this workspace (the root lockfile is
 * intentionally frozen for this task), so we declare the subset of the
 * `ws` API the WebSocket transport actually uses. The surface matches
 * `ws@8.x` (Node-style constructor options + EventEmitter events).
 */

declare module 'ws' {
  export interface WebSocketClientOptions {
    headers?: Record<string, string>;
    handshakeTimeout?: number;
    maxPayload?: number;
    [key: string]: unknown;
  }

  export interface WebSocketServerOptions {
    host?: string;
    port?: number;
    path?: string;
    noServer?: boolean;
    [key: string]: unknown;
  }

  export class WebSocket {
    constructor(url: string, options?: WebSocketClientOptions);
    readonly readyState: number;
    readonly url: string;
    readonly protocol: string;
    readonly bufferedAmount: number;

    static readonly CONNECTING: 0;
    static readonly OPEN: 1;
    static readonly CLOSING: 2;
    static readonly CLOSED: 3;

    on(event: string, listener: (...args: unknown[]) => void): this;
    on(event: 'open', listener: () => void): this;
    on(event: 'message', listener: (data: unknown, isBinary: boolean) => void): this;
    on(event: 'error', listener: (error: Error) => void): this;
    on(event: 'close', listener: (code: number, reason: Buffer) => void): this;

    once(event: string, listener: (...args: unknown[]) => void): this;

    send(data: string | Buffer | ArrayBuffer | Buffer[]): void;
    close(code?: number, reason?: string): void;
    terminate(): void;

    addEventListener(event: string, listener: (event: unknown) => void): void;
    removeEventListener(event: string, listener: (...args: unknown[]) => void): void;

    removeAllListeners(): void;
  }

  export interface WebSocketServerClient extends WebSocket {
    upgradeReq?: unknown;
  }

  export class WebSocketServer {
    constructor(options?: WebSocketServerOptions);
    on(event: string, listener: (...args: unknown[]) => void): this;
    on(
      event: 'connection',
      listener: (socket: WebSocketServerClient, request: unknown) => void,
    ): this;
    on(
      event: 'upgrade',
      listener: (request: unknown, socket: unknown, head: unknown) => void,
    ): this;
    emit(event: string, ...args: unknown[]): boolean;
    handleUpgrade(
      request: unknown,
      socket: unknown,
      head: unknown,
      callback: (socket: WebSocketServerClient) => void,
    ): void;
    close(callback?: () => void): void;
    clients: Set<WebSocketServerClient>;
    address(): { address: string; family: string; port: number } | string;
  }

  export default WebSocket;
}