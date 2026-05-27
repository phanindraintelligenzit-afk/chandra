<div align="center">

# CHANDRA

**Enterprise AI Cloud Operations Platform**
*Autonomous · Governed · Audit-Ready*

[![Next.js](https://img.shields.io/badge/Next.js-16.2-black?logo=next.js)](https://nextjs.org/)
[![React](https://img.shields.io/badge/React-18.3-149eca?logo=react)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.6-3178c6?logo=typescript)](https://www.typescriptlang.org/)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-3.4-38bdf8?logo=tailwindcss)](https://tailwindcss.com/)
[![Framer Motion](https://img.shields.io/badge/Framer_Motion-11-ff3b3b)](https://www.framer.com/motion/)
[![Deployed](https://img.shields.io/badge/Live-GitHub_Pages-ff3b3b)](https://aishanic12.github.io/chandra_extended/)
[![Status](https://img.shields.io/badge/Status-Demo_Build-ffb347)]()

**Live Demo →** [aishanic12.github.io/chandra_extended](https://aishanic12.github.io/chandra_extended/)

</div>

---

## OVERVIEW

**Chandra** is a premium, futuristic operations console for an autonomous AI cloud engineer — an enterprise *digital worker* that observes, triages, and remediates cloud operations under continuous human supervision.

Chandra reframes cloud operations around three ideas:

- **Digital Workforce.** Each agent is provisioned like an employee: it has a name, an identity, a role, a maturity level, and a permissions scope. It is on-boarded, deployed, and accountable.
- **Operational Intelligence.** Live telemetry, incidents, cost signals, and audit evidence are unified into one cinematic command center — built for SRE, FinOps, security, and governance teams to share.
- **Human-in-the-Loop Governance.** Every destructive remediation is gated by an approval state. Nothing dangerous executes until a supervisor approves, escalates, or rejects.

This repository ships the **frontend onboarding + operations dashboard** that drives this experience. It is designed as a thin, real-time UI layer that will plug into a forthcoming FastAPI + LangGraph backend orchestrating AWS, GuardDuty, and CloudWatch.

---

## CORE FEATURES

| Capability | Description |
|---|---|
| **Onboarding Wizard** | Five-step provisioning flow with name, avatar, role, maturity, KRAs, and permissions |
| **Avatar System** | Image-based holographic agent identities with glow/selection states |
| **Role Selection** | Six operational roles (AWS, Azure, K8s, DevOps, Security, Java) with frosted SVG icons |
| **Maturity Model** | L1–L4 concentric ring indicators with operational pulse animation |
| **KRA Management** | Predefined + custom KRAs that dynamically shape the dashboard surface |
| **Operational Dashboard** | Single-pane command center for governed cloud operations |
| **Live Ops Stream** | WebSocket-ready feed of severity-ranked incidents and remediations |
| **Active Incidents** | Filterable, expandable incident table with approval state and lock state |
| **Operational Waveform** | 24-hour telemetry visualization across incidents and stability |
| **Performance Scoring** | FTE-style weighted score combining productivity, quality, reliability, governance |
| **Cost Monitoring** | FinOps cards with API spend, token usage, infra load, and projected monthly cost |
| **Human Approval Center** | Pending high-risk queue with Approve / Reject / Escalate flows |
| **Audit Trail** | Immutable evidence log with CSV / XLSX / PDF export |
| **Operational Intelligence** | Alert + observation + recommendation + security-posture panel |
| **Ops Copilot** | Approval-aware conversational assistant overlay |

---

## ONBOARDING FLOW

The onboarding flow is the **identity-provisioning ceremony** for a digital worker. Each screen has a specific operational purpose and is gated so the agent cannot deploy without a complete profile.

### 1. Deployment Selection *(implicit entry point)*
The root path immediately redirects to onboarding. Stale state is cleared on a fresh session so every new browser session starts with a blank identity.

### 2. Agent Naming — *Define Your Digital Operator Identity*
**Purpose.** Assigns a workforce identity. Names are uniqueness-checked against the registry; the system auto-generates a stable `EMPLOYEE ID` from the name. Gender / identity mode and avatar are chosen here as a single act of personalization.

### 3. Avatar Selection
**Purpose.** Six holographic avatars give the operator a face. The choice persists across onboarding and dashboard and is shown as a top-right operational pill once both name and avatar are set.

### 4. Role Selection
**Purpose.** Defines the operating discipline. Role icons (AWS, Java, Azure, DevOps, Security, Kubernetes) are framed in frosted glass containers with Chandra-red operational glow so they read clearly against the dark cockpit. Only AWS Cloud Engineer is enabled in the demo; the others are surfaced as "Coming Soon" to communicate roadmap.

### 5. Maturity Pathway
**Purpose.** Sets governance posture. Maturity is rendered as **concentric pulsing rings** — L1 → one ring, L2 → two rings, L3 → three rings, L4 → four rings — communicating "how much autonomy this agent is trusted with." L2 *Operate* is the default supervised tier.

### 6. KRA Configuration
**Purpose.** Selects the Key Result Areas the agent will be measured against. Predefined KRAs (Infrastructure Monitoring, Incident Detection, Cost Optimization, Deployment Intelligence, Audit & Compliance) and free-form custom KRAs both contribute to the dashboard surface and the scoring engine.

### 7. Permissions Configuration
**Purpose.** Grants governed access scopes (IAM, CloudWatch, Security Hub, API keys, Incident Management, Audit Logs, Cost Monitoring, Infra Remediation, Webhook, Read-Only Governance, Approval Escalation). The dashboard later reports how many scopes are active.

### 8. Deployment Sequence
**Purpose.** A cinematic deploy gauge runs through *Initializing → Provisioning → Syncing → Preparing Systems → Configuring Workflows → Establishing Governance*. Only at the end is `onboardingCompleted` set and the dashboard unlocked.

---

## DASHBOARD COMPONENT BREAKDOWN

The dashboard is structured for sustained operator attention — high information density, low chrome, with each module mapping to a real operational concern.

### Global Operations Header
- **Displays.** Selected avatar + agent name, operating status, regions, accounts, incident load, uptime.
- **Why it matters.** The "rolling NORAD strip" — the at-a-glance answer to *is my fleet healthy right now?*
- **Future integration.** Will subscribe to a `/ops/summary` WebSocket channel for live region/account counts and uptime.

### Operational Intelligence
- **Displays.** Live alerts (P1–P4), observations, recommended next actions, security-posture summary.
- **Why it matters.** Synthesizes raw telemetry into *what should the human do next?*
- **Future integration.** LangGraph reasoning node emits triaged observations and recommendations into this panel.

### Operational Waveform
- **Displays.** Rolling 24-hour line chart across *Incident Activity*, *Stable Operations*, and *Background Load*.
- **Why it matters.** Pattern recognition — the operator can spot a spike before a page does.
- **Future integration.** Backed by CloudWatch / Prometheus time-series via a streaming aggregator.

### Live Ops Stream
- **Displays.** A WebSocket-ranked feed of the five most severe live incidents with expandable detail.
- **Why it matters.** The cardinal "tail -f" of the cloud — the heartbeat of the platform.
- **Future integration.** WebSocket channel directly off the LangGraph event bus.

### Active Incidents
- **Displays.** Filterable table of incidents with severity, approval state, reviewer, lock state, escalation, confidence, triage, ETA.
- **Why it matters.** The investigation surface. Every row carries the operator into root-cause + remediation + human-escalation detail.
- **Future integration.** Hydrated from the GuardDuty / Security Hub / incident-store backend; remediation status comes from LangGraph workflow runs.

### Human Approval Center
- **Displays.** Compact cards for pending high-risk approvals with **Approve / Reject / Escalate** actions, severity, account, requested-at, lock state, and email status.
- **Why it matters.** The single most important UI in the platform — it is the brake pedal. No destructive action ships without it.
- **Future integration.** Two-way WebSocket so a SecOps reviewer's decision *immediately* unblocks (or kills) an in-flight LangGraph remediation.

### Performance Scoring Engine
- **Displays.** Compact radial FTE-score gauge, formula (`(P × Q × 1.5E) + (G × 1.5R) + (C × V)`), per-metric weights and contributions for Productivity, Quality, Efficiency, Goal Attainment, Reliability, Collaboration, Value Add.
- **Why it matters.** Treats the AI worker as accountable headcount — measurable, comparable, reportable.
- **Future integration.** Score recomputed nightly against actual KRA outcomes pulled from the audit store.

### Selected KRA Performance Review
- **Displays.** Per-selected-KRA card with target / actual / confidence / automation and a one-line insight.
- **Why it matters.** Closes the loop on the KRAs chosen during onboarding — operators see *did the agent actually deliver on the responsibilities I assigned?*
- **Future integration.** Metric values stream from the backend per-KRA telemetry collector.

### Cost Monitoring
- **Displays.** FinOps cards — API cost today, AWS MTD, model token consumption, infra load, projected monthly spend.
- **Why it matters.** The CFO-side of cloud operations. Every remediation has a cost shadow.
- **Future integration.** AWS Cost Explorer + model-provider billing APIs aggregated into a single normalized stream.

### Audit Trail
- **Displays.** Immutable evidence table — timestamp, incident ID, remediation, account, confidence, reviewer, compliance control (SOC2 / GxP / 21CFR), evidence pack.
- **Why it matters.** SOC2, GxP, and 21 CFR Part 11 readiness *out of the box*. The platform proves what it did, when, with whose approval.
- **Future integration.** Exports already generate CSV / XLSX / PDF client-side; future backend will mirror to an immutable S3 evidence bucket with object-lock.

### Infrastructure Health
- **Displays.** EC2 utilization, uptime, area-chart of stability over 24 hours.
- **Why it matters.** The fleet's vital signs.
- **Future integration.** CloudWatch metric subscriptions.

### Deployment Intelligence
- **Displays.** Release success rate, rollback alerts, recent deployment log.
- **Why it matters.** Operators see deployment health beside operational health — the two are coupled.
- **Future integration.** CodeDeploy / GitHub Actions release stream.

### Ops Copilot *(floating overlay)*
- **Displays.** A floating, approval-aware chat surface with slash-command suggestions (`/review-pending-approvals`, `/summarize-high-risk-incidents`, `/draft-approval-email`, `/explain-governance`).
- **Why it matters.** Operators can interrogate the live state in natural language without leaving the cockpit.
- **Future integration.** Streams from the LangGraph conversational node with context already wired to incidents / approvals / audit.

---

## HUMAN-IN-THE-LOOP GOVERNANCE

Every high-risk remediation flows through three explicit operator actions:

| Action | Effect |
|---|---|
| **Approve** | Releases the lock; the remediation proceeds. Evidence pack is sealed. |
| **Reject** | Kills the remediation. Lock state stays paused. The rejection note is captured for audit. |
| **Escalate** | Routes the decision to a security owner. The agent remains paused until a higher-authority reviewer rules. |

**Why this matters.** Autonomy without governance is liability. Chandra's premise is that an AI worker should be as trustable as a junior SRE — empowered to act, but always escalating when the blast radius is material. The Human Approval Center, the per-incident `approvalState`, the `lockState` field, and the audit trail are designed as a single, cohesive control system.

---

## FRONTEND ARCHITECTURE

### Stack

- **Next.js 16** (App Router, static export)
- **React 18** with the `"use client"` boundary applied where state lives
- **TypeScript** end-to-end
- **Tailwind CSS 3** with a custom dark enterprise palette (`obsidian`, `signal`, `amber`, `frost`, `operational`)
- **Framer Motion** for entry transitions, modal animations, and Reveal-on-scroll
- **Recharts** for radial, line, and area visualizations
- **lucide-react** for inline iconography

### Folder Structure

```
chandra_extended/
├── app/
│   ├── layout.tsx           Root HTML shell + providers mount
│   ├── providers.tsx        OnboardingProvider injection
│   ├── globals.css          Theme tokens + telemetry/glow/ring animations
│   ├── page.tsx             Root redirect → /onboarding
│   ├── onboarding/page.tsx  Hosts the OnboardingWizard
│   └── dashboard/page.tsx   Hosts ChandraExperience + completion guard
├── components/
│   ├── OnboardingWizard.tsx Five-step provisioning flow
│   └── ChandraExperience.tsx Full operations dashboard composition
├── store/
│   ├── OnboardingContext.tsx  Identity + KRA + permissions state
│   ├── agentProfile.ts        Avatars, permissions, ID generation
│   └── kraCatalog.ts          KRA catalog + per-KRA operational metrics
├── public/
│   ├── avatars/             Holographic agent portraits (PNG)
│   └── icons/               Role SVGs (AWS / Azure / DevOps / Java / K8s / Security)
├── .github/workflows/       GitHub Pages deployment workflow
├── next.config.mjs          basePath + assetPrefix wired for GH Pages
├── tailwind.config.ts       Color tokens + custom shadows
└── tsconfig.json
```

### State Management

State is held in a single `OnboardingProvider` with a deliberate persistence policy:

- **`sessionStorage`** is the canonical store — every fresh browser session starts blank, so an old name / avatar never leaks into a new demo.
- An `onboardingCompleted` flag is the *gate* — `/dashboard` returns null and redirects to `/onboarding` until it is true.
- Legacy `localStorage` entries are actively purged on hydration.

### WebSocket-Ready Composition

All real-time surfaces (`LiveOpsStream`, `ActiveIncidents`, `HumanReviewQueue`, `OperationalIntelligencePanel`) already render from an in-memory store updated by interval timers in `useOperationalFeed()`. Swapping these timers for a single WebSocket subscription is a localized change — the components themselves are already streaming-shaped.

### API Abstraction (planned)

A `services/` layer will sit between components and the backend, exposing typed methods like `subscribeToIncidents()`, `submitApproval(id, decision)`, and `fetchAuditTrail(window)`. Today these are inlined; the seams are explicit so the swap is mechanical.

---

## ASSET SYSTEM

| Asset | Location | Purpose |
|---|---|---|
| Agent avatars | `public/avatars/*.png` | Six holographic portraits used in onboarding selection + dashboard header pill |
| Role icons | `public/icons/*.svg` | Vector icons for AWS, Azure, DevOps, Java, Kubernetes, Security roles |
| Maturity rings | CSS-generated | Concentric pulsing rings rendered via `.maturity-ring` keyframes |
| Telemetry glow | CSS-generated | Red/amber radial gradients via `.onboarding-ambient`, `.pulse-core`, `.telemetry-shimmer` |

All static assets are served through Next's `basePath`-aware helpers (`getAvatarImageSrc`, `getRoleIconSrc`) so they resolve correctly at both `localhost:3000` and `aishanic12.github.io/chandra_extended/`.

---

## REAL-TIME ARCHITECTURE *(WebSocket + LangGraph — planned)*

Chandra is structured for a streaming backend from day one. The roadmap:

1. **LangGraph as the orchestration brain.** Triage, root-cause synthesis, remediation planning, and approval routing run as nodes in a LangGraph workflow. Each transition emits an event.
2. **FastAPI as the streaming gateway.** A WebSocket endpoint (`/ws/operations`) forwards LangGraph events to the frontend. A REST surface backs Approve / Reject / Escalate decisions and audit queries.
3. **Frontend subscriptions.** Live Ops Stream, Active Incidents, Approval Center, and the Operational Waveform attach to this channel. The Ops Copilot streams chat tokens over the same wire.
4. **Replay + audit.** Every event is mirrored to the audit store before it is broadcast, so the live UI and the immutable evidence pack are guaranteed to match.

The current demo simulates this stream with deterministic interval timers so the operator experience is faithful before the backend lands.

---

## AWS + BACKEND INTEGRATION *(planned)*

| Layer | Integration |
|---|---|
| **Telemetry** | CloudWatch metrics + logs, AWS Health, Cost Explorer |
| **Security signals** | GuardDuty findings, Security Hub posture, IAM Access Analyzer |
| **Remediation** | Bounded, policy-checked actions through governed runbooks (e.g. block public ACL, revoke session) |
| **Evidence** | S3 evidence bucket with object-lock for SOC2 / GxP / 21 CFR Part 11 packs |
| **Orchestration** | LangGraph nodes for triage, recommendation, approval routing, post-action verification |
| **Identity** | Cognito / Auth0 for supervisor identity + reviewer attribution |
| **Notifications** | SES / SendGrid for approval-request emails (already modeled in the frontend's `emailStatus` field) |

---

## LOCAL DEVELOPMENT

### Prerequisites
- Node.js 20+
- npm 10+

### Setup

```bash
git clone https://github.com/aishanic12/chandra_extended.git
cd chandra_extended
npm install
```

### Run the dev server

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The root redirects to `/onboarding`.

### Production build

```bash
npm run build
```

The static export is emitted to `./out/`.

### Useful scripts

| Command | Purpose |
|---|---|
| `npm run dev` | Start Next.js dev server with Turbopack |
| `npm run build` | Compile + statically export to `./out` |
| `npm run start` | Serve the production build |
| `npm run lint` | ESLint over `app/` and `components/` |

---

## GITHUB PAGES DEPLOYMENT

Deployment is fully automated via [`.github/workflows/nextjs.yml`](.github/workflows/nextjs.yml).

- Pushes to `main` trigger a build job on `ubuntu-latest`.
- `actions/configure-pages@v5` enables the Next.js static-export path.
- `next build` produces `./out/`, which `actions/upload-pages-artifact` uploads.
- `actions/deploy-pages@v5` publishes to GitHub Pages.

### basePath / asset prefix

`next.config.mjs` derives `basePath` from `GITHUB_REPOSITORY` at build time, exposes `NEXT_PUBLIC_BASE_PATH` to the client, and sets `assetPrefix` accordingly. All avatar and icon URLs flow through `getAvatarImageSrc` / `getRoleIconSrc` so they resolve correctly under both root and sub-path deployments.

### Live deployment

**→ [aishanic12.github.io/chandra_extended/](https://aishanic12.github.io/chandra_extended/)**

---

## UI / UX DESIGN PHILOSOPHY

Chandra is designed as a **cinematic enterprise observability cockpit** — not a SaaS dashboard.

- **Operational intelligence first.** Every surface answers an operator's question, not a marketing one.
- **Dense by design.** Each panel earns its pixels. There is no decorative whitespace.
- **Red telemetry, amber operational, emerald approved.** The palette codifies severity. Red is the Chandra accent of identity; amber communicates operational watchfulness; emerald signals trust and approval.
- **Subtle motion.** Pulse cores, ring animations, ambient red glows, and a slow telemetry shimmer create a sense of *aliveness* without distraction. No gaming UI, no neon, no bounce.
- **Mono typography.** Tabular numerals, uppercase labels, fine tracking — the typography of mission control.
- **Human always reachable.** The Human Approval Center is one click away from every incident, every audit row, every conversation.

---

## TECH STACK

| Layer | Technology |
|---|---|
| Framework | Next.js 16 (App Router, static export) |
| Language | TypeScript 5.6 |
| UI runtime | React 18.3 |
| Styling | Tailwind CSS 3.4 + custom CSS |
| Motion | Framer Motion 11 |
| Charts | Recharts 2 |
| Icons | lucide-react 0.468 + custom SVG set |
| Hosting | GitHub Pages (static export) |
| Backend *(planned)* | FastAPI + WebSocket |
| Orchestration *(planned)* | LangGraph |
| Cloud *(planned)* | AWS (CloudWatch, GuardDuty, IAM, Cost Explorer, S3 evidence bucket) |
| Identity *(planned)* | Cognito / Auth0 |

---

## ROADMAP

- [ ] WebSocket bridge to a FastAPI backend for live ops events
- [ ] LangGraph orchestration for triage, remediation, and post-action verification
- [ ] CloudWatch + GuardDuty + Security Hub telemetry ingestion
- [ ] Cost Explorer integration for live FinOps cards
- [ ] Cognito / Auth0 supervisor identity + reviewer attribution
- [ ] Immutable evidence packs in an object-locked S3 bucket
- [ ] Realtime approval round-trip with email + Slack delivery
- [ ] Role-based access control across the dashboard surface
- [ ] Multi-agent workforce (Azure, K8s, Security, Java engineers go live)
- [ ] Conversational replay of any incident from the audit trail
- [ ] Production deployment behind a managed CDN

---

## REPOSITORY

- **Repo.** [github.com/aishanic12/chandra_extended](https://github.com/aishanic12/chandra_extended)
- **Live.** [aishanic12.github.io/chandra_extended](https://aishanic12.github.io/chandra_extended/)
- **Branch.** `main` (deploys automatically on push)

---

<div align="center">

**CHANDRA — Observable · Accountable · Supervised**

</div>
