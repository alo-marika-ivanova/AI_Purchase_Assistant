from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from typing import Iterable

from app.db.database import get_connection


ACTION_LOCK_LEASE_SECONDS = 300


class PurchasingRepository:
    """
    Central database access layer.

    Services should call this class instead of writing SQL directly.
    This keeps SQL in one place and makes future FastAPI/email/WhatsApp integration safer.
    """

    # ---------- Suppliers ----------

    def list_active_suppliers(self) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, supplier_code, name, contact_channel, whatsapp_number, email, category, notes
                FROM suppliers
                WHERE active = 1
                ORDER BY name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_material_choices(self) -> list[dict]:
        """Return materials imported from the buyer supplier filter workbook."""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    sg.goods_name,
                    COALESCE(sg.goods_group, 'Other') AS goods_group,
                    COUNT(DISTINCT sg.supplier_id) AS supplier_count
                FROM supplier_goods sg
                JOIN suppliers s ON s.id = sg.supplier_id
                WHERE s.active = 1
                GROUP BY sg.goods_group, sg.goods_name
                HAVING supplier_count > 0
                ORDER BY goods_group, goods_name
                """
            ).fetchall()

        return [dict(row) for row in rows]

    def material_exists(self, goods_name: str) -> bool:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM supplier_goods
                WHERE lower(goods_name) = lower(?)
                LIMIT 1
                """,
                (goods_name.strip(),),
            ).fetchone()

        return row is not None

    def count_material_choices(self) -> int:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT goods_name) AS material_count
                FROM supplier_goods
                """
            ).fetchone()

        return int(row["material_count"])

    def list_suppliers_for_material(self, goods_name: str) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT
                    s.id,
                    s.supplier_code,
                    s.name,
                    s.contact_channel,
                    s.whatsapp_number,
                    s.email,
                    s.category,
                    s.notes
                FROM suppliers s
                JOIN supplier_goods sg ON sg.supplier_id = s.id
                WHERE s.active = 1
                  AND sg.goods_name = ?
                ORDER BY s.name
                """,
                (goods_name.strip(),),
            ).fetchall()

        return [dict(row) for row in rows]


    # ---------- Cases ----------

    def list_cases(self) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.case_number, c.item_material, c.quantity, c.notes, c.status,
                       c.created_at, c.auto_send_messages,
                       COALESCE(cnp.notify_human_review_email, 0) AS notify_human_review_email,
                       COUNT(cs.supplier_id) AS supplier_count
                FROM negotiation_cases c
                LEFT JOIN case_suppliers cs ON cs.case_id = c.id AND cs.included = 1
                LEFT JOIN case_notification_preferences cnp ON cnp.case_id = c.id
                GROUP BY c.id
                ORDER BY c.id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_case(
            self,
            item_material: str,
            quantity: float,
            notes: str,
            supplier_ids: Iterable[int],
            auto_send_messages: bool = False,
            notify_buyer_on_human_review: bool = False,
    ) -> int:
        clean_item = item_material.strip()
        supplier_ids = list(dict.fromkeys(int(sid) for sid in supplier_ids))

        now_stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        case_number = f"CASE-{now_stamp}-{uuid4().hex[:6].upper()}"

        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO negotiation_cases
                (
                    case_number,
                    item_material,
                    quantity,
                    notes,
                    status,
                    auto_send_messages
                )
                VALUES (?, ?, ?, ?, 'READY_TO_START', ?)
                """,
                (
                    case_number,
                    clean_item,
                    quantity,
                    notes.strip() or None,
                    1 if auto_send_messages else 0,
                ),
            )

            case_id = int(cur.lastrowid)

            conn.execute(
                """
                INSERT INTO case_notification_preferences
                (
                    case_id,
                    notify_human_review_email
                )
                VALUES (?, ?)
                """,
                (
                    case_id,
                    1 if notify_buyer_on_human_review else 0,
                ),
            )

            conn.executemany(
                """
                INSERT INTO case_suppliers
                (
                    case_id,
                    supplier_id,
                    included
                )
                VALUES (?, ?, 1)
                """,
                [(case_id, supplier_id) for supplier_id in supplier_ids],
            )

            # Initialize supplier states immediately.
            conn.executemany(
                """
                INSERT OR IGNORE INTO supplier_negotiation_state
                (
                    case_id,
                    supplier_id,
                    state,
                    updated_at
                )
                VALUES (?, ?, 'NOT_CONTACTED', CURRENT_TIMESTAMP)
                """,
                [(case_id, supplier_id) for supplier_id in supplier_ids],
            )

            conn.execute(
                """
                INSERT INTO negotiation_events
                (
                    case_id,
                    event_type,
                    details
                )
                VALUES (?, 'case_ready_to_start', ?)
                """,
                (
                    case_id,
                    (
                        f"Case {case_number} created with {len(supplier_ids)} supplier(s). "
                        "Ready to start negotiation."
                    ),
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs
                (
                    case_id,
                    action,
                    details
                )
                VALUES (?, 'create_case', ?)
                """,
                (
                    case_id,
                    (
                        "Buyer created case in Streamlit UI. "
                        f"Auto-send messages: {'yes' if auto_send_messages else 'no'}. "
                        "Human-review email notification: "
                        f"{'yes' if notify_buyer_on_human_review else 'no'}."
                    ),
                ),
            )

            conn.commit()

        return case_id


    def get_case_details(self, case_id: int) -> dict | None:
        with get_connection() as conn:
            case = conn.execute(
                """
                SELECT
                    c.*,
                    COALESCE(cnp.notify_human_review_email, 0)
                        AS notify_human_review_email
                FROM negotiation_cases c
                LEFT JOIN case_notification_preferences cnp
                    ON cnp.case_id = c.id
                WHERE c.id = ?
                """,
                (case_id,),
            ).fetchone()

            if case is None:
                return None

            suppliers = conn.execute(
                """
                SELECT s.id, s.supplier_code, s.name, s.contact_channel,
                       s.whatsapp_number, s.email, s.category
                FROM case_suppliers cs
                JOIN suppliers s ON s.id = cs.supplier_id
                WHERE cs.case_id = ? AND cs.included = 1
                ORDER BY s.name
                """,
                (case_id,),
            ).fetchall()

            events = conn.execute(
                """
                SELECT event_type, details, created_at
                FROM negotiation_events
                WHERE case_id = ?
                ORDER BY id DESC
                """,
                (case_id,),
            ).fetchall()

        return {
            "case": dict(case),
            "suppliers": [dict(row) for row in suppliers],
            "events": [dict(row) for row in events],
        }

    def update_case_status(self, case_id: int, status: str) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE negotiation_cases
                SET status = ?
                WHERE id = ?
                """,
                (status, case_id),
            )
            conn.commit()

    # ---------- Common validation ----------

    def ensure_supplier_linked_to_case(self, case_id: int, supplier_id: int) -> None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM case_suppliers
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND included = 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        if row is None:
            raise ValueError("Selected supplier is not linked to this case.")

    # ---------- Messages ----------
    def add_message(
            self,
            case_id: int,
            supplier_id: int | None,
            direction: str,
            channel: str,
            body: str,
            status: str = "recorded",
            message_type: str = "general",
            approval_required: bool = False,
            approved_by_buyer: bool = True,
            approved_at: str | None = None,
            sent_at: str | None = None,
    ) -> int:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages
                (
                    case_id,
                    supplier_id,
                    message_type,
                    approval_required,
                    approved_by_buyer,
                    approved_at,
                    sent_at,
                    direction,
                    channel,
                    body,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    supplier_id,
                    message_type,
                    1 if approval_required else 0,
                    1 if approved_by_buyer else 0,
                    approved_at,
                    sent_at,
                    direction,
                    channel,
                    body,
                    status,
                ),
            )
            message_id = int(cur.lastrowid)

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'message_added', ?)
                """,
                (
                    case_id,
                    f"{direction.title()} message added. "
                    f"Type: {message_type}. Channel: {channel}. Status: {status}.",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'add_message', ?)
                """,
                (case_id, f"Message ID {message_id} added."),
            )

            conn.commit()

        return message_id


    def list_messages_for_case(self, case_id: int) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    m.direction,
                    m.channel,
                    m.body,
                    m.status,
                    m.created_at
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.approved_at,
                    m.sent_at, 
                FROM messages m
                LEFT JOIN suppliers s ON s.id = m.supplier_id
                WHERE m.case_id = ?
                ORDER BY m.id DESC
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    # ---------- Offers ----------

    def add_offer(
        self,
        case_id: int,
        supplier_id: int,
        unit_price_usd: float,
        quantity: float | None,
        message_id: int | None,
        extraction_method: str,
        extraction_confidence: str,
        notes: str | None,
    ) -> int:
        total_price_usd = None
        if quantity is not None and quantity > 0:
            total_price_usd = unit_price_usd * quantity

        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO offers
                (
                    case_id, supplier_id, message_id, unit_price_usd,
                    quantity, total_price_usd, extraction_method,
                    extraction_confidence, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    supplier_id,
                    message_id,
                    unit_price_usd,
                    quantity,
                    total_price_usd,
                    extraction_method,
                    extraction_confidence,
                    notes,
                ),
            )

            offer_id = int(cur.lastrowid)

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'offer_recorded', ?)
                """,
                (case_id, f"Offer recorded: supplier ID {supplier_id}, unit price USD {unit_price_usd}."),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'add_offer', ?)
                """,
                (case_id, f"Offer ID {offer_id} saved."),
            )

            conn.commit()

        return offer_id

    def list_offers_for_case(self, case_id: int) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    o.id,
                    o.case_id,
                    o.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    o.message_id,
                    o.unit_price_usd,
                    o.quantity,
                    o.total_price_usd,
                    o.extraction_method,
                    o.extraction_confidence,
                    o.status,
                    o.notes,
                    o.created_at
                FROM offers o
                JOIN suppliers s ON s.id = o.supplier_id
                WHERE o.case_id = ?
                  AND o.status = 'active'
                ORDER BY o.unit_price_usd ASC, o.id DESC
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    # ---------- Recommendation / approval ----------

    def get_active_offers_for_recommendation(self, case_id: int) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    o.id AS offer_id,
                    o.case_id,
                    o.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    o.unit_price_usd,
                    o.quantity,
                    o.total_price_usd,
                    o.extraction_method,
                    o.extraction_confidence,
                    o.notes,
                    o.created_at
                FROM offers o
                JOIN suppliers s ON s.id = o.supplier_id
                WHERE o.case_id = ?
                  AND o.status = 'active'
                ORDER BY o.unit_price_usd ASC, o.id DESC
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def approve_winner(self, case_id: int, offer_id: int, reason: str) -> int:
        with get_connection() as conn:
            offer = conn.execute(
                """
                SELECT
                    o.id,
                    o.case_id,
                    o.supplier_id,
                    s.name AS supplier_name,
                    o.unit_price_usd
                FROM offers o
                JOIN suppliers s ON s.id = o.supplier_id
                WHERE o.id = ?
                  AND o.case_id = ?
                  AND o.status = 'active'
                """,
                (offer_id, case_id),
            ).fetchone()

            if offer is None:
                raise ValueError("Offer not found for this case.")

            offer = dict(offer)

            cur = conn.execute(
                """
                INSERT INTO winner_decisions
                (case_id, supplier_id, offer_id, decision_status, reason, approved_by)
                VALUES (?, ?, ?, 'approved', ?, 'buyer')
                """,
                (case_id, offer["supplier_id"], offer_id, reason),
            )

            decision_id = int(cur.lastrowid)

            conn.execute(
                """
                UPDATE negotiation_cases
                SET status = 'WINNER SELECTED'
                WHERE id = ?
                """,
                (case_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'winner_approved', ?)
                """,
                (
                    case_id,
                    f"Buyer approved {offer['supplier_name']} as winner at USD {offer['unit_price_usd']}.",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'approve_winner', ?)
                """,
                (case_id, f"Winner decision ID {decision_id} created."),
            )

            conn.commit()

        return decision_id

    def get_winner_decision(self, case_id: int) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    wd.id,
                    wd.case_id,
                    wd.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    wd.offer_id,
                    o.unit_price_usd,
                    wd.decision_status,
                    wd.reason,
                    wd.approved_by,
                    wd.created_at
                FROM winner_decisions wd
                JOIN suppliers s ON s.id = wd.supplier_id
                JOIN offers o ON o.id = wd.offer_id
                WHERE wd.case_id = ?
                ORDER BY wd.id DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()

        return dict(row) if row else None

    # ---------- Winner notification ----------

    def get_winner_notification_data(self, case_id: int) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    c.case_number,
                    c.item_material,
                    c.quantity,
                    s.name AS supplier_name,
                    o.unit_price_usd
                FROM winner_decisions wd
                JOIN negotiation_cases c ON c.id = wd.case_id
                JOIN suppliers s ON s.id = wd.supplier_id
                JOIN offers o ON o.id = wd.offer_id
                WHERE wd.case_id = ?
                ORDER BY wd.id DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()

        return dict(row) if row else None

    def get_latest_winner_supplier_id(self, case_id: int) -> int | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT supplier_id
                FROM winner_decisions
                WHERE case_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()

        return int(row["supplier_id"]) if row else None

    def get_latest_winner_notification_draft(self, case_id: int) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.name AS supplier_name,
                    s.supplier_code,
                    m.direction,
                    m.channel,
                    m.body,
                    m.status,
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.approved_at,
                    m.sent_at,
                    m.created_at
                FROM messages m
                JOIN suppliers s ON s.id = m.supplier_id
                WHERE m.case_id = ?
                  AND m.supplier_id = (
                      SELECT supplier_id
                      FROM winner_decisions
                      WHERE case_id = ?
                      ORDER BY id DESC
                      LIMIT 1
                  )
                  AND m.direction = 'outbound'
                  AND m.message_type = 'winner_notification'
                  AND m.status IN ('draft', 'sent_manual', 'sent_email')
                ORDER BY m.id DESC
                LIMIT 1
                """,
                (case_id, case_id),
            ).fetchone()

        return dict(row) if row else None

    def mark_outbound_message_sent_manual(self, message_id: int) -> int:
        """
        Mark winner notification as manually sent.

        This is intentionally limited to message_type='winner_notification'.
        """
        with get_connection() as conn:
            message = conn.execute(
                """
                SELECT case_id
                FROM messages
                WHERE id = ?
                  AND direction = 'outbound'
                  AND message_type = 'winner_notification'
                """,
                (message_id,),
            ).fetchone()

            if message is None:
                raise ValueError("Winner notification message not found.")

            case_id = int(message["case_id"])

            conn.execute(
                """
                UPDATE messages
                SET
                    status = 'sent_manual',
                    approved_by_buyer = 1,
                    approved_at = COALESCE(approved_at, CURRENT_TIMESTAMP),
                    sent_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message_id,),
            )

            conn.execute(
                """
                UPDATE negotiation_cases
                SET status = 'WINNER_NOTIFIED'
                WHERE id = ?
                """,
                (case_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'winner_notified', ?)
                """,
                (case_id, f"Buyer marked winner notification message ID {message_id} as sent."),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'mark_winner_notification_sent', ?)
                """,
                (case_id, f"Message ID {message_id} marked as sent manually."),
            )

            conn.commit()

        return case_id


    # ---------- RFQ drafts ----------

    def list_case_suppliers(self, case_id: int) -> list[dict]:
        """
        Return included suppliers for one case.

        Used when generating RFQ drafts for all selected suppliers.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.supplier_code,
                    s.name,
                    s.contact_channel,
                    s.whatsapp_number,
                    s.email,
                    s.category
                FROM case_suppliers cs
                JOIN suppliers s ON s.id = cs.supplier_id
                WHERE cs.case_id = ?
                  AND cs.included = 1
                ORDER BY s.name
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_case_basic(self, case_id: int) -> dict | None:
        """
        Return basic case fields needed for RFQ text generation.
        """
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    c.id,
                    c.case_number,
                    c.item_material,
                    c.quantity,
                    c.notes,
                    c.status,
                    c.auto_send_messages,
                    COALESCE(cnp.notify_human_review_email, 0)
                        AS notify_human_review_email
                FROM negotiation_cases c
                LEFT JOIN case_notification_preferences cnp
                    ON cnp.case_id = c.id
                WHERE c.id = ?
                """,
                (case_id,),
            ).fetchone()

        return dict(row) if row else None

    def count_rfq_drafts_for_case(self, case_id: int) -> int:
        """
        Count outbound RFQ draft messages for this case.
        """
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS draft_count
                FROM messages
                WHERE case_id = ?
                  AND direction = 'outbound'
                  AND status = 'draft'
                  AND message_type = 'rfq'
                """,
                (case_id,),
            ).fetchone()

        return int(row["draft_count"])

    def update_case_status_with_event(
        self,
        case_id: int,
        status: str,
        event_type: str,
        details: str,
    ) -> None:
        """
        Update case status and log timeline/action information.
        """
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE negotiation_cases
                SET status = ?
                WHERE id = ?
                """,
                (status, case_id),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, ?, ?)
                """,
                (case_id, event_type, details),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, ?, ?)
                """,
                (case_id, event_type, details),
            )

            conn.commit()

    def list_rfq_draft_messages_for_case(self, case_id: int) -> list[dict]:
        """
        Return outbound RFQ draft messages for a case.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    m.direction,
                    m.channel,
                    m.body,
                    m.status,
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.approved_at,
                    m.sent_at,
                    m.created_at
                FROM messages m
                JOIN suppliers s ON s.id = m.supplier_id
                WHERE m.case_id = ?
                  AND m.direction = 'outbound'
                  AND m.status = 'draft'
                  AND m.message_type = 'rfq'
                ORDER BY s.name
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def mark_rfq_drafts_sent_manual(self, case_id: int) -> int:
        """
        Mark all outbound RFQ drafts as manually sent.

        This does not send anything.
        It records that the buyer sent RFQs outside the app.
        """
        with get_connection() as conn:
            draft_count_row = conn.execute(
                """
                SELECT COUNT(*) AS draft_count
                FROM messages
                WHERE case_id = ?
                  AND direction = 'outbound'
                  AND status = 'draft'
                  AND message_type = 'rfq'
                """,
                (case_id,),
            ).fetchone()

            draft_count = int(draft_count_row["draft_count"])

            if draft_count == 0:
                raise ValueError("No outbound RFQ draft messages found for this case.")

            conn.execute(
                """
                UPDATE messages
                SET
                    status = 'sent_manual',
                    approved_by_buyer = 1,
                    approved_at = COALESCE(approved_at, CURRENT_TIMESTAMP),
                    sent_at = CURRENT_TIMESTAMP
                WHERE case_id = ?
                  AND direction = 'outbound'
                  AND status = 'draft'
                  AND message_type = 'rfq'
                """,
                (case_id,),
            )

            conn.execute(
                """
                UPDATE negotiation_cases
                SET status = 'COLLECTING_OFFERS'
                WHERE id = ?
                """,
                (case_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'rfq_sent_manual', ?)
                """,
                (
                    case_id,
                    f"Buyer marked {draft_count} RFQ message(s) as sent manually.",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'mark_rfq_sent_manual', ?)
                """,
                (
                    case_id,
                    f"{draft_count} outbound RFQ draft message(s) marked as sent_manual.",
                ),
            )

            conn.commit()

        return draft_count

    def mark_rfq_drafts_sent_manual(self, case_id: int) -> int:
        """
        Mark all outbound RFQ drafts as manually sent.

        This does not send anything.
        It only records that buyer sent them outside the app.
        """
        with get_connection() as conn:
            draft_count_row = conn.execute(
                """
                SELECT COUNT(*) AS draft_count
                FROM messages
                WHERE case_id = ?
                  AND direction = 'outbound'
                  AND status = 'draft'
                """,
                (case_id,),
            ).fetchone()

            draft_count = int(draft_count_row["draft_count"])

            if draft_count == 0:
                raise ValueError("No outbound draft messages found for this case.")

            conn.execute(
                """
                UPDATE messages
                SET status = 'sent_manual'
                WHERE case_id = ?
                  AND direction = 'outbound'
                  AND status = 'draft'
                """,
                (case_id,),
            )

            conn.execute(
                """
                UPDATE negotiation_cases
                SET status = 'COLLECTING_OFFERS'
                WHERE id = ?
                """,
                (case_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'rfq_sent_manual', ?)
                """,
                (
                    case_id,
                    f"Buyer marked {draft_count} RFQ message(s) as sent manually.",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'mark_rfq_sent_manual', ?)
                """,
                (
                    case_id,
                    f"{draft_count} outbound draft RFQ message(s) marked as sent_manual.",
                ),
            )

            conn.commit()

        return draft_count

    # ---------- Email sending ----------
    def list_email_drafts_for_case(self, case_id: int) -> list[dict]:
        """
        Return outbound email RFQ draft messages for a case.

        Important:
        This intentionally does NOT return winner notifications or negotiation drafts.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    s.email,
                    m.direction,
                    m.channel,
                    m.body,
                    m.status,
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.approved_at,
                    m.sent_at,
                    m.created_at
                FROM messages m
                JOIN suppliers s ON s.id = m.supplier_id
                WHERE m.case_id = ?
                  AND m.direction = 'outbound'
                  AND m.channel = 'email'
                  AND m.status = 'draft'
                  AND m.message_type = 'rfq'
                ORDER BY s.name
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def mark_message_sent_email(
            self,
            message_id: int,
            provider_message_id: str | None,
    ) -> None:
        with get_connection() as conn:
            message = conn.execute(
                """
                SELECT case_id
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()

            if message is None:
                raise ValueError("Message not found.")

            case_id = int(message["case_id"])

            conn.execute(
                """
                UPDATE messages
                SET
                    status = 'sent_email',
                    sent_at = CURRENT_TIMESTAMP,
                    approved_by_buyer = 1
                WHERE id = ?
                """,
                (message_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'email_sent', ?)
                """,
                (
                    case_id,
                    f"Email message ID {message_id} sent. Provider ID: {provider_message_id}",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'send_email', ?)
                """,
                (
                    case_id,
                    f"Email message ID {message_id} sent via email adapter.",
                ),
            )

            conn.commit()

    def mark_message_send_failed(
            self,
            message_id: int,
            error: str,
    ) -> None:
        with get_connection() as conn:
            message = conn.execute(
                """
                SELECT case_id
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()

            if message is None:
                raise ValueError("Message not found.")

            case_id = int(message["case_id"])

            conn.execute(
                """
                UPDATE messages
                SET status = 'send_failed'
                WHERE id = ?
                """,
                (message_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'message_send_failed', ?)
                """,
                (
                    case_id,
                    f"Message ID {message_id} failed to send: {error}",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'message_send_failed', ?)
                """,
                (
                    case_id,
                    f"Message ID {message_id} failed to send: {error}",
                ),
            )

            conn.commit()

    # ---------- Conversation simulation ----------
    def list_messages_for_case_supplier(self, case_id: int, supplier_id: int) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.approved_at,
                    m.sent_at,
                    m.direction,
                    m.channel,
                    m.body,
                    m.status,
                    m.created_at
                FROM messages m
                LEFT JOIN suppliers s ON s.id = m.supplier_id
                WHERE m.case_id = ?
                  AND m.supplier_id = ?
                ORDER BY m.id ASC
                """,
                (case_id, supplier_id),
            ).fetchall()

        return [dict(row) for row in rows]


    def list_negotiation_drafts_for_case(self, case_id: int) -> list[dict]:
        """
        Return unsent buyer negotiation/manual drafts.

        These are approval-required drafts and are separate from RFQs and winner notifications.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    m.direction,
                    m.channel,
                    m.body,
                    m.status,
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.approved_at,
                    m.sent_at,
                    m.created_at
                FROM messages m
                JOIN suppliers s ON s.id = m.supplier_id
                WHERE m.case_id = ?
                  AND m.direction = 'outbound'
                  AND m.status = 'draft'
                  AND m.message_type IN (
                        'negotiation_followup',
                        'price_reduction_request',
                        'manual_note'
                  )
                ORDER BY m.id DESC
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def mark_negotiation_draft_sent_manual(self, message_id: int) -> None:
        """
        Mark one negotiation draft as manually sent.

        This does NOT close the case.
        """
        with get_connection() as conn:
            message = conn.execute(
                """
                SELECT case_id
                FROM messages
                WHERE id = ?
                  AND direction = 'outbound'
                  AND status = 'draft'
                  AND message_type IN (
                        'negotiation_followup',
                        'price_reduction_request',
                        'manual_note'
                  )
                """,
                (message_id,),
            ).fetchone()

            if message is None:
                raise ValueError("Negotiation draft message not found.")

            case_id = int(message["case_id"])

            conn.execute(
                """
                UPDATE messages
                SET
                    status = 'sent_manual',
                    approved_by_buyer = 1,
                    approved_at = COALESCE(approved_at, CURRENT_TIMESTAMP),
                    sent_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message_id,),
            )

            conn.execute(
                """
                UPDATE negotiation_cases
                SET status = 'COLLECTING_OFFERS'
                WHERE id = ?
                """,
                (case_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'negotiation_message_sent_manual', ?)
                """,
                (
                    case_id,
                    f"Buyer marked negotiation message ID {message_id} as sent manually.",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'mark_negotiation_message_sent_manual', ?)
                """,
                (
                    case_id,
                    f"Negotiation message ID {message_id} marked as sent manually.",
                ),
            )

            conn.commit()


    # ---------- Rule-based negotiation simulation ----------

    def get_supplier_negotiation_states(self, case_id: int) -> list[dict]:
        """
        Return negotiation state rows for one case.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    ns.id,
                    ns.case_id,
                    ns.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    ns.rfq_sent_at,
                    ns.followup_sent_at,
                    ns.last_inbound_at,
                    ns.best_offer_usd,
                    ns.target_price_usd,
                    ns.negotiation_attempts,
                    ns.awaiting_supplier_reply,
                    ns.closed,
                    ns.updated_at
                FROM supplier_negotiation_state ns
                JOIN suppliers s ON s.id = ns.supplier_id
                WHERE ns.case_id = ?
                ORDER BY s.name
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def create_or_update_negotiation_state_on_rfq(
        self,
        case_id: int,
        supplier_id: int,
        rfq_sent_at: str,
    ) -> None:
        """
        Create supplier state when RFQ is sent in simulation.
        """
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO supplier_negotiation_state
                (
                    case_id,
                    supplier_id,
                    rfq_sent_at,
                    awaiting_supplier_reply,
                    updated_at
                )
                VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(case_id, supplier_id)
                DO UPDATE SET
                    rfq_sent_at = excluded.rfq_sent_at,
                    awaiting_supplier_reply = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (case_id, supplier_id, rfq_sent_at),
            )
            conn.commit()

    def update_negotiation_state_followup_sent(
        self,
        case_id: int,
        supplier_id: int,
        followup_sent_at: str,
    ) -> None:
        """
        Record that the system sent a no-response follow-up.
        """
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE supplier_negotiation_state
                SET
                    followup_sent_at = ?,
                    awaiting_supplier_reply = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE case_id = ?
                  AND supplier_id = ?
                """,
                (followup_sent_at, case_id, supplier_id),
            )
            conn.commit()

    def update_negotiation_state_after_inbound(
        self,
        case_id: int,
        supplier_id: int,
        last_inbound_at: str,
        best_offer_usd: float | None,
    ) -> None:
        """
        Update supplier state after manually entered supplier response.

        If price was extracted, best_offer_usd is updated only if it improves.
        """
        with get_connection() as conn:
            existing = conn.execute(
                """
                SELECT best_offer_usd
                FROM supplier_negotiation_state
                WHERE case_id = ?
                  AND supplier_id = ?
                """,
                (case_id, supplier_id),
            ).fetchone()

            current_best = None
            if existing and existing["best_offer_usd"] is not None:
                current_best = float(existing["best_offer_usd"])

            new_best = current_best

            if best_offer_usd is not None:
                if current_best is None:
                    new_best = float(best_offer_usd)
                else:
                    new_best = min(current_best, float(best_offer_usd))

            conn.execute(
                """
                INSERT INTO supplier_negotiation_state
                (
                    case_id,
                    supplier_id,
                    last_inbound_at,
                    best_offer_usd,
                    awaiting_supplier_reply,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                ON CONFLICT(case_id, supplier_id)
                DO UPDATE SET
                    last_inbound_at = excluded.last_inbound_at,
                    best_offer_usd = ?,
                    awaiting_supplier_reply = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    case_id,
                    supplier_id,
                    last_inbound_at,
                    new_best,
                    new_best,
                ),
            )
            conn.commit()

    def set_target_price_for_case(self, case_id: int, target_price_usd: float) -> None:
        """
        Store the same target price for all suppliers in the case.
        """
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE supplier_negotiation_state
                SET
                    target_price_usd = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE case_id = ?
                """,
                (target_price_usd, case_id),
            )
            conn.commit()

    def increment_negotiation_attempt(
        self,
        case_id: int,
        supplier_id: int,
    ) -> None:
        """
        Increment supplier negotiation attempt count after buyer/system sends a price negotiation message.
        """
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE supplier_negotiation_state
                SET
                    negotiation_attempts = negotiation_attempts + 1,
                    awaiting_supplier_reply = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE case_id = ?
                  AND supplier_id = ?
                """,
                (case_id, supplier_id),
            )
            conn.commit()

    def close_negotiation_for_case(self, case_id: int) -> None:
        """
        Mark all supplier negotiation states as closed.
        """
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE supplier_negotiation_state
                SET
                    closed = 1,
                    awaiting_supplier_reply = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE case_id = ?
                """,
                (case_id,),
            )
            conn.commit()

    def has_simulated_winner_notification(self, case_id: int) -> bool:
        """
        Prevent duplicate simulated winner recommendation/notification events.
        """
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM negotiation_events
                WHERE case_id = ?
                  AND event_type IN (
                      'simulated_winner_notified',
                      'simulated_winner_recommended'
                  )
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()

        return row is not None

