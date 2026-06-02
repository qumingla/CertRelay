export interface PaginatedResponse<T> {
  data: T[];
  total: number;
}

export interface ApiError {
  error: string;
  code?: string;
  details?: Record<string, unknown>;
}

export interface AuthStatus {
  initialized: boolean;
  setupRequired: boolean;
}

export interface AuthAccount {
  username: string;
}

export interface Domain {
  id: string;
  domain: string;
  enabled: boolean;
  dnsChannelId: string;
  expiresAt: string | null;
  daysRemaining: number | null;
  lastIssuedAt: string | null;
  lastSyncAt: string | null;
  certSha256: string | null;
  status: 'active' | 'expiring' | 'expired' | 'error' | 'pending';
}

export interface CertNode {
  id: string;
  name: string;
  ip: string;
  isOnline: boolean;
  lastHeartbeatAt: string | null;
  certDir: string;
  assignedDomainsCount: number;
  lastError: string | null;
}

export interface NodeAssignment {
  id: string;
  nodeId: string;
  domainId: string;
  domainName?: string;
  desiredSha256: string | null;
  deployedSha256: string | null;
  status: 'synced' | 'pending' | 'error';
  lastDeployAt: string | null;
  expiresAt: string | null;
  lastError: string | null;
}

export interface NodeDetailResponse extends CertNode {
  assignments: NodeAssignment[];
  recentEvents: SystemEvent[];
}

export interface DnsChannel {
  id: string;
  name: string;
  provider: 'dns_cf' | 'dns_ali' | 'dns_tencent' | 'dns_dp' | 'dns_huaweicloud' | 'dns_gd' | 'custom';
  credentials: Record<string, string>; // Masked on the frontend
  createdAt: string;
}

export interface Job {
  id: string;
  type: 'issue' | 'renew' | 'sync' | 'deploy' | 'delete' | 'test_dns';
  targetId: string; // domainId, nodeId, etc.
  targetName?: string;
  status: 'running' | 'success' | 'failed' | 'pending';
  startedAt: string | null;
  endedAt: string | null;
  durationMs: number | null;
  error: string | null;
}

export interface SystemEvent {
  id: string;
  type: 'node_heartbeat' | 'job_started' | 'job_finished' | 'deploy_success' | 'deploy_failed' | 'cert_expiring';
  level: 'info' | 'warning' | 'error' | 'success';
  message: string;
  createdAt: string;
  payload: Record<string, unknown>;
}

export interface DashboardStats {
  onlineNodes: number;
  totalNodes: number;
  totalDomains: number;
  expiringSoon: number;
  failedJobs: number;
}

export interface OverviewResponse {
  stats: DashboardStats;
  certificates: Domain[];
  nodes: CertNode[];
  recentEvents: SystemEvent[];
}

export interface Settings {
  webdav: {
    url: string;
    auth: string;
  };
  telegram: {
    botToken: string;
    chatId: string;
  };
  acme: {
    acmeHome: string;
    stagingBase: string;
    defaultRenewDays: number;
    defaultCa: string;
    accountEmail: string;
  };
  node: {
    publicBaseUrl: string;
  };
}

export interface BackupDnsChannel {
  id: string;
  name: string;
  provider: string;
  credentials: Record<string, string>;
  createdAt: string;
  updatedAt: string;
}

export interface BackupDomain {
  id: string;
  domain: string;
  enabled: boolean;
  dnsChannelId: string;
  expiresAt: string | null;
  lastIssuedAt: string | null;
  lastSyncAt: string | null;
  certSha256: string | null;
  status: string;
  lastError: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface BackupNode {
  id: string;
  name: string;
  ip: string;
  isOnline: boolean;
  lastHeartbeatAt: string | null;
  certDir: string;
  lastError: string | null;
  tokenHash: string;
  createdAt: string;
  updatedAt: string;
}

export interface BackupAssignment {
  id: string;
  nodeId: string;
  domainId: string;
  desiredSha256: string | null;
  deployedSha256: string | null;
  status: string;
  lastDeployAt: string | null;
  expiresAt: string | null;
  lastError: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface BackupPayload {
  version: number;
  exportedAt: string;
  settings: Settings;
  dnsChannels: BackupDnsChannel[];
  domains: BackupDomain[];
  nodes: BackupNode[];
  assignments: BackupAssignment[];
}
