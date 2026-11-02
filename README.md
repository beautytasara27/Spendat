# Spenddat — Autonomous Purchasing Agent

Turns a plain-English request ("buy a phone case from acp merchant, under $25") into a real
purchase, governed by a deterministic, auditable policy engine. In-policy purchases execute
**autonomously**; only risky, ambiguous or out-of-policy ones are **escalated** to a human;
forbidden ones are **denied** outright. Every step lands in a tamper-evident audit log.

```
request ──► Intent parser (LLM, schema-validated) ──► Vendor adapter: search + resolve price
                                                          │
                              ┌── DENY ──► reject + notify user (no human, no purchase)
Policy engine (pure, no LLM) ─┼── ESCALATE ──► approval (human) ──► approve / deny / timeout
                              └── ALLOW ────────────┐                    │
                                                    ▼                    ▼
                               Execution gate ─► hard circuit breakers ─► scoped payment token
                                                 ─► vendor checkout ─► revoke token ─► audit
```

* **Vendor A:** any merchant implementing the **Agentic Commerce Protocol (ACP)** — an open
  REST checkout standard merchants publish *for* AI agents (official API, no scraping).
* **Payments:** **Stripe Shared Payment Tokens** — per-transaction, amount-capped,
  seller-scoped, time-limited, revoked after use. No card number ever touches this system.
* **Intent parsing:** Anthropic Messages API with a forced tool call, then deterministic
  validation.
* **Storage:** SQLite, hash-chained append-only audit log.

See [`docs/architecture.md`](docs/architecture.md) and the ADRs in [`docs/adr/`](docs/adr/).

## How I designed it

I wanted an agent that can actually spend money without me worrying about it. My rule
for the whole project: **the LLM never decides whether money moves.** The model only
turns my sentence into structured data. A deterministic policy engine makes the
decision, and a single execution gate is the only code path that can pay.

### User stories

I wrote these before the code and used the acceptance criteria as test cases.

| ID | As a… | I want to… | So that… | Acceptance criteria |
|---|---|---|---|---|
| US-1 | shopper | ask for a purchase in plain English ("white Sambas, size 8.5, under $100") | I don't have to browse stores myself | The request is parsed into item, size, colour, budget and store. Vague requests get a clarifying question instead of a guess. |
| US-2 | shopper | have small, in-policy purchases go through on their own | routine buying doesn't need me | An ALLOW decision executes with no human step, and the payment token is capped at the quoted total. |
| US-3 | shopper | approve risky or unusual purchases before they happen | I stay in control of anything out of the ordinary | An ESCALATE decision creates an approval showing item, store and full price. I can approve, deny or lower the cap. It expires after a timeout. |
| US-4 | shopper | have forbidden purchases refused outright | the agent can't be talked into buying something I've ruled out | Blocked stores and categories are DENIED before checkout, with a reason code. |
| US-5 | shopper | set budgets per purchase, per day, per week and per category | spending stays predictable | Policy lives in versioned YAML. Running totals are checked at the same moment the spend is reserved. |
| US-6 | shopper | shop across several stores, not just one | I get the best match and price | Every enabled store is searched; size/colour variants are matched and the cheapest valid option wins. |
| US-7 | owner | see every transaction and why it was decided that way | I can audit the agent | The dashboard shows history and spend. Each decision stores the policy version and the rules that fired. |
| US-8 | owner | know if anyone tampered with the log | the audit trail is trustworthy | Events are hash-chained and append-only. `spenddat verify-audit` detects any edited or deleted row. |
| US-9 | owner | never be charged twice, even after a crash | a timeout doesn't turn into a duplicate order | Idempotency keys on every request. Unknown outcomes go to reconciliation, never to a retry. |
| US-10 | developer | test everything without real money | I can iterate safely | Sandbox and live use separate secrets. Live needs an explicit confirmation latch. Shopify runs as a dry run in sandbox. |

### Architecture