#    def has_simulated_winner_notification(self, case_id: int) -> bool:
#        """
#        Prevent duplicate simulated winner notifications.
#        """
#        with get_connection() as conn:
#            row = conn.execute(
#                """
#                SELECT 1
#                FROM negotiation_events
#                WHERE case_id = ?
#                  AND event_type = 'simulated_winner_notified'
#                LIMIT 1
#                """,
#                (case_id,),
#            ).fetchone()
#
#        return row is not None

    # ---------- Email transport / imports ----------
    def list_pending_simulated_email_messages_for_case(self, case_id: int) -> list[dict]:
        """
        Return simulated outbound email RFQs that have not yet been actually sent.

        Safety:
        - Only RFQ messages are eligible.
        - Winner notifications are never auto-sent.
        - Negotiation drafts are never auto-sent while rules are undefined.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    s.email,
                    m.body,
                    m.status,
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.sent_at,
                    c.case_number,
                    c.item_material
                FROM messages m
                JOIN suppliers s ON s.id = m.supplier_id
                JOIN negotiation_cases c ON c.id = m.case_id
                WHERE m.case_id = ?
                  AND m.direction = 'outbound'
                  AND m.channel = 'email'
                  AND m.status = 'sent_simulated'
                  AND m.message_type IN (
                      'rfq',
                      'negotiation_followup',
                      'price_reduction_request',
                      'manual_note'
                  )
                  AND m.approved_by_buyer = 1
                  AND m.sent_at IS NULL
                  AND m.sent_at IS NULL
                  AND s.email IS NOT NULL
                  AND trim(s.email) <> ''
                ORDER BY m.id ASC
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]


    def email_import_exists(self, graph_message_id: str) -> bool:
        """
        Prevent importing the same Microsoft Graph email twice.
        """
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM email_imports
                WHERE graph_message_id = ?
                LIMIT 1
                """,
                (graph_message_id,),
            ).fetchone()

        return row is not None

    def record_email_import(
        self,
        graph_message_id: str,
        case_id: int,
        message_id: int,
        sender_email: str,
        subject: str,
        received_at: str,
    ) -> None:
        """
        Store imported email ID so it will not be imported again.
        """
        with get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO email_imports
                (
                    graph_message_id,
                    case_id,
                    message_id,
                    sender_email,
                    subject,
                    received_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_message_id,
                    case_id,
                    message_id,
                    sender_email,
                    subject,
                    received_at,
                ),
            )

            conn.commit()

    def find_case_supplier_by_email(
        self,
        case_id: int,
        sender_email: str,
    ) -> dict | None:
        """
        Find selected supplier by sender email.

        In EMAIL_TEST_MODE, the supplier test email may not match supplier master data.
        The transport service handles that separately.
        """
        normalized = sender_email.strip().lower()

        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    s.id,
                    s.supplier_code,
                    s.name,
                    s.email,
                    s.contact_channel
                FROM case_suppliers cs
                JOIN suppliers s ON s.id = cs.supplier_id
                WHERE cs.case_id = ?
                  AND cs.included = 1
                  AND lower(s.email) = ?
                LIMIT 1
                """,
                (case_id, normalized),
            ).fetchone()

        return dict(row) if row else None


    # ---------- Worker support ----------
    def list_cases_for_transport_worker(self) -> list[dict]:
        """Return cases that still require transport processing.

        LIMITED_COMPETITION and NO_VALID_OFFERS remain eligible because a
        supplier may reply after the RFQ deadline. The transport worker must
        import that delayed message so the supplier and case can be reopened.

        Review and completed cases remain excluded from automatic worker
        processing.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    case_number,
                    item_material,
                    quantity,
                    status,
                    auto_send_messages,
                    created_at
                FROM negotiation_cases
                WHERE status NOT IN (
                    'BUYER_REVIEW',
                    'WINNER_SELECTED',
                    'WINNER_NOTIFIED',
                    'CLOSED',
                    'CANCELLED'
                )
                ORDER BY id DESC
                """
            ).fetchall()

        return [dict(row) for row in rows]


    def log_worker_event(
        self,
        case_id: int,
        event_type: str,
        details: str,
    ) -> None:
        """
        Log worker activity without changing business state.
        """
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, ?, ?)
                """,
                (case_id, event_type, details),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, ?, ?)
                """,
                (case_id, event_type, details),
            )

            conn.commit()

    # ---------- Email threading headers ----------

    def record_email_message_header(
        self,
        message_id: int,
        case_id: int,
        supplier_id: int,
        subject: str,
        internet_message_id: str | None,
        in_reply_to: str | None = None,
        reference_chain: str | None = None,
        graph_conversation_id: str | None = None,
    ) -> None:
        """
        Store email threading metadata for one chat message.
        """
        with get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO email_message_headers
                (
                    message_id,
                    case_id,
                    supplier_id,
                    subject,
                    internet_message_id,
                    in_reply_to,
                    reference_chain,
                    graph_conversation_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    case_id,
                    supplier_id,
                    subject,
                    internet_message_id,
                    in_reply_to,
                    reference_chain,
                    graph_conversation_id,
                ),
            )

            conn.commit()

    def get_latest_email_thread_header(
        self,
        case_id: int,
        supplier_id: int,
    ) -> dict | None:
        """
        Return latest email threading metadata for this case/supplier.

        Used when sending the next outbound email.
        """
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    message_id,
                    case_id,
                    supplier_id,
                    subject,
                    internet_message_id,
                    in_reply_to,
                    reference_chain,
                    graph_conversation_id,
                    created_at
                FROM email_message_headers
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND internet_message_id IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return dict(row) if row else None

    # ---------- Message/idempotency helpers ----------

    def count_outbound_messages_for_case_supplier(
        self,
        case_id: int,
        supplier_id: int,
        status: str | None = None,
    ) -> int:
        """
        Count outbound messages for one case/supplier.

        Used by the rule engine to avoid duplicate RFQs/follow-ups.
        """
        with get_connection() as conn:
            if status is None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS message_count
                    FROM messages
                    WHERE case_id = ?
                      AND supplier_id = ?
                      AND direction = 'outbound'
                    """,
                    (case_id, supplier_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS message_count
                    FROM messages
                    WHERE case_id = ?
                      AND supplier_id = ?
                      AND direction = 'outbound'
                      AND status = ?
                    """,
                    (case_id, supplier_id, status),
                ).fetchone()

        return int(row["message_count"])

    def count_recent_followups_for_case_supplier(
            self,
            case_id: int,
            supplier_id: int,
    ) -> int:
        """
        Count follow-up messages for one supplier.
        """
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS message_count
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'outbound'
                  AND message_type = 'negotiation_followup'
                """,
                (case_id, supplier_id),
            ).fetchone()

        return int(row["message_count"])


    def mark_message_as_sent_simulated_processed(
        self,
        message_id: int,
    ) -> None:
        """
        Mark a simulated message as processed without sending.

        Useful if a message is not email/whatsapp transportable.
        """
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE messages
                SET status = 'processed_simulated'
                WHERE id = ?
                """,
                (message_id,),
            )
            conn.commit()

    # ---------- Anti-spam conversation guards ----------

    def get_supplier_negotiation_state(
            self,
            case_id: int,
            supplier_id: int,
    ) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    case_id,
                    supplier_id,
                    rfq_sent_at,
                    followup_sent_at,
                    last_inbound_at,
                    best_offer_usd,
                    target_price_usd,
                    negotiation_attempts,
                    awaiting_supplier_reply,
                    closed,
                    updated_at
                FROM supplier_negotiation_state
                WHERE case_id = ?
                  AND supplier_id = ?
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return dict(row) if row else None

    def outbound_message_body_exists_since_latest_inbound(
            self,
            case_id: int,
            supplier_id: int,
            body: str,
    ) -> bool:
        """
        Prevent repeated identical automatic messages after the latest supplier reply.
        """
        clean_body = body.strip().lower()

        if not clean_body:
            return False

        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'outbound'
                  AND lower(trim(body)) = ?
                  AND id > COALESCE(
                        (
                            SELECT MAX(id)
                            FROM messages
                            WHERE case_id = ?
                              AND supplier_id = ?
                              AND direction = 'inbound'
                        ),
                        0
                  )
                LIMIT 1
                """,
                (
                    case_id,
                    supplier_id,
                    clean_body,
                    case_id,
                    supplier_id,
                ),
            ).fetchone()

        return row is not None

    def get_latest_message_for_case_supplier(
        self,
        case_id: int,
        supplier_id: int,
    ) -> dict | None:
        """
        Return the latest chat message for one case/supplier.

        Used to prevent automatic buyer messages when the supplier
        has not replied yet.
        """
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    case_id,
                    supplier_id,
                    direction,
                    channel,
                    body,
                    status,
                    created_at
                    message_type,
                    approval_required,
                    approved_by_buyer,
                    approved_at,
                    sent_at,
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return dict(row) if row else None

    def count_outbound_messages_since_latest_inbound(
        self,
        case_id: int,
        supplier_id: int,
    ) -> int:
        """
        Count how many buyer/system messages were sent after the latest
        supplier message.

        If the supplier never replied, this counts all outbound messages.

        This is the core anti-spam guard.
        """
        with get_connection() as conn:
            latest_inbound = conn.execute(
                """
                SELECT id
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'inbound'
                ORDER BY id DESC
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

            if latest_inbound is None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS message_count
                    FROM messages
                    WHERE case_id = ?
                      AND supplier_id = ?
                      AND direction = 'outbound'
                    """,
                    (case_id, supplier_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS message_count
                    FROM messages
                    WHERE case_id = ?
                      AND supplier_id = ?
                      AND direction = 'outbound'
                      AND id > ?
                    """,
                    (case_id, supplier_id, int(latest_inbound["id"])),
                ).fetchone()

        return int(row["message_count"])

    def has_pending_buyer_message_for_case_supplier(
        self,
        case_id: int,
        supplier_id: int,
    ) -> bool:
        """
        Return True if the last message is outbound.

        Meaning:
        buyer/system already said something and is waiting for supplier.
        """
        latest = self.get_latest_message_for_case_supplier(
            case_id=case_id,
            supplier_id=supplier_id,
        )

        if latest is None:
            return False

        return latest["direction"] == "outbound"

    def close_supplier_negotiation(
        self,
        case_id: int,
        supplier_id: int,
        reason: str,
    ) -> None:
        """
        Close one supplier conversation in the negotiation state.

        This prevents further automatic messages to that supplier.
        """
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE supplier_negotiation_state
                SET
                    closed = 1,
                    awaiting_supplier_reply = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE case_id = ?
                  AND supplier_id = ?
                """,
                (case_id, supplier_id),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'supplier_negotiation_closed', ?)
                """,
                (case_id, reason),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'close_supplier_negotiation', ?)
                """,
                (case_id, reason),
            )

            conn.commit()


    # ---------- Safer inbound duplicate detection ----------

    def inbound_message_duplicate_exists(
        self,
        case_id: int,
        supplier_id: int,
        channel: str,
        body: str,
    ) -> bool:
        """
        Prevent importing/recording the same inbound supplier message repeatedly.

        This is a safety net in addition to email_imports.graph_message_id.
        """
        clean_body = body.strip()

        if not clean_body:
            return False

        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'inbound'
                  AND channel = ?
                  AND trim(body) = ?
                LIMIT 1
                """,
                (
                    case_id,
                    supplier_id,
                    channel,
                    clean_body,
                ),
            ).fetchone()

        return row is not None



    def find_case_supplier_by_code(
        self,
        case_id: int,
        supplier_code: str,
    ) -> dict | None:
        """
        Find selected supplier by supplier_code.

        This is useful for email testing when multiple suppliers use the same
        test sender address.
        """
        normalized = supplier_code.strip()

        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    s.id,
                    s.supplier_code,
                    s.name,
                    s.email,
                    s.contact_channel
                FROM case_suppliers cs
                JOIN suppliers s ON s.id = cs.supplier_id
                WHERE cs.case_id = ?
                  AND cs.included = 1
                  AND s.supplier_code = ?
                LIMIT 1
                """,
                (case_id, normalized),
            ).fetchone()

        return dict(row) if row else None

    def list_pending_approval_messages_for_case(self, case_id: int) -> list[dict]:
        """
        Messages generated by the system that require buyer approval before sending.

        Winner notifications are excluded here because they have a separate
        final winner approval workflow.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    s.email,
                    m.direction,
                    m.channel,
                    m.body,
                    m.status,
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.approved_at,
                    m.sent_at,
                    m.created_at
                FROM messages m
                JOIN suppliers s ON s.id = m.supplier_id
                WHERE m.case_id = ?
                  AND m.direction = 'outbound'
                  AND m.status = 'draft'
                  AND m.approval_required = 1
                  AND m.approved_by_buyer = 0
                  AND m.message_type <> 'winner_notification'
                ORDER BY m.id ASC
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]



    def approve_message_for_sending(self, message_id: int) -> int:
        """
        Buyer approves a generated outbound draft.

        Approval does not directly mean final purchase approval.
        It only makes this message send-eligible.

        Winner notifications are intentionally blocked here.
        """
        with get_connection() as conn:
            message = conn.execute(
                """
                SELECT
                    id,
                    case_id,
                    message_type,
                    direction,
                    status,
                    approval_required,
                    approved_by_buyer
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()

            if message is None:
                raise ValueError("Message not found.")

            if message["direction"] != "outbound":
                raise ValueError("Only outbound messages can be approved for sending.")

            if message["message_type"] == "winner_notification":
                raise ValueError(
                    "Winner notification must be handled through the final winner workflow."
                )

            if message["status"] != "draft":
                raise ValueError("Only draft messages can be approved for sending.")

            case_id = int(message["case_id"])

            conn.execute(
                """
                UPDATE messages
                SET
                    status = 'sent_simulated',
                    approved_by_buyer = 1,
                    approved_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'message_approved_for_sending', ?)
                """,
                (
                    case_id,
                    f"Buyer approved message ID {message_id} for sending.",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'approve_message_for_sending', ?)
                """,
                (
                    case_id,
                    f"Message ID {message_id} approved and marked sent_simulated.",
                ),
            )

            conn.commit()

        return case_id




    def get_message_by_id(self, message_id: int) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    m.id,
                    m.case_id,
                    m.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    s.email,
                    s.whatsapp_number,
                    m.message_type,
                    m.approval_required,
                    m.approved_by_buyer,
                    m.approved_at,
                    m.sent_at,
                    m.direction,
                    m.channel,
                    m.body,
                    m.status,
                    m.created_at
                FROM messages m
                LEFT JOIN suppliers s ON s.id = m.supplier_id
                WHERE m.id = ?
                """,
                (message_id,),
            ).fetchone()

        return dict(row) if row else None

    def get_best_offer_for_case_supplier(
            self,
            case_id: int,
            supplier_id: int,
    ) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    o.id AS offer_id,
                    o.case_id,
                    o.supplier_id,
                    s.name AS supplier_name,
                    s.supplier_code,
                    o.unit_price_usd,
                    o.quantity,
                    o.total_price_usd,
                    o.extraction_method,
                    o.extraction_confidence,
                    o.notes,
                    o.created_at
                FROM offers o
                JOIN suppliers s ON s.id = o.supplier_id
                WHERE o.case_id = ?
                  AND o.supplier_id = ?
                ORDER BY o.unit_price_usd ASC, o.id DESC
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return dict(row) if row else None

    def count_messages_for_case_supplier_type(
            self,
            case_id: int,
            supplier_id: int,
            message_type: str,
            direction: str | None = None,
    ) -> int:
        with get_connection() as conn:
            if direction is None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS message_count
                    FROM messages
                    WHERE case_id = ?
                      AND supplier_id = ?
                      AND message_type = ?
                    """,
                    (case_id, supplier_id, message_type),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS message_count
                    FROM messages
                    WHERE case_id = ?
                      AND supplier_id = ?
                      AND message_type = ?
                      AND direction = ?
                    """,
                    (case_id, supplier_id, message_type, direction),
                ).fetchone()

        return int(row["message_count"])


    def count_outbound_messages_after_latest_inbound(
            self,
            case_id: int,
            supplier_id: int,
    ) -> int:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS message_count
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'outbound'
                  AND id > COALESCE(
                        (
                            SELECT MAX(id)
                            FROM messages
                            WHERE case_id = ?
                              AND supplier_id = ?
                              AND direction = 'inbound'
                        ),
                        0
                  )
                """,
                (case_id, supplier_id, case_id, supplier_id),
            ).fetchone()

        return int(row["message_count"])

    def get_latest_outbound_message_for_case_supplier_types(
            self,
            case_id: int,
            supplier_id: int,
            message_types: list[str],
    ) -> dict | None:
        if not message_types:
            return None

        placeholders = ",".join("?" for _ in message_types)

        with get_connection() as conn:
            row = conn.execute(
                f"""
                SELECT
                    id,
                    case_id,
                    supplier_id,
                    message_type,
                    direction,
                    channel,
                    body,
                    status,
                    sent_at,
                    created_at
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'outbound'
                  AND message_type IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
                """,
                (case_id, supplier_id, *message_types),
            ).fetchone()

        return dict(row) if row else None

    def update_message_body(self, message_id: int, body: str) -> None:
        clean_body = body.strip()

        if not clean_body:
            raise ValueError("Message body is required.")

        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT case_id
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()

            if row is None:
                raise ValueError("Message not found.")

            conn.execute(
                """
                UPDATE messages
                SET body = ?
                WHERE id = ?
                """,
                (clean_body, message_id),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'update_message_body', ?)
                """,
                (
                    int(row["case_id"]),
                    f"Message ID {message_id} body updated.",
                ),
            )

            conn.commit()

    def normalize_phone_number(self, value: str | None) -> str:
        """
        Normalize phone numbers for matching.

        Meta webhook sender usually arrives without '+':
        420776183762

        Supplier CSV may contain:
        +420776183762
        """
        if not value:
            return ""

        return "".join(ch for ch in value if ch.isdigit())


    def find_supplier_by_whatsapp_number(self, phone_number: str) -> dict | None:
        normalized = self.normalize_phone_number(phone_number)

        if not normalized:
            return None

        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    supplier_code,
                    name,
                    contact_channel,
                    whatsapp_number,
                    email,
                    category,
                    active,
                    notes
                FROM suppliers
                WHERE active = 1
                  AND whatsapp_number IS NOT NULL
                  AND trim(whatsapp_number) <> ''
                """
            ).fetchall()

        for row in rows:
            supplier = dict(row)
            if self.normalize_phone_number(supplier.get("whatsapp_number")) == normalized:
                return supplier

        return None


    def list_open_cases_for_supplier(self, supplier_id: int) -> list[dict]:
        """
        Return non-final cases where this supplier is selected.

        Used by WhatsApp inbound matching when the incoming message does not
        contain a case number.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.id,
                    c.case_number,
                    c.item_material,
                    c.quantity,
                    c.notes,
                    c.status,
                    c.created_at
                FROM negotiation_cases c
                JOIN case_suppliers cs ON cs.case_id = c.id
                WHERE cs.supplier_id = ?
                  AND c.status NOT IN (
                        'WINNER_NOTIFIED',
                        'CLOSED',
                        'CANCELLED'
                  )
                ORDER BY c.id DESC
                """,
                (supplier_id,),
            ).fetchall()

        return [dict(row) for row in rows]


    def find_case_by_case_number(self, case_number: str) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    case_number,
                    item_material,
                    quantity,
                    notes,
                    status,
                    created_at
                FROM negotiation_cases
                WHERE case_number = ?
                LIMIT 1
                """,
                (case_number,),
            ).fetchone()

        return dict(row) if row else None


    def whatsapp_import_exists(self, wa_message_id: str) -> bool:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM whatsapp_imports
                WHERE wa_message_id = ?
                LIMIT 1
                """,
                (wa_message_id,),
            ).fetchone()

        return row is not None


    def record_whatsapp_import(
        self,
        wa_message_id: str,
        case_id: int,
        supplier_id: int,
        message_id: int,
        sender_phone: str,
        received_at: str | None,
    ) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO whatsapp_imports
                (
                    wa_message_id,
                    case_id,
                    supplier_id,
                    message_id,
                    sender_phone,
                    received_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    wa_message_id,
                    case_id,
                    supplier_id,
                    message_id,
                    sender_phone,
                    received_at,
                ),
            )

            conn.commit()


    def mark_message_sent_whatsapp(
        self,
        message_id: int,
        provider_message_id: str | None,
    ) -> None:
        with get_connection() as conn:
            message = conn.execute(
                """
                SELECT case_id
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()

            if message is None:
                raise ValueError("Message not found.")

            case_id = int(message["case_id"])

            conn.execute(
                """
                UPDATE messages
                SET
                    status = 'sent_whatsapp',
                    sent_at = CURRENT_TIMESTAMP,
                    approved_by_buyer = 1
                WHERE id = ?
                """,
                (message_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'whatsapp_sent', ?)
                """,
                (
                    case_id,
                    f"WhatsApp message ID {message_id} sent. Provider ID: {provider_message_id}",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs (case_id, action, details)
                VALUES (?, 'send_whatsapp', ?)
                """,
            (
                case_id,
                f"WhatsApp message ID {message_id} sent via WhatsApp adapter.",
            ),
        )

        conn.commit()

    def get_supplier_policy_state(
            self,
            case_id: int,
            supplier_id: int,
    ) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    case_id,
                    supplier_id,
                    state,
                    rfq_sent_at,
                    followup_sent_at,
                    last_inbound_at,
                    best_offer_usd,
                    target_price_usd,
                    negotiation_attempts,
                    awaiting_supplier_reply,
                    closed,
                    updated_at
                FROM supplier_negotiation_state
                WHERE case_id = ?
                  AND supplier_id = ?
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return dict(row) if row else None

    def set_supplier_policy_state(
            self,
            case_id: int,
            supplier_id: int,
            state: str,
            best_offer_usd: float | None = None,
            target_price_usd: float | None = None,
    ) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO supplier_negotiation_state
                (
                    case_id,
                    supplier_id,
                    state,
                    best_offer_usd,
                    target_price_usd,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(case_id, supplier_id)
                DO UPDATE SET
                    state = excluded.state,
                    best_offer_usd = COALESCE(excluded.best_offer_usd, supplier_negotiation_state.best_offer_usd),
                    target_price_usd = COALESCE(excluded.target_price_usd, supplier_negotiation_state.target_price_usd),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    case_id,
                    supplier_id,
                    state,
                    best_offer_usd,
                    target_price_usd,
                ),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'supplier_state_changed', ?)
                """,
                (
                    case_id,
                    f"Supplier ID {supplier_id} state set to {state}.",
                ),
            )

            conn.commit()

    def list_supplier_policy_states_for_case(self, case_id: int) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    sns.id,
                    sns.case_id,
                    sns.supplier_id,
                    s.name AS supplier_name,
                    s.supplier_code,
                    sns.state,
                    sns.best_offer_usd,
                    sns.target_price_usd,
                    sns.negotiation_attempts,
                    sns.awaiting_supplier_reply,
                    sns.closed,
                    sns.updated_at
                FROM supplier_negotiation_state sns
                JOIN suppliers s ON s.id = sns.supplier_id
                WHERE sns.case_id = ?
                ORDER BY s.name
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    # ---------- Case negotiation comparison ----------

    def list_best_supplier_offers_for_case(
        self,
        case_id: int,
    ) -> list[dict]:
        """
        Return exactly one best active offer per supplier, ranked by price.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    o.id AS offer_id,
                    o.case_id,
                    o.supplier_id,
                    s.supplier_code,
                    s.name AS supplier_name,
                    o.unit_price_usd,
                    o.extraction_method,
                    o.extraction_confidence,
                    o.created_at
                FROM offers o
                JOIN suppliers s ON s.id = o.supplier_id
                WHERE o.case_id = ?
                  AND o.status = 'active'
                  AND o.id = (
                      SELECT o2.id
                      FROM offers o2
                      WHERE o2.case_id = o.case_id
                        AND o2.supplier_id = o.supplier_id
                        AND o2.status = 'active'
                      ORDER BY
                          o2.unit_price_usd ASC,
                          o2.id DESC
                      LIMIT 1
                  )
                ORDER BY
                    o.unit_price_usd ASC,
                    o.id DESC
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def upsert_case_negotiation_context(
        self,
        case_id: int,
        initial_best_offer_usd: float,
        target_price_usd: float,
        best_supplier_id: int,
        best_offer_id: int,
        valid_offer_count: int,
        target_discount_percent: float,
        ranking_json: str,
    ) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO case_negotiation_context
                (
                    case_id,
                    initial_best_offer_usd,
                    target_price_usd,
                    best_supplier_id,
                    best_offer_id,
                    valid_offer_count,
                    target_discount_percent,
                    ranking_json,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(case_id)
                DO UPDATE SET
                    initial_best_offer_usd =
                        excluded.initial_best_offer_usd,
                    target_price_usd =
                        excluded.target_price_usd,
                    best_supplier_id =
                        excluded.best_supplier_id,
                    best_offer_id =
                        excluded.best_offer_id,
                    valid_offer_count =
                        excluded.valid_offer_count,
                    target_discount_percent =
                        excluded.target_discount_percent,
                    ranking_json =
                        excluded.ranking_json,
                    updated_at =
                        CURRENT_TIMESTAMP
                """,
                (
                    case_id,
                    initial_best_offer_usd,
                    target_price_usd,
                    best_supplier_id,
                    best_offer_id,
                    valid_offer_count,
                    target_discount_percent,
                    ranking_json,
                ),
            )

            conn.commit()

    def get_case_negotiation_context(
        self,
        case_id: int,
    ) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    cnc.case_id,
                    cnc.initial_best_offer_usd,
                    cnc.target_price_usd,
                    cnc.best_supplier_id,
                    s.name AS best_supplier_name,
                    s.supplier_code AS best_supplier_code,
                    cnc.best_offer_id,
                    cnc.valid_offer_count,
                    cnc.target_discount_percent,
                    cnc.ranking_json,
                    cnc.created_at,
                    cnc.updated_at
                FROM case_negotiation_context cnc
                JOIN suppliers s
                  ON s.id = cnc.best_supplier_id
                WHERE cnc.case_id = ?
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()

        return dict(row) if row else None



    def acquire_action_lock(
            self,
            case_id: int,
            supplier_id: int | None,
            action_key: str,
            action_type: str,
    ) -> bool:
        """Acquire an idempotency lock with crash recovery.

        A hard process or computer shutdown can occur after a lock is stored but
        before the corresponding outbound message is stored. Such an abandoned
        lock must not block the action forever.

        Fresh locks are preserved for ACTION_LOCK_LEASE_SECONDS so concurrent
        Streamlit and worker processes cannot perform the same action. Older
        locks may be reclaimed. The caller's normal message/state guards still
        decide whether the action is required, so completed actions are not
        repeated merely because their old lock is reclaimed.

        Returns:
            True  = lock created or an abandoned lock was reclaimed.
            False = another process still owns a fresh lock.
        """
        stale_modifier = f"-{ACTION_LOCK_LEASE_SECONDS} seconds"

        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO negotiation_action_locks
                (
                    case_id,
                    supplier_id,
                    action_key,
                    action_type
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    case_id,
                    supplier_id,
                    action_key,
                    action_type,
                ),
            )

            if cursor.rowcount == 1:
                conn.commit()
                return True

            delete_cursor = conn.execute(
                """
                DELETE FROM negotiation_action_locks
                WHERE case_id = ?
                  AND supplier_id IS ?
                  AND action_key = ?
                  AND created_at <= datetime('now', ?)
                """,
                (
                    case_id,
                    supplier_id,
                    action_key,
                    stale_modifier,
                ),
            )

            if delete_cursor.rowcount != 1:
                conn.commit()
                return False

            retry_cursor = conn.execute(
                """
                INSERT OR IGNORE INTO negotiation_action_locks
                (
                    case_id,
                    supplier_id,
                    action_key,
                    action_type
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    case_id,
                    supplier_id,
                    action_key,
                    action_type,
                ),
            )
            conn.commit()

            return retry_cursor.rowcount == 1

    def release_action_lock(
            self,
            case_id: int,
            supplier_id: int | None,
            action_key: str,
    ) -> None:
        """Release an action lock after a failed real delivery.

        Failed outbound deliveries must be retryable on a later worker cycle.
        """
        with get_connection() as conn:
            conn.execute(
                """
                DELETE FROM negotiation_action_locks
                WHERE case_id = ?
                  AND supplier_id IS ?
                  AND action_key = ?
                """,
                (case_id, supplier_id, action_key),
            )
            conn.commit()

    def count_supplier_outbound_message_type(
            self,
            case_id: int,
            supplier_id: int,
            message_type: str,
    ) -> int:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS message_count
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'outbound'
                  AND message_type = ?
                  AND status <> 'send_failed'
                """,
                (case_id, supplier_id, message_type),
            ).fetchone()

        return int(row["message_count"])

    def get_latest_supplier_outbound_message(
            self,
            case_id: int,
            supplier_id: int,
    ) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    case_id,
                    supplier_id,
                    message_type,
                    direction,
                    channel,
                    body,
                    status,
                    created_at,
                    sent_at
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'outbound'
                  AND status <> 'send_failed'
                ORDER BY id DESC
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return dict(row) if row else None

    def supplier_has_inbound_message(
            self,
            case_id: int,
            supplier_id: int,
    ) -> bool:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'inbound'
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return row is not None

    def set_supplier_state(
            self,
            case_id: int,
            supplier_id: int,
            state: str,
    ) -> None:
        """
        Store current supplier negotiation state.
        Uses supplier_negotiation_state table.
        """
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO supplier_negotiation_state
                (
                    case_id,
                    supplier_id,
                    state,
                    updated_at
                )
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(case_id, supplier_id)
                DO UPDATE SET
                    state = excluded.state,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    case_id,
                    supplier_id,
                    state,
                ),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events (case_id, event_type, details)
                VALUES (?, 'supplier_state_changed', ?)
                """,
                (
                    case_id,
                    f"Supplier ID {supplier_id} state changed to {state}.",
                ),
            )

            conn.commit()

    def get_supplier_state(
            self,
            case_id: int,
            supplier_id: int,
    ) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    case_id,
                    supplier_id,
                    state,
                    best_offer_usd,
                    target_price_usd,
                    updated_at
                FROM supplier_negotiation_state
                WHERE case_id = ?
                  AND supplier_id = ?
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return dict(row) if row else None

    def list_supplier_states_for_case(self, case_id: int) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    sns.case_id,
                    sns.supplier_id,
                    s.name AS supplier_name,
                    s.supplier_code,
                    sns.state,
                    sns.best_offer_usd,
                    sns.target_price_usd,
                    sns.updated_at
                FROM supplier_negotiation_state sns
                JOIN suppliers s ON s.id = sns.supplier_id
                WHERE sns.case_id = ?
                ORDER BY s.name
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_first_supplier_outbound_message_type(
            self,
            case_id: int,
            supplier_id: int,
            message_type: str,
    ) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    case_id,
                    supplier_id,
                    message_type,
                    direction,
                    channel,
                    body,
                    status,
                    created_at,
                    sent_at
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'outbound'
                  AND message_type = ?
                  AND status <> 'send_failed'
                ORDER BY id ASC
                LIMIT 1
                """,
                (case_id, supplier_id, message_type),
            ).fetchone()

        return dict(row) if row else None

    # ---------- Human review items ----------

    def create_human_review_item(
        self,
        case_id: int,
        supplier_id: int | None,
        message_id: int | None,
        review_type: str,
        reason: str,
    ) -> int:
        """
        Create a human review item unless an identical open item already exists.
        """
        with get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM human_review_items
                WHERE case_id = ?
                  AND COALESCE(supplier_id, -1) = COALESCE(?, -1)
                  AND COALESCE(message_id, -1) = COALESCE(?, -1)
                  AND review_type = ?
                  AND status = 'open'
                LIMIT 1
                """,
                (case_id, supplier_id, message_id, review_type),
            ).fetchone()

            if existing is not None:
                return int(existing["id"])

            cur = conn.execute(
                """
                INSERT INTO human_review_items
                (
                    case_id,
                    supplier_id,
                    message_id,
                    review_type,
                    reason,
                    status
                )
                VALUES (?, ?, ?, ?, ?, 'open')
                """,
                (
                    case_id,
                    supplier_id,
                    message_id,
                    review_type,
                    reason,
                ),
            )

            review_id = int(cur.lastrowid)

            conn.execute(
                """
                INSERT INTO negotiation_events
                (
                    case_id,
                    event_type,
                    details
                )
                VALUES (?, 'human_review_item_created', ?)
                """,
                (
                    case_id,
                    f"Human review item {review_id} created: {review_type}. {reason}",
                ),
            )

            conn.execute(
                """
                INSERT INTO action_logs
                (
                    case_id,
                    action,
                    details
                )
                VALUES (?, 'create_human_review_item', ?)
                """,
                (
                    case_id,
                    f"Human review item {review_id} created for supplier ID {supplier_id}.",
                ),
            )

            conn.commit()

        return review_id


    def get_human_review_email_notification_data(
        self,
        review_item_id: int,
    ) -> dict | None:
        """Return all data needed for an internal buyer notification."""
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    hri.id AS review_item_id,
                    hri.case_id,
                    hri.supplier_id,
                    hri.message_id,
                    hri.review_type,
                    hri.reason,
                    hri.status AS review_status,
                    hri.created_at AS review_created_at,
                    c.case_number,
                    c.item_material,
                    c.quantity,
                    COALESCE(cnp.notify_human_review_email, 0)
                        AS notify_human_review_email,
                    s.name AS supplier_name,
                    s.supplier_code,
                    m.body AS supplier_message
                FROM human_review_items hri
                JOIN negotiation_cases c ON c.id = hri.case_id
                LEFT JOIN case_notification_preferences cnp
                    ON cnp.case_id = c.id
                LEFT JOIN suppliers s ON s.id = hri.supplier_id
                LEFT JOIN messages m ON m.id = hri.message_id
                WHERE hri.id = ?
                """,
                (review_item_id,),
            ).fetchone()

        return dict(row) if row else None

    def claim_human_review_email_notification(
        self,
        review_item_id: int,
        recipient_email: str | None,
    ) -> bool:
        """Claim one notification exactly once when the case opted in."""
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO human_review_email_notifications
                (
                    review_item_id,
                    status,
                    recipient_email,
                    attempted_at,
                    updated_at
                )
                SELECT
                    hri.id,
                    'sending',
                    ?,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                FROM human_review_items hri
                JOIN case_notification_preferences cnp
                    ON cnp.case_id = hri.case_id
                WHERE hri.id = ?
                  AND cnp.notify_human_review_email = 1
                """,
                (recipient_email, review_item_id),
            )
            claimed = cur.rowcount == 1
            conn.commit()

        return claimed

    def complete_human_review_email_notification(
        self,
        review_item_id: int,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Record the outcome of an internal buyer notification."""
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE human_review_email_notifications
                SET status = ?,
                    sent_at = CASE
                        WHEN ? = 1 THEN CURRENT_TIMESTAMP
                        ELSE sent_at
                    END,
                    error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE review_item_id = ?
                """,
                (
                    'sent' if success else 'failed',
                    1 if success else 0,
                    error,
                    review_item_id,
                ),
            )
            conn.commit()

    def get_human_review_email_notification(
        self,
        review_item_id: int,
    ) -> dict | None:
        """Return the stored notification result for tests and diagnostics."""
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    review_item_id,
                    status,
                    recipient_email,
                    attempted_at,
                    sent_at,
                    error,
                    updated_at
                FROM human_review_email_notifications
                WHERE review_item_id = ?
                """,
                (review_item_id,),
            ).fetchone()

        return dict(row) if row else None


    def list_open_human_review_items_for_case(self, case_id: int) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    hri.id,
                    hri.case_id,
                    hri.supplier_id,
                    hri.message_id,
                    hri.review_type,
                    hri.reason,
                    hri.status,
                    hri.created_at,
                    s.name AS supplier_name,
                    s.supplier_code,
                    m.body AS message_body
                FROM human_review_items hri
                LEFT JOIN suppliers s ON s.id = hri.supplier_id
                LEFT JOIN messages m ON m.id = hri.message_id
                WHERE hri.case_id = ?
                  AND hri.status = 'open'
                ORDER BY hri.id DESC
                """,
                (case_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_open_human_review_item(self, review_item_id: int) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    hri.id,
                    hri.case_id,
                    hri.supplier_id,
                    hri.message_id,
                    hri.review_type,
                    hri.reason,
                    hri.status,
                    hri.created_at,

                    c.case_number,
                    c.item_material,
                    c.quantity,
                    c.status AS case_status,
                    c.auto_send_messages,

                    s.name AS supplier_name,
                    s.supplier_code,
                    s.contact_channel,
                    s.email,
                    s.whatsapp_number,

                    m.body AS message_body
                FROM human_review_items hri
                JOIN negotiation_cases c ON c.id = hri.case_id
                LEFT JOIN suppliers s ON s.id = hri.supplier_id
                LEFT JOIN messages m ON m.id = hri.message_id
                WHERE hri.id = ?
                  AND hri.status = 'open'
                LIMIT 1
                """,
                (review_item_id,),
            ).fetchone()

        return dict(row) if row else None

    def has_open_human_review_item_for_message(
        self,
        message_id: int,
        review_type: str | None = None,
    ) -> bool:
        with get_connection() as conn:
            if review_type is None:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM human_review_items
                    WHERE message_id = ?
                      AND status = 'open'
                    LIMIT 1
                    """,
                    (message_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM human_review_items
                    WHERE message_id = ?
                      AND review_type = ?
                      AND status = 'open'
                    LIMIT 1
                    """,
                    (message_id, review_type),
                ).fetchone()

        return row is not None


    def resolve_human_review_item(self, review_item_id: int) -> None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT case_id
                FROM human_review_items
                WHERE id = ?
                """,
                (review_item_id,),
            ).fetchone()

            if row is None:
                raise ValueError("Human review item not found.")

            case_id = int(row["case_id"])

            conn.execute(
                """
                UPDATE human_review_items
                SET
                    status = 'resolved',
                    resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (review_item_id,),
            )

            conn.execute(
                """
                INSERT INTO negotiation_events
                (
                    case_id,
                    event_type,
                    details
                )
                VALUES (?, 'human_review_item_resolved', ?)
                """,
                (
                    case_id,
                    f"Human review item {review_item_id} resolved.",
                ),
            )

            conn.commit()


    def get_latest_supplier_inbound_message(
        self,
        case_id: int,
        supplier_id: int,
    ) -> dict | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    case_id,
                    supplier_id,
                    message_type,
                    direction,
                    channel,
                    body,
                    status,
                    created_at
                FROM messages
                WHERE case_id = ?
                  AND supplier_id = ?
                  AND direction = 'inbound'
                ORDER BY id DESC
                LIMIT 1
                """,
                (case_id, supplier_id),
            ).fetchone()

        return dict(row) if row else None
