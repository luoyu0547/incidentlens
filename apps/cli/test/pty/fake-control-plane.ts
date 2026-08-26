import { createServer, type IncomingMessage, type Server } from 'node:http';
import { WebSocketServer, type WebSocket } from 'ws';

export interface FakeControlPlane {
  readonly url: string;
  readonly token: string;
  readonly cancelCalls: string[];
  readonly secret: string;
  readonly closeStream: () => void;
  readonly stop: () => Promise<void>;
}

export async function startFakeControlPlane(): Promise<FakeControlPlane> {
  const token = 'pty-secret-token';
  const secret = 'server-only-secret';
  const cancelCalls: string[] = [];
  const sessions = new Map<string, { messages: Array<Record<string, unknown>> }>();
  const server: Server = createServer(async (req, res) => {
    const body = async (): Promise<Record<string, unknown>> => {
      let raw = '';
      for await (const chunk of req) raw += chunk;
      return raw ? JSON.parse(raw) as Record<string, unknown> : {};
    };
    const path = new URL(req.url ?? '/', 'http://127.0.0.1').pathname;
    res.setHeader('content-type', 'application/json');
    if (req.headers.authorization !== `Bearer ${token}`) {
      res.writeHead(401); res.end(JSON.stringify({ message: 'unauthorized' })); return;
    }
    if (path === '/api/v1/version') { res.end(JSON.stringify({ protocol_version: '1.0.0', minimum_cli_protocol_version: '1.0.0' })); return; }
    if (path === '/api/v1/principal') { res.end(JSON.stringify({ principal_id: 'pty-user', display_name: 'PTY User', scopes: ['agent:read', 'agent:write'] })); return; }
    if (path === '/api/v1/targets') { res.end(JSON.stringify([{ target_id: 'target-1', name: 'demo', kind: 'ssh', status: 'ready', host: '127.0.0.1' }])); return; }
    if (path === '/api/v1/agent-sessions' && req.method === 'GET') { res.end(JSON.stringify([])); return; }
    if (path === '/api/v1/agent-sessions' && req.method === 'POST') {
      const id = 'session-1'; sessions.set(id, { messages: [] });
      res.end(JSON.stringify({ session_id: id, target_id: 'target-1', title: 'PTY session', status: 'idle', created_at: new Date().toISOString(), updated_at: new Date().toISOString() })); return;
    }
    const match = path.match(/^\/api\/v1\/agent-sessions\/([^/]+)(?:\/(messages|cancel|resume))?$/);
    if (match) {
      const id = match[1]; const session = sessions.get(id) ?? { messages: [] }; sessions.set(id, session);
      if (match[2] === 'messages' && req.method === 'POST') {
        const input = await body(); session.messages.push({ message_id: `message-${session.messages.length + 1}`, role: 'user', content: String(input.content ?? ''), created_at: new Date().toISOString() });
        res.end(JSON.stringify({ message_id: 'message-accepted', operation_id: 'operation-1' })); return;
      }
      if (match[2] === 'cancel') { cancelCalls.push(id); res.end(JSON.stringify({ operation_id: 'operation-cancel', status: 'cancelled' })); return; }
      if (match[2] === 'resume') { res.end(JSON.stringify({ operation_id: 'operation-resume', status: 'queued' })); return; }
      if (req.method === 'GET') { res.end(JSON.stringify({ session_id: id, target_id: 'target-1', title: 'PTY session', status: 'running', created_at: new Date().toISOString(), updated_at: new Date().toISOString() })); return; }
    }
    if (path === '/api/v1/approvals') { res.end(JSON.stringify({ items: [], total: 0, limit: 500, offset: 0 })); return; }
    res.writeHead(404); res.end(JSON.stringify({ message: 'not found' }));
  });
  const wss = new WebSocketServer({ noServer: true });
  let active: WebSocket | undefined;
  wss.on('connection', (socket) => {
    active = socket;
    socket.send(JSON.stringify({ schema_version: 1, event_type: 'stream.hello', session_id: 'session-1', sequence: 0, payload: {} }));
    socket.send(JSON.stringify({ schema_version: 1, event_type: 'agent.text.delta', session_id: 'session-1', sequence: 1, payload: { text: '连接成功' } }));
  });
  server.on('upgrade', (req, socket, head) => {
    if (req.headers.authorization !== `Bearer ${token}`) { socket.destroy(); return; }
    wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const port = typeof address === 'object' && address ? address.port : 0;
  return { url: `http://127.0.0.1:${port}`, token, secret, cancelCalls, closeStream: () => active?.close(1012, 'forced reconnect'), stop: async () => { wss.close(); await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())); } };
}
