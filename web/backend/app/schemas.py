from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthStatusResponse(BaseModel):
    initialized: bool
    setupRequired: bool


class AuthAccountResponse(BaseModel):
    username: str


class BootstrapRequest(BaseModel):
    username: str
    password: str


class AccountUpdateRequest(BaseModel):
    username: str
    currentPassword: str
    newPassword: str = ""


class DomainCreate(BaseModel):
    domain: str
    dnsChannelId: str
    enabled: bool = True


class DomainPatch(BaseModel):
    domain: str | None = None
    dnsChannelId: str | None = None
    enabled: bool | None = None


class BulkDomainActionRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    action: str


class DnsChannelCreate(BaseModel):
    name: str
    provider: str
    credentials: dict[str, str] = Field(default_factory=dict)


class DnsChannelPatch(BaseModel):
    name: str | None = None
    provider: str | None = None
    credentials: dict[str, str] | None = None


class NodeCreate(BaseModel):
    name: str
    ip: str = ""
    certDir: str = "/etc/nginx/ssl"


class NodePatch(BaseModel):
    name: str | None = None
    ip: str | None = None
    certDir: str | None = None
    lastError: str | None = None


class AssignmentUpdate(BaseModel):
    domainIds: list[str]


class NodeCommandRequest(BaseModel):
    domainIds: list[str] = Field(default_factory=list)


class WebDavSettings(BaseModel):
    url: str
    auth: str


class TelegramSettings(BaseModel):
    botToken: str
    chatId: str


class AcmeSettings(BaseModel):
    acmeHome: str
    stagingBase: str
    defaultRenewDays: int
    defaultCa: str = "letsencrypt"
    accountEmail: str = ""


class NodeAccessSettings(BaseModel):
    publicBaseUrl: str = ""


class SettingsPayload(BaseModel):
    webdav: WebDavSettings
    telegram: TelegramSettings
    acme: AcmeSettings
    node: NodeAccessSettings = Field(default_factory=NodeAccessSettings)


class BackupDnsChannel(BaseModel):
    id: str
    name: str
    provider: str
    credentials: dict[str, str] = Field(default_factory=dict)
    createdAt: str
    updatedAt: str


class BackupDomain(BaseModel):
    id: str
    domain: str
    enabled: bool = True
    dnsChannelId: str
    expiresAt: str | None = None
    lastIssuedAt: str | None = None
    lastSyncAt: str | None = None
    certSha256: str | None = None
    status: str = "pending"
    lastError: str | None = None
    createdAt: str
    updatedAt: str


class BackupNode(BaseModel):
    id: str
    name: str
    ip: str = ""
    isOnline: bool = False
    lastHeartbeatAt: str | None = None
    certDir: str = "/etc/nginx/ssl"
    lastError: str | None = None
    tokenHash: str
    createdAt: str
    updatedAt: str


class BackupAssignment(BaseModel):
    id: str
    nodeId: str
    domainId: str
    desiredSha256: str | None = None
    deployedSha256: str | None = None
    status: str = "pending"
    lastDeployAt: str | None = None
    expiresAt: str | None = None
    lastError: str | None = None
    createdAt: str
    updatedAt: str


class BackupPayload(BaseModel):
    version: int = 1
    exportedAt: str
    settings: SettingsPayload
    dnsChannels: list[BackupDnsChannel] = Field(default_factory=list)
    domains: list[BackupDomain] = Field(default_factory=list)
    nodes: list[BackupNode] = Field(default_factory=list)
    assignments: list[BackupAssignment] = Field(default_factory=list)


class NodeHeartbeat(BaseModel):
    hostname: str | None = None
    ip: str | None = None
    version: str | None = None
    certDir: str | None = None


class NodeReportItem(BaseModel):
    domainId: str
    domainName: str | None = None
    deployedSha256: str | None = None
    status: str
    expiresAt: str | None = None
    lastError: str | None = None


class NodeReport(BaseModel):
    items: list[NodeReportItem]


class NodeCommandAck(BaseModel):
    status: str = "completed"
    error: str | None = None
    summary: str | None = None
