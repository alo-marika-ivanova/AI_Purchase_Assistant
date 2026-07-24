PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    contact_channel TEXT NOT NULL CHECK (contact_channel IN ('whatsapp', 'email')),
    whatsapp_number TEXT,
    email TEXT,
    category TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS negotiation_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number TEXT NOT NULL UNIQUE,
    item_material TEXT NOT NULL,
    quantity REAL NOT NULL,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    auto_send_messages INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS case_suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER NOT NULL,
    included INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(case_id, supplier_id),
    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER,
    message_type TEXT NOT NULL DEFAULT 'general',
    approval_required INTEGER NOT NULL DEFAULT 0,
    approved_by_buyer INTEGER NOT NULL DEFAULT 0,
    approved_at TEXT,
    sent_at TEXT,
    direction TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    channel TEXT NOT NULL CHECK (channel IN ('whatsapp', 'email', 'manual')),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
);


CREATE TABLE IF NOT EXISTS negotiation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    approval_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    approved_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS action_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER NOT NULL,
    message_id INTEGER,

    unit_price_usd REAL NOT NULL,
    quantity REAL,
    total_price_usd REAL,

    extraction_method TEXT NOT NULL DEFAULT 'manual',
    extraction_confidence TEXT NOT NULL DEFAULT 'human_verified',
    status TEXT NOT NULL DEFAULT 'active',

    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE,
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS winner_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER NOT NULL,
    offer_id INTEGER NOT NULL,

    decision_status TEXT NOT NULL DEFAULT 'approved',
    reason TEXT,
    approved_by TEXT NOT NULL DEFAULT 'buyer',

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE,
    FOREIGN KEY(offer_id) REFERENCES offers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS supplier_negotiation_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    case_id INTEGER NOT NULL,
    supplier_id INTEGER NOT NULL,

    state TEXT NOT NULL DEFAULT 'NOT_CONTACTED',

    rfq_sent_at TEXT,
    followup_sent_at TEXT,
    last_inbound_at TEXT,

    best_offer_usd REAL,
    target_price_usd REAL,

    negotiation_attempts INTEGER NOT NULL DEFAULT 0,

    -- 1 means system has sent a buyer message and is waiting for supplier reply
    awaiting_supplier_reply INTEGER NOT NULL DEFAULT 0,

    -- 1 means this supplier/case is finished
    closed INTEGER NOT NULL DEFAULT 0,

    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE,

    UNIQUE(case_id, supplier_id)
);


CREATE TABLE IF NOT EXISTS email_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    graph_message_id TEXT NOT NULL UNIQUE,
    case_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,

    sender_email TEXT,
    subject TEXT,
    received_at TEXT,

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS email_message_headers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    message_id INTEGER NOT NULL UNIQUE,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER NOT NULL,

    subject TEXT NOT NULL,
    internet_message_id TEXT,
    in_reply_to TEXT,
    reference_chain TEXT,
    graph_conversation_id TEXT,

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS whatsapp_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_message_id TEXT NOT NULL UNIQUE,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sender_phone TEXT NOT NULL,
    received_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (case_id) REFERENCES negotiation_cases(id),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

-- Staging table for inbound WhatsApp webhook events. The webhook inserts a
-- row here and returns immediately; classification, case routing, and
-- negotiation processing happen later (transport worker poll, or a
-- background task triggered right after the response is sent), not in the
-- webhook request itself. whatsapp_imports (above) remains the durable
-- record of a successfully processed inbound WhatsApp message, written once
-- processing completes.
CREATE TABLE IF NOT EXISTS whatsapp_inbound_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    wa_message_id TEXT NOT NULL UNIQUE,
    sender_phone TEXT NOT NULL,
    body TEXT NOT NULL,
    received_at TEXT,

    -- failed also covers "processing was interrupted by a crash/restart":
    -- reprocessing is not safe to do automatically (record_supplier_message_simple
    -- is not idempotent), so an abandoned event is surfaced for manual
    -- review rather than silently retried.
    status TEXT NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'processing', 'processed', 'failed')),

    case_id INTEGER,
    supplier_id INTEGER,
    message_id INTEGER,
    error TEXT,

    locked_at TEXT,
    locked_by TEXT,

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT,

    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE SET NULL,
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL,
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_inbound_events_status
ON whatsapp_inbound_events(status);

CREATE TABLE IF NOT EXISTS negotiation_action_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER,
    action_key TEXT NOT NULL,
    action_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(case_id, supplier_id, action_key),
    FOREIGN KEY (case_id) REFERENCES negotiation_cases(id),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
);

CREATE TABLE IF NOT EXISTS human_review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER,
    message_id INTEGER,
    review_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    FOREIGN KEY (case_id) REFERENCES negotiation_cases(id),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS case_negotiation_context (
    case_id INTEGER PRIMARY KEY,
    initial_best_offer_usd REAL NOT NULL,
    target_price_usd REAL NOT NULL,
    best_supplier_id INTEGER NOT NULL,
    best_offer_id INTEGER NOT NULL,
    valid_offer_count INTEGER NOT NULL,
    target_discount_percent REAL NOT NULL,
    ranking_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(case_id)
        REFERENCES negotiation_cases(id)
        ON DELETE CASCADE,

    FOREIGN KEY(best_supplier_id)
        REFERENCES suppliers(id),

    FOREIGN KEY(best_offer_id)
        REFERENCES offers(id)
);

CREATE TABLE IF NOT EXISTS supplier_goods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL,
    goods_group TEXT,
    goods_name TEXT NOT NULL,
    source_sheet TEXT,
    source_column TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(supplier_id, goods_name),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_supplier_goods_goods_name
ON supplier_goods(goods_name);

CREATE INDEX IF NOT EXISTS idx_supplier_goods_supplier_id
ON supplier_goods(supplier_id);


CREATE TABLE IF NOT EXISTS case_notification_preferences (
    case_id INTEGER PRIMARY KEY,
    notify_human_review_email INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS human_review_email_notifications (
    review_item_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'sending'
        CHECK (status IN ('sending', 'sent', 'failed')),
    recipient_email TEXT,
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (review_item_id) REFERENCES human_review_items(id) ON DELETE CASCADE
);

-- Transport outbox: one row per real outbound email/WhatsApp delivery job.
-- The messages table remains the permanent conversation/audit record; the
-- outbox tracks delivery attempts, retries, and provider outcomes for the
-- message it references. No row is created for simulated-case messages
-- (auto_send_messages = 0), since nothing is handed to a provider for those.
CREATE TABLE IF NOT EXISTS transport_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    message_id INTEGER NOT NULL UNIQUE,
    case_id INTEGER NOT NULL,
    supplier_id INTEGER,

    channel TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    idempotency_key TEXT NOT NULL UNIQUE,

    -- simulated: the adapter itself ran in dry-run/test mode (EMAIL_DRY_RUN,
    -- WHATSAPP_DRY_RUN), so no real provider call was made.
    -- delivery_unknown: a timeout/connection loss made it impossible to tell
    -- whether the provider accepted the message. Never auto-retried.
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',
            'processing',
            'sent',
            'transient_failure',
            'permanent_failure',
            'delivery_unknown',
            'simulated'
        )),

    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,

    locked_at TEXT,
    locked_by TEXT,

    provider_message_id TEXT,
    last_error TEXT,
    failure_type TEXT
        CHECK (failure_type IN ('transient', 'permanent', 'unknown') OR failure_type IS NULL),

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,

    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY(case_id) REFERENCES negotiation_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_transport_outbox_claim
ON transport_outbox(status, next_attempt_at);