The system has three layers. The **interfaces** take requests. The **core** decides.
The **adapters** talk to the outside world. Every adapter implements the same four-method
interface, so adding a store type never touches the policy or the orchestrator.

```mermaid
flowchart LR
    subgraph UI["Interfaces"]
        WEB["Web app<br/>dashboard + chat"]
        CLI["CLI<br/>buy, approve, history"]
    end

    subgraph CORE["Core (deterministic)"]
        ORCH["Orchestrator<br/>pipeline + state machine"]
        INTENT["Intent parser<br/>LLM + schema validation"]
        POLICY["Policy engine<br/>pure functions, no LLM"]
        APPROVAL["Approval service<br/>human-in-the-loop"]
        GATE["Execution gate<br/>sealed permit + circuit breakers"]
    end

    subgraph DATA["Persistence"]
        DB[("SQLite (WAL)<br/>audit_events, transactions, approvals")]
        CFG[/"policy.yaml + vendors.yaml<br/>versioned config"/]
    end

    subgraph EXT["External services"]
        LLM["OpenRouter / Anthropic"]
        ACP["ACP merchants<br/>REST checkout"]
        SHOP["Shopify stores<br/>WebMCP in Chrome"]
        STRIPE["Stripe<br/>Shared Payment Tokens"]
    end

    WEB --> ORCH
    CLI --> ORCH
    ORCH --> INTENT --> LLM
    ORCH --> POLICY
    POLICY -. reads .-> CFG
    ORCH --> APPROVAL
    ORCH --> GATE
    GATE -- "issue / revoke token" --> STRIPE
    GATE -- checkout --> ACP
    GATE -- checkout --> SHOP
    ORCH -- "search + quote" --> ACP
    ORCH -- "search + quote" --> SHOP
    ORCH -- "append events" --> DB
    APPROVAL --> DB
```

### Purchase sequence (technical view)

A single purchase from request to receipt. The `alt` block shows the three policy
outcomes. The payment token exists only for the few seconds of checkout and is revoked
straight after. Shopify stores skip the Stripe steps and use your saved Shop Pay card; the gate re-checks the total against the approved cap first.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant O as Orchestrator
    participant L as LLM
    participant V as Store adapter
    participant P as Policy engine
    participant A as Approval service
    participant G as Execution gate
    participant S as Stripe
    participant DB as Audit store

    U->>O: "buy white Sambas size 8.5 under $100"
    O->>DB: create transaction, log request_received
    O->>L: extract intent (forced tool call)
    L-->>O: PurchaseIntent JSON
    O->>O: validate against schema
    O->>V: search + resolve variant and price
    V-->>O: candidate with total incl. shipping
    O->>P: evaluate(facts, spend so far)
    P-->>O: decision + reason codes + policy version
    O->>DB: log decision

    alt DENY
        O-->>U: refused, with reason
    else ESCALATE
        O->>A: open approval (expires after timeout)
        A-->>U: approval request
        U->>A: approve, deny or lower the cap
        A->>O: decision
    end

    opt ALLOW, or approved
        O->>G: authorize (breakers + reserve spend, atomically)
        G->>S: issue token capped at approved total
        S-->>G: shared payment token
        G->>V: complete checkout with token
        V-->>G: order confirmation
        G->>S: revoke token
        O->>DB: log completion
        O-->>U: receipt
    end
```

### Transaction lifecycle (UML state machine)

Every transaction moves through these states and nothing else. Each transition writes
an audit event, so the state machine and the log always agree.

```mermaid
stateDiagram-v2
    [*] --> Received
    Received --> NeedsClarification: request too vague
    Received --> Rejected: unsupported request
    Received --> Denied: policy DENY
    Received --> PendingApproval: policy ESCALATE
    Received --> Executing: policy ALLOW
    Received --> BlockedByCircuitBreaker: hard limit hit

    PendingApproval --> Executing: approved
    PendingApproval --> ApprovalDenied: denied
    PendingApproval --> ApprovalExpired: timed out
    PendingApproval --> BlockedByCircuitBreaker: hard limit hit

    Executing --> Completed: order confirmed
    Executing --> Failed: definite failure
    Executing --> NeedsReconciliation: outcome unknown

    NeedsReconciliation --> Completed: order found
    NeedsReconciliation --> Failed: no order found

    NeedsClarification --> [*]
    Rejected --> [*]
    Denied --> [*]
    ApprovalDenied --> [*]
    ApprovalExpired --> [*]
    BlockedByCircuitBreaker --> [*]
    Completed --> [*]
    Failed --> [*]
