/* eslint-disable no-useless-escape */
import type { CertNode, DnsChannel, Domain, Job, Settings, SystemEvent, NodeAssignment, NodeDetailResponse } from "../types/api";

// In-memory Mock Store
let domains: Domain[] = [
  { id: 'd1', domain: 'example.com', enabled: true, dnsChannelId: 'c1', expiresAt: new Date(Date.now() + 86400000 * 30).toISOString(), daysRemaining: 30, lastIssuedAt: new Date(Date.now() - 86400000 * 60).toISOString(), lastSyncAt: new Date().toISOString(), certSha256: 'a1b2c3d4e5f6', status: 'active' },
  { id: 'd2', domain: 'test.io', enabled: true, dnsChannelId: 'c2', expiresAt: new Date(Date.now() + 86400000 * 5).toISOString(), daysRemaining: 5, lastIssuedAt: new Date(Date.now() - 86400000 * 85).toISOString(), lastSyncAt: new Date().toISOString(), certSha256: 'b2c3d4e5f6g7', status: 'expiring' },
];

let nodes: CertNode[] = [
  { id: 'n1', name: 'web-node-01', ip: '192.168.1.10', isOnline: true, lastHeartbeatAt: new Date().toISOString(), certDir: '/etc/nginx/ssl', assignedDomainsCount: 2, lastError: null },
  { id: 'n2', name: 'db-node-01', ip: '192.168.1.11', isOnline: false, lastHeartbeatAt: new Date(Date.now() - 86400000).toISOString(), certDir: '/etc/nginx/ssl', assignedDomainsCount: 1, lastError: 'Connection timeout' },
];

let nodeAssignments: NodeAssignment[] = [
  { id: 'a1', nodeId: 'n1', domainId: 'd1', domainName: 'example.com', desiredSha256: 'a1b2c3d4e5f6', deployedSha256: 'a1b2c3d4e5f6', status: 'synced', lastDeployAt: new Date().toISOString(), expiresAt: null, lastError: null },
  { id: 'a2', nodeId: 'n1', domainId: 'd2', domainName: 'test.io', desiredSha256: 'b2c3d4e5f6g7', deployedSha256: 'b2c3d4e5f6g7', status: 'synced', lastDeployAt: new Date().toISOString(), expiresAt: null, lastError: null },
  { id: 'a3', nodeId: 'n2', domainId: 'd1', domainName: 'example.com', desiredSha256: 'a1b2c3d4e5f6', deployedSha256: null, status: 'pending', lastDeployAt: null, expiresAt: null, lastError: null }
];

let channels: DnsChannel[] = [
  { id: 'c1', name: 'Cloudflare Default', provider: 'dns_cf', credentials: { CF_Token: '***', CF_Key: '***', CF_Email: '***' }, createdAt: new Date().toISOString() },
  { id: 'c2', name: 'Aliyun Main', provider: 'dns_ali', credentials: { Ali_Key: '***', Ali_Secret: '***' }, createdAt: new Date().toISOString() },
];

const jobs: Job[] = [
  { id: 'j1', type: 'issue', targetId: 'd1', targetName: 'example.com', status: 'success', startedAt: new Date(Date.now() - 60000).toISOString(), endedAt: new Date(Date.now() - 10000).toISOString(), durationMs: 50000, error: null },
  { id: 'j2', type: 'sync', targetId: 'n2', targetName: 'db-node-01', status: 'failed', startedAt: new Date(Date.now() - 3600000).toISOString(), endedAt: new Date(Date.now() - 3590000).toISOString(), durationMs: 10000, error: 'WebDAV upload failed' },
];

let settings: Settings = {
  webdav: { url: 'https://dav.example.com', auth: 'user:pass' },
  telegram: { botToken: '123:abc', chatId: '-100123' },
  acme: { acmeHome: '/root/.acme.sh', stagingBase: "/tmp/acme_staging", defaultRenewDays: 20, defaultCa: 'letsencrypt', accountEmail: '' },
  node: { publicBaseUrl: 'https://ssl.example.com' },
};