```

### Database (UML class diagram)

Three tables. `audit_events` is the source of truth. `transactions` and `approvals` are
read models I keep for fast queries. `audit_events` can't be changed after the fact:
SQLite triggers abort any `UPDATE` or `DELETE`, and each row stores the SHA-256 of the
previous row, so any edit breaks the chain. Money is always stored in integer cents,
never floats.

```mermaid
classDiagram
    direction LR

    class transactions {
        <<table>>
        +TEXT txn_id PK
        +TEXT mode
        +TEXT state
        +TEXT request_text
        +TEXT created_at
        +TEXT updated_at
        +TEXT vendor
        +TEXT category
        +TEXT sku
        +INTEGER unit_price_minor
        +INTEGER amount_minor
        +TEXT vendor_ref
        +TEXT committed_at
        +TEXT completed_at
        +TEXT snapshot  JSON
    }

    class audit_events {
        <<table, append-only>>
        +INTEGER seq PK AUTOINCREMENT
        +TEXT txn_id FK
        +TEXT event_type
        +TEXT mode
        +TEXT ts
        +TEXT payload  JSON
        +TEXT prev_hash
        +TEXT hash UNIQUE
        verify_chain() int
    }

    class approvals {
        <<table>>
        +TEXT approval_id PK
        +TEXT txn_id FK UNIQUE
        +TEXT mode
        +TEXT status
        +TEXT created_at
        +TEXT expires_at
        +TEXT presented  JSON
        +TEXT decided_by
        +TEXT decided_at
        +TEXT action
        +TEXT modification  JSON
    }

    class TxnState {
        <<enumeration>>
        received
        needs_clarification
        rejected
        denied
        pending_approval
        approval_denied
        approval_expired
        executing
        completed
        failed
        needs_reconciliation
        blocked_by_circuit_breaker
    }

    class ApprovalStatus {
        <<enumeration>>
        pending
        approved
        denied
        expired
    }

    transactions "1" -- "1..*" audit_events : logged by
    transactions "1" -- "0..1" approvals : may require
    transactions ..> TxnState : state
    approvals ..> ApprovalStatus : status
```

The indexes are `ix_events_txn(txn_id)` for per-transaction history and
`ix_txn_mode_state(mode, state)` for spend totals and the approval queue. Because every
row carries a `mode`, sandbox and live data can never be mixed up in a budget
calculation.

### How it feels to use (user view)

From the user's side it's a chat. Most purchases finish without any extra steps; the
agent only stops to ask when it should.

```mermaid
flowchart TD
    A([Type what you want in the chat]) --> B{Did the agent<br/>understand?}
    B -- No --> C[Agent asks a follow-up question] --> A
    B -- Yes --> D[Agent searches your stores<br/>and finds the best match]
    D --> E{Found it in your<br/>size and colour?}
    E -- No --> F[Agent explains what's<br/>missing or sold out] --> Z([Done])
    E -- Yes --> G{What does your<br/>policy say?}
    G -- Not allowed --> H[Refused, with the reason] --> Z
    G -- Within limits --> K[Bought automatically]
    G -- Needs a look --> I[Approval card: item, store, total]
    I --> J{Your call}
    J -- Approve --> K
    J -- Deny or ignore --> L[Cancelled, no money moves] --> Z
    K --> M[Receipt in chat,<br/>purchase on the dashboard] --> Z
```