const MOCK_AUTH_STORAGE_KEY = "ssl-sync-mock-auth";

interface MockAuthState {
  initialized: boolean;
  username: string;
  password: string;
  token: string;
}

const mockAuthDefaults: MockAuthState = {
  initialized: false,
  username: "",
  password: "",
  token: "mock-jwt-token",
};

function loadMockAuth() {
  try {
    const raw = localStorage.getItem(MOCK_AUTH_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<MockAuthState>;
    if (typeof parsed.initialized !== "boolean") {
      return null;
    }
    return {
      ...mockAuthDefaults,
      ...parsed,
    };
  } catch {
    return null;
  }
}

function persistMockAuth() {
  localStorage.setItem(MOCK_AUTH_STORAGE_KEY, JSON.stringify(mockAuth));
}

let mockAuth = loadMockAuth() ?? { ...mockAuthDefaults };

export const eventStreamSubscribers: Array<(event: SystemEvent) => void> = [];

export function emitMockEvent(event: Omit<SystemEvent, 'id' | 'createdAt'>) {
  const fullEvent: SystemEvent = {
    ...event,
    id: `evt_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
    createdAt: new Date().toISOString()
  };
  eventStreamSubscribers.forEach(sub => sub(fullEvent));
}

setInterval(() => {
  if (Math.random() > 0.7) {
    emitMockEvent({
      type: 'node_heartbeat',
      level: 'info',
      message: 'Node web-node-01 heartbeat received',
      payload: { nodeId: 'n1' }
    });
  }
}, 15000);

// Fetch Interceptor
const originalFetch = window.fetch;

window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
  
  if (!url.includes('/api/admin') && !url.includes('/api/auth')) {
    return originalFetch(input, init);
  }

  console.log(`[Mock] ${init?.method || 'GET'} ${url}`);
  
  const method = init?.method || 'GET';
  const delay = () => new Promise(resolve => setTimeout(resolve, 300 + Math.random() * 300));
  await delay();

  const createResponse = (body: unknown, status = 200) => {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' }
    });
  };

  const getBody = () => {
    try {
      return init?.body ? JSON.parse(init.body as string) : {};
    } catch { return {}; }
  };

  // Auth
  if (url.includes('/api/auth/status') && method === 'GET') {
    return createResponse({
      initialized: mockAuth.initialized,
      setupRequired: !mockAuth.initialized,
    });
  }

  if (url.includes('/api/auth/bootstrap') && method === 'POST') {
    if (mockAuth.initialized) {
      return createResponse({ error: 'Initial setup has already been completed', setupRequired: false }, 409);
    }
    const { username, password } = getBody();
    const trimmedUsername = typeof username === 'string' ? username.trim() : '';
    const rawPassword = typeof password === 'string' ? password : '';
    if (!trimmedUsername) {
      return createResponse({ error: 'Username is required' }, 400);
    }
    if (rawPassword.length < 8) {
      return createResponse({ error: 'Password must be at least 8 characters long' }, 400);
    }
    mockAuth = {
      ...mockAuth,
      initialized: true,
      username: trimmedUsername,
      password: rawPassword,
    };
    persistMockAuth();
    return createResponse({ token: mockAuth.token });
  }

  if (url.includes('/api/auth/login') && method === 'POST') {
    if (!mockAuth.initialized) {
      return createResponse({ error: 'Initial setup is required', setupRequired: true }, 409);
    }
    const { username, password } = getBody();
    if (username === mockAuth.username && password === mockAuth.password) {
      return createResponse({ token: mockAuth.token });
    }
    return createResponse({ error: 'Invalid credentials' }, 401);
  }

  // Check auth
  const authHeader = (init?.headers as Record<string, string>)?.['Authorization'] || '';
  if (!authHeader.startsWith(`Bearer ${mockAuth.token}`) && !url.includes('/auth/login') && !url.includes('/auth/status') && !url.includes('/auth/bootstrap')) {
    return createResponse({ error: 'Unauthorized' }, 401);
  }

  try {
    if (url.includes('/api/auth/account') && method === 'GET') {
      return createResponse({ username: mockAuth.username });
    }
    if (url.includes('/api/auth/account') && method === 'PATCH') {
      const { username, currentPassword, newPassword } = getBody() as {
        username?: string;
        currentPassword?: string;
        newPassword?: string;
      };
      const trimmedUsername = typeof username === 'string' ? username.trim() : '';
      if (!trimmedUsername) {
        return createResponse({ error: 'Username is required' }, 400);
      }
      if (!currentPassword) {
        return createResponse({ error: 'Current password is required' }, 400);
      }
      if (currentPassword !== mockAuth.password) {
        return createResponse({ error: 'Current password is incorrect' }, 401);
      }
      if (!newPassword && trimmedUsername === mockAuth.username) {
        return createResponse({ error: 'No account changes were provided' }, 400);
      }
      if (newPassword && newPassword.length < 8) {
        return createResponse({ error: 'New password must be at least 8 characters long' }, 400);
      }
      mockAuth = {
        ...mockAuth,
        username: trimmedUsername,
        password: typeof newPassword === 'string' && newPassword.length > 0 ? newPassword : mockAuth.password,
      };
      persistMockAuth();
      return createResponse({ username: mockAuth.username });
    }

    // Overview
    if (url.endsWith('/api/admin/overview') && method === 'GET') {
      return createResponse({
        stats: {
          onlineNodes: nodes.filter(n => n.isOnline).length,
          totalNodes: nodes.length,
          totalDomains: domains.length,
          expiringSoon: domains.filter(d => (d.daysRemaining || 0) <= 7).length,
          failedJobs: jobs.filter(j => j.status === 'failed').length,
        },
        certificates: domains,
        nodes: nodes,
        recentEvents: []
      });
    }

    // Domains
    if (url.match(new RegExp('/api/admin/domains$'))) {
      if (method === 'GET') return createResponse(domains);
      if (method === 'POST') {
        const d: Domain = { id: `d${Date.now()}`, ...getBody(), daysRemaining: 90, status: 'active', lastIssuedAt: null, lastSyncAt: null, expiresAt: null, certSha256: null, enabled: true };
        domains.push(d);
        return createResponse(d);
      }
    }
    const domainMatch = url.match(/\/api\/admin\/domains\/([^\/]+)$/);
    if (domainMatch) {
      const id = domainMatch[1];
      if (method === 'PATCH') {
        domains = domains.map(d => d.id === id ? { ...d, ...getBody() } : d);
        return createResponse(domains.find(d => d.id === id));
      }
      if (method === 'DELETE') {
        domains = domains.filter(d => d.id !== id);
        return createResponse({ success: true });
      }
    }
    if (url.endsWith('/api/admin/domains/bulk-action') && method === 'POST') {
      const body = getBody() as { ids?: string[]; action?: 'issue' | 'renew' | 'sync' };
      const ids = Array.isArray(body.ids) ? body.ids : [];
      const action = body.action;
      if (!action || ids.length === 0) {
        return createResponse({ error: 'Invalid bulk request' }, 400);
      }

      const targetDomains = domains.filter((domain) => ids.includes(domain.id));
      const newJob: Job = {
        id: `j${Date.now()}`,
        type: action,
        targetId: 'bulk',
        targetName: `${targetDomains.length} domains`,
        status: 'running',
        startedAt: new Date().toISOString(),
        endedAt: null,
        durationMs: null,
        error: null
      };
      jobs.unshift(newJob);

      emitMockEvent({
        type: 'job_started',
        level: 'info',
        message: `Started bulk ${action} for ${targetDomains.length} domains`,
        payload: { jobId: newJob.id }
      });

      setTimeout(() => {
        const jobIndex = jobs.findIndex((job) => job.id === newJob.id);
        if (jobIndex > -1) {
          jobs[jobIndex].status = 'success';
          jobs[jobIndex].endedAt = new Date().toISOString();
          jobs[jobIndex].durationMs = 3500;
        }

        ids.forEach((id) => {
          const domainIndex = domains.findIndex((domain) => domain.id === id);
          if (domainIndex === -1) return;
          if (action === 'issue' || action === 'renew') {
            domains[domainIndex].certSha256 = Math.random().toString(16).substring(2, 14);
            domains[domainIndex].lastIssuedAt = new Date().toISOString();
            domains[domainIndex].daysRemaining = 90;
            domains[domainIndex].status = 'active';
          }
          if (action === 'sync') {
            domains[domainIndex].lastSyncAt = new Date().toISOString();
          }
        });

        emitMockEvent({
          type: 'job_finished',
          level: 'success',
          message: `Completed bulk ${action} for ${targetDomains.length} domains`,
          payload: { jobId: newJob.id }
        });
      }, 4000);

      return createResponse(newJob);
    }
    const domainActionMatch = url.match(/\/api\/admin\/domains\/([^/]+)\/(issue|renew|sync)$/);
    if (domainActionMatch && method === 'POST') {
      const id = domainActionMatch[1];
      const action = domainActionMatch[2];
      const d = domains.find(x => x.id === id);
      
      const newJob: Job = {
        id: `j${Date.now()}`,
        type: action as 'issue' | 'renew' | 'sync',
        targetId: id,
        targetName: d?.domain,
        status: 'running',
        startedAt: new Date().toISOString(),
        endedAt: null,
        durationMs: null,
        error: null
      };
      jobs.unshift(newJob);
      
      emitMockEvent({
        type: 'job_started',
        level: 'info',
        message: `Started ${action} for ${d?.domain}`,
        payload: { jobId: newJob.id }
      });

      setTimeout(() => {
        const jobIndex = jobs.findIndex(j => j.id === newJob.id);
        if (jobIndex > -1) {
          jobs[jobIndex].status = 'success';
          jobs[jobIndex].endedAt = new Date().toISOString();
          jobs[jobIndex].durationMs = 2500;
        }
        if (action === 'issue' || action === 'renew') {
          const domainIndex = domains.findIndex(x => x.id === id);
          if (domainIndex > -1) {
            domains[domainIndex].certSha256 = Math.random().toString(16).substring(2, 14);
            domains[domainIndex].lastIssuedAt = new Date().toISOString();
            domains[domainIndex].daysRemaining = 90;
            domains[domainIndex].status = 'active';
          }
        }
        if (action === 'sync') {
          const domainIndex = domains.findIndex(x => x.id === id);
          if (domainIndex > -1) domains[domainIndex].lastSyncAt = new Date().toISOString();
        }

        emitMockEvent({
          type: 'job_finished',
          level: 'success',
          message: `Completed ${action} for ${d?.domain}`,
          payload: { jobId: newJob.id }
        });
      }, 4000);

      return createResponse(newJob);
    }

    // DNS Channels
    if (url.match(new RegExp('/api/admin/dns-channels$'))) {
      if (method === 'GET') return createResponse(channels);
      if (method === 'POST') {
        const body = getBody();
        const credentials = { ...body.credentials };
        Object.keys(credentials).forEach(k => credentials[k] = credentials[k] ? '***' : credentials[k]);
        const c: DnsChannel = { id: `c${Date.now()}`, createdAt: new Date().toISOString(), ...body, credentials };
        channels.push(c);
        return createResponse(c);
      }
    }
    const channelMatch = url.match(/\/api\/admin\/dns-channels\/([^\/]+)$/);
    if (channelMatch) {
      const id = channelMatch[1];
      if (method === 'PATCH') {
        const body = getBody();
        if (body.credentials) {
           Object.keys(body.credentials).forEach(k => body.credentials[k] = body.credentials[k] ? '***' : body.credentials[k]);
        }
        channels = channels.map(c => c.id === id ? { ...c, ...body } : c);
        return createResponse(channels.find(c => c.id === id));
      }
      if (method === 'DELETE') {
        channels = channels.filter(c => c.id !== id);
        return createResponse({ success: true });
      }
    }
    const channelTestMatch = url.match(/\/api\/admin\/dns-channels\/([^/]+)\/test$/);
    if (channelTestMatch && method === 'POST') {
      const id = channelTestMatch[1];
      const ch = channels.find(c => c.id === id);
      const newJob: Job = { id: `j${Date.now()}`, type: 'test_dns', targetId: id, targetName: ch?.name, status: 'success', startedAt: new Date().toISOString(), endedAt: new Date().toISOString(), durationMs: 1200, error: null };
      jobs.unshift(newJob);
      return createResponse({ success: true });
    }

    // Nodes
    if (url.match(new RegExp('/api/admin/nodes$'))) {
      if (method === 'GET') return createResponse(nodes);
      if (method === 'POST') {
        const body = getBody() as { name?: string; ip?: string; certDir?: string };
        const n: CertNode = {
          id: `n${Date.now()}`,
          name: body.name || 'node',
          ip: body.ip || '',
          certDir: body.certDir || '/etc/nginx/ssl',
          isOnline: false,
          lastHeartbeatAt: null,
          assignedDomainsCount: 0,
          lastError: null,
        };
        nodes.push(n);
        return createResponse({ ...n, token: `eyMockToken_${n.id}`, certDir: n.certDir });
      }
    }
    const nodeMatch = url.match(/\/api\/admin\/nodes\/([^\/]+)$/);
    if (nodeMatch) {
      const id = nodeMatch[1];
      if (method === 'GET') {
        const node = nodes.find(n => n.id === id);
        if (!node) return createResponse({ error: 'Not found' }, 404);
        const nodeAsgmts = nodeAssignments.filter(a => a.nodeId === id);
        const res: NodeDetailResponse = {
          ...node,
          assignments: nodeAsgmts,
          recentEvents: []
        };
        return createResponse(res);
      }
      if (method === 'PATCH') {
        nodes = nodes.map(n => n.id === id ? { ...n, ...getBody() } : n);
        return createResponse(nodes.find(n => n.id === id));
      }
      if (method === 'DELETE') {
        nodes = nodes.filter(n => n.id !== id);
        return createResponse({ success: true });
      }
    }
    const nodeAssignmentsMatch = url.match(/\/api\/admin\/nodes\/([^/]+)\/assignments$/);
    if (nodeAssignmentsMatch && method === 'PUT') {
      const id = nodeAssignmentsMatch[1];
      const body = getBody();
      if (!Array.isArray(body.domainIds)) return createResponse({ error: 'Invalid domainIds' }, 400);
      
      // Update assignments
      nodeAssignments = nodeAssignments.filter(a => a.nodeId !== id);
      body.domainIds.forEach((domainId: string) => {
        const dom = domains.find(d => d.id === domainId);
        nodeAssignments.push({
          id: `a${Date.now()}_${Math.random()}`,
          nodeId: id,
          domainId: domainId,
          domainName: dom?.domain,
          desiredSha256: dom?.certSha256 || null,
          deployedSha256: null,
          status: 'pending',
          lastDeployAt: null,
          expiresAt: null,
          lastError: null
        });
      });
      
      const node = nodes.find(n => n.id === id);
      if (!node) return createResponse({ error: 'Node not found' }, 404);
      node.assignedDomainsCount = body.domainIds.length;
      
      const res: NodeDetailResponse = {
        ...node,
        assignments: nodeAssignments.filter(a => a.nodeId === id),
        recentEvents: []
      };
      return createResponse(res);
    }
    
    const nodeRunMatch = url.match(/\/api\/admin\/nodes\/([^/]+)\/run-now$/);
    if (nodeRunMatch && method === 'POST') {
      const id = nodeRunMatch[1];
      const node = nodes.find(n => n.id === id);
      const newJob: Job = {
        id: `j${Date.now()}`,
        type: 'deploy',
        targetId: id,
        targetName: node?.name,
        status: 'running',
        startedAt: new Date().toISOString(),
        endedAt: null,
        durationMs: null,
        error: null
      };
      jobs.unshift(newJob);
      
      setTimeout(() => {
        const jobIndex = jobs.findIndex(j => j.id === newJob.id);
        if (jobIndex > -1) {
          jobs[jobIndex].status = 'success';
          jobs[jobIndex].endedAt = new Date().toISOString();
          jobs[jobIndex].durationMs = 1500;
        }
      }, 2000);
      
      return createResponse(newJob);
    }

    const nodeDeployMatch = url.match(/\/api\/admin\/nodes\/([^/]+)\/deploy$/);
    if (nodeDeployMatch && method === 'POST') {
      const id = nodeDeployMatch[1];
      const body = getBody() as { domainIds?: string[] };
      const targetIds = Array.isArray(body.domainIds) && body.domainIds.length > 0
        ? body.domainIds
        : nodeAssignments.filter((item) => item.nodeId === id).map((item) => item.domainId);
      const targetNames = nodeAssignments
        .filter((item) => item.nodeId === id && targetIds.includes(item.domainId))
        .map((item) => item.domainName || item.domainId);
      const node = nodes.find(n => n.id === id);
      const newJob: Job = {
        id: `j${Date.now()}`,
        type: 'deploy',
        targetId: id,
        targetName: targetNames.length > 0 ? `${node?.name} (${targetNames.join(', ')})` : node?.name,
        status: 'running',
        startedAt: new Date().toISOString(),
        endedAt: null,
        durationMs: null,
        error: null
      };
      jobs.unshift(newJob);

      setTimeout(() => {
        nodeAssignments = nodeAssignments.map((assignment) => (
          assignment.nodeId === id && targetIds.includes(assignment.domainId)
            ? {
                ...assignment,
                deployedSha256: assignment.desiredSha256,
                status: 'synced',
                lastDeployAt: new Date().toISOString(),
                lastError: null,
              }
            : assignment
        ));
        const jobIndex = jobs.findIndex(j => j.id === newJob.id);
        if (jobIndex > -1) {
          jobs[jobIndex].status = 'success';
          jobs[jobIndex].endedAt = new Date().toISOString();
          jobs[jobIndex].durationMs = 1200;
        }
      }, 1200);
      return createResponse(newJob);
    }

    const nodeDeleteCertsMatch = url.match(/\/api\/admin\/nodes\/([^/]+)\/delete-certs$/);
    if (nodeDeleteCertsMatch && method === 'POST') {
      const id = nodeDeleteCertsMatch[1];
      const body = getBody() as { domainIds?: string[] };
      const targetIds = Array.isArray(body.domainIds) ? body.domainIds : [];
      const targetNames = nodeAssignments
        .filter((item) => item.nodeId === id && targetIds.includes(item.domainId))
        .map((item) => item.domainName || item.domainId);
      const node = nodes.find(n => n.id === id);
      const newJob: Job = {
        id: `j${Date.now()}`,
        type: 'delete',
        targetId: id,
        targetName: targetNames.length > 0 ? `${node?.name} (${targetNames.join(', ')})` : node?.name,
        status: 'running',
        startedAt: new Date().toISOString(),
        endedAt: null,
        durationMs: null,
        error: null
      };
      jobs.unshift(newJob);

      setTimeout(() => {
        nodeAssignments = nodeAssignments.map((assignment) => (
          assignment.nodeId === id && targetIds.includes(assignment.domainId)
            ? {
                ...assignment,
                deployedSha256: null,
                status: 'pending',
                lastDeployAt: new Date().toISOString(),
                lastError: null,
              }
            : assignment
        ));
        const jobIndex = jobs.findIndex(j => j.id === newJob.id);
        if (jobIndex > -1) {
          jobs[jobIndex].status = 'success';
          jobs[jobIndex].endedAt = new Date().toISOString();
          jobs[jobIndex].durationMs = 900;
        }
      }, 900);
      return createResponse(newJob);
    }

    // Jobs
    if (url.match(new RegExp('/api/admin/jobs$')) && method === 'GET') {
      return createResponse(jobs);
    }
    const jobMatch = url.match(/\/api\/admin\/jobs\/([^\/]+)$/);
    if (jobMatch && method === 'GET') {
      const j = jobs.find(x => x.id === jobMatch[1]);
      return j ? createResponse(j) : createResponse({ error: 'Not found' }, 404);
    }
    const jobLogMatch = url.match(/\/api\/admin\/jobs\/([^/]+)\/logs$/);
    if (jobLogMatch && method === 'GET') {
      const j = jobs.find(x => x.id === jobLogMatch[1]);
      let logs = `[INFO] Job ${jobLogMatch[1]} started\n[INFO] Target: ${j?.targetName}\n`;
      if (j?.status === 'running') {
        logs += `[INFO] Running task...\n[INFO] Elapsed: ${Date.now() - new Date(j.startedAt!).getTime()}ms\n`;
      } else {
        logs += `[INFO] Completed with status ${j?.status}.\n`;
      }
      return createResponse({ logs });
    }

    // Settings
    if (url.endsWith('/api/admin/backup') && method === 'GET') {
      return createResponse({
        version: 1,
        exportedAt: new Date().toISOString(),
        settings,
        dnsChannels: channels.map((channel) => ({ ...channel, updatedAt: channel.createdAt })),
        domains: domains.map((domain) => ({
          ...domain,
          lastError: null,
          createdAt: domain.lastIssuedAt ?? new Date().toISOString(),
          updatedAt: domain.lastSyncAt ?? new Date().toISOString(),
        })),
        nodes: nodes.map((node) => ({
          id: node.id,
          name: node.name,
          ip: node.ip,
          isOnline: node.isOnline,
          lastHeartbeatAt: node.lastHeartbeatAt,
          certDir: node.certDir,
          lastError: node.lastError,
          tokenHash: `mock-hash-${node.id}`,
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        })),
        assignments: nodeAssignments.map((assignment) => ({
          ...assignment,
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        })),
      });
    }
    if (url.endsWith('/api/admin/backup/restore') && method === 'POST') {
      const body = getBody() as {
        settings: Settings;
        dnsChannels: DnsChannel[];
        domains: Domain[];
        nodes: Array<{ id: string; name: string; ip: string; isOnline: boolean; lastHeartbeatAt: string | null; certDir: string; lastError: string | null }>;
        assignments: NodeAssignment[];
      };
      settings = body.settings;
      channels = body.dnsChannels.map((channel) => ({
        id: channel.id,
        name: channel.name,
        provider: channel.provider,
        credentials: channel.credentials,
        createdAt: channel.createdAt,
      }));
      domains = body.domains;
      nodeAssignments = body.assignments;
      nodes = body.nodes.map((node) => ({
        id: node.id,
        name: node.name,
        ip: node.ip,
        isOnline: node.isOnline,
        lastHeartbeatAt: node.lastHeartbeatAt,
        certDir: node.certDir,
        assignedDomainsCount: nodeAssignments.filter((item) => item.nodeId === node.id).length,
        lastError: node.lastError,
      }));
      emitMockEvent({
        type: 'job_finished',
        level: 'warning',
        message: 'Configuration restored from backup',
        payload: { restored: true }
      });
      return createResponse({ success: true });
    }
    if (url.endsWith('/api/admin/settings')) {
      if (method === 'GET') return createResponse(settings);
      if (method === 'PATCH') {
        settings = { ...settings, ...getBody() };
        return createResponse(settings);
      }
    }
    if (url.endsWith('/api/admin/settings/webdav/test') && method === 'POST') {
      return createResponse({ success: true });
    }
    if (url.endsWith('/api/admin/settings/telegram/test') && method === 'POST') {
      return createResponse({ success: true });
    }

    console.warn(`[Mock] Unhandled request: ${method} ${url}`);
    return createResponse({ error: 'Not found in mock' }, 404);

  } catch (error: unknown) {
    console.error('[Mock] Error', error);
    return createResponse({ error: (error as Error).message }, 500);
  }
};
